"""OpenCode provider implementation."""

import logging
import json
import re
from typing import Optional, Dict, Any

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

# Constants
# We inject the CAO MCP server so the agent can orchestrate (assign, handoff, etc.)
OPENCODE_CMD = 'opencode run --format json --continue --mcp "python3 -m cli_agent_orchestrator.mcp_server.server"'

# Regex patterns for cleaning (module-level constants)
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
ESCAPE_SEQUENCE_PATTERN = r"\[[?0-9;]*[a-zA-Z]"
CONTROL_CHAR_PATTERN = r"[\x00-\x1f\x7f-\x9f]"

class OpenCodeProvider(BaseProvider):
    """Provider for OpenCode CLI tool integration."""

    def __init__(self, terminal_id: str, session_name: str, window_name: str, agent_profile: str):
        super().__init__(terminal_id, session_name, window_name)
        self._agent_profile = agent_profile
        self._last_step_id = None
        self._initialized = False

    def initialize(self) -> bool:
        """Initialize OpenCode CLI provider."""
        # Wait for shell to be ready first
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        # Launch OpenCode in JSON mode to make parsing reliable
        # We append --continue to pick up existing sessions if needed, 
        # or we might want to start fresh. For swarms, --continue is usually good.
        command = f"{OPENCODE_CMD}"
        
        # If agent_profile is set, we might want to pass it as --agent (if supported)
        # or just assume the profile implies a specific configuration.
        # For now, we just run the default opencode.
        if self._agent_profile and self._agent_profile != "default":
             command += f" --agent {self._agent_profile}"

        tmux_client.send_keys(self.session_name, self.window_name, command)

        # We wait until we see some JSON output indicating startup
        if not wait_until_status(self, TerminalStatus.IDLE, timeout=30.0):
             # It might be fine, it just means it's ready for input.
             # But let's log it.
             logger.warning("OpenCode initialization took longer than 30s or detected status incorrectly.")

        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get OpenCode status by analyzing terminal output with JSON events."""
        logger.debug(f"get_status: tail_lines={tail_lines}")
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        if not output:
             # Initially, if no output, we are technically IDLE (ready for first command)
             return TerminalStatus.IDLE

        lines = output.strip().splitlines()
        
        # Iterate backwards to find the last valid JSON event
        for line in reversed(lines):
            try:
                # Find JSON part if mixed with other output
                json_match = re.search(r'(\{.*\})', line)
                if not json_match:
                    continue
                    
                event = json.loads(json_match.group(1))
                event_type = event.get("type")
                
                # State detection logic based on OpenCode events
                if event_type == "step_finish":
                     # For CAO handoff to work, we must signal COMPLETED when the turn is done
                     return TerminalStatus.COMPLETED
                
                if event_type == "step_start":
                     return TerminalStatus.PROCESSING
                
                # If we see actual content, we are definitely processing
                if event_type in ["text", "call", "result", "tool_use"]:
                     return TerminalStatus.PROCESSING

                if event_type == "error":
                     return TerminalStatus.ERROR

            except (json.JSONDecodeError, ValueError):
                continue

        # Check for shell prompt if opencode hasn't started yet or has crashed
        if lines and re.search(r"(\$|#|>)\s*$", lines[-1]):
             return TerminalStatus.IDLE

        return TerminalStatus.PROCESSING

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract agent's final response message by gathering all text events."""
        lines = script_output.strip().splitlines()
        
        all_text_parts = []
        
        # Accumulate all text parts from the whole output
        # (Assuming one-shot run, this gets the whole response)
        for line in lines:
            try:
                json_match = re.search(r'(\{.*\})', line.strip())
                if not json_match:
                    continue
                    
                event = json.loads(json_match.group(1))
                if event.get("type") == "text":
                     part = event.get("part", {})
                     text = part.get("text", "")
                     if text:
                          all_text_parts.append(text)
            except (json.JSONDecodeError, ValueError):
                continue
        
        final_answer = "".join(all_text_parts).strip()

        if not final_answer:
            raise ValueError("No text found in OpenCode output")

        # Clean up the message (JSON text should be clean, but for robustness)
        final_answer = re.sub(ANSI_CODE_PATTERN, "", final_answer)
        final_answer = re.sub(ESCAPE_SEQUENCE_PATTERN, "", final_answer)
        final_answer = re.sub(CONTROL_CHAR_PATTERN, "", final_answer)
        return final_answer.strip()

    def get_idle_pattern_for_log(self) -> str:
        """Return a pattern to search for in logs to detect IDLE."""
        # In JSON mode, we look for step_finish event
        return r'"type":\s*"step_finish"'

    def exit_cli(self) -> str:
        """Command to exit."""
        return "\x03" # Ctrl+C

    def cleanup(self) -> None:
        """Clean up OpenCode provider."""
        self._initialized = False
