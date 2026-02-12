"""OpenCode provider implementation.
@pocketcoder-core: OpenCode Provider. Custom extension to sync CAO with OpenCode events.
"""

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
# We exclude \n (0x0a), \r (0x0d), and \t (0x09) from removal
CONTROL_CHAR_PATTERN = r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]"

class OpenCodeProvider(BaseProvider):
    """Provider for OpenCode CLI tool integration."""

    def __init__(self, terminal_id: str, session_name: str, window_name: str, agent_profile: str):
        super().__init__(terminal_id, session_name, window_name)
        self._agent_profile = agent_profile
        self._last_step_id = None
        self._initialized = False

    def initialize(self) -> bool:
        """Initialize OpenCode CLI provider by ensuring shell is ready."""
        # Just wait for the shell to be responsive before we attempt the first 'run'
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        self._initialized = True
        return True

    def send_input(self, message: str) -> None:
        """Execute opencode run with the provided message."""
        # Launch OpenCode in JSON mode to make parsing reliable
        # We use a heredoc to safely pass multi-line messages with quotes
        command = f"opencode run --format json --continue --agent {self._agent_profile} << 'EOF_OPENCODE'\n{message}\nEOF_OPENCODE"
        
        tmux_client.send_keys(self.session_name, self.window_name, command)

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get OpenCode status by analyzing terminal output with JSON events."""
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        if not output or not output.strip():
             return TerminalStatus.IDLE

        # Clean output for easier regex/parsing
        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)
        clean_output = re.sub(ESCAPE_SEQUENCE_PATTERN, "", clean_output)
        clean_output = re.sub(CONTROL_CHAR_PATTERN, "", clean_output)
        
        lines = [line.strip() for line in clean_output.splitlines() if line.strip()]
        
        # 1. Look for completion markers in the relevant history
        # We check both forms of JSON spacing for maximum robustness
        has_finish_event = '"type":"step_finish"' in clean_output or '"type": "step_finish"' in clean_output
        has_error_event = '"type":"error"' in clean_output or '"type": "error"' in clean_output
        
        # 2. Check for shell prompt at the very end
        # The prompt is the definitive signal that the process has returned control to the shell
        at_prompt = lines and re.search(r"root@.*:.*#\s*$", lines[-1])

        status = TerminalStatus.PROCESSING
        if at_prompt:
             if has_finish_event:
                  status = TerminalStatus.COMPLETED
             elif has_error_event:
                  status = TerminalStatus.ERROR
             else:
                  status = TerminalStatus.IDLE

        logger.debug(f"OpenCode get_status: at_prompt={at_prompt}, has_finish={has_finish_event}, status={status}")
        
        if at_prompt:
             return status

        # 3. If not at prompt, determine if we are still processing
        for line in reversed(lines):
            try:
                json_match = re.search(r'(\{.*\})', line)
                if not json_match:
                    continue
                    
                event = json.loads(json_match.group(1))
                event_type = event.get("type")
                
                if event_type in ["step_start", "text", "call", "result", "tool_use", "step_finish"]:
                     return TerminalStatus.PROCESSING
                
                if event_type == "error":
                     return TerminalStatus.ERROR

            except (json.JSONDecodeError, ValueError):
                continue

        return TerminalStatus.PROCESSING

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract agent's final response message by gathering text from the LAST message block."""
        lines = script_output.strip().splitlines()
        
        # 1. Find the last sessionID/messageID from a step_finish event
        last_message_id = None
        for line in reversed(lines):
            try:
                json_match = re.search(r'(\{.*\})', line.strip())
                if not json_match:
                    continue
                event = json.loads(json_match.group(1))
                if event.get("type") == "step_finish":
                     last_message_id = event.get("messageID") or event.get("part", {}).get("messageID")
                     if last_message_id:
                          break
            except (json.JSONDecodeError, ValueError):
                continue

        all_text_parts = []
        
        # 2. Gather all text events matching that specific message ID
        for line in lines:
            try:
                json_match = re.search(r'(\{.*\})', line.strip())
                if not json_match:
                    continue
                event = json.loads(json_match.group(1))
                if event.get("type") == "text":
                     message_id = event.get("messageID") or event.get("part", {}).get("messageID")
                     # If we found a specific turn, only take parts from that turn
                     # Otherwise (fallback), take anything that looks like a response
                     if not last_message_id or message_id == last_message_id:
                          part = event.get("part", {})
                          text = part.get("text", "")
                          if text:
                               all_text_parts.append(text)
            except (json.JSONDecodeError, ValueError):
                continue
        
        final_answer = "".join(all_text_parts).strip()

        if not final_answer:
            # Final fallback: if JSON parsing failed but we are finished, 
            # try to grab anything between the last command and the prompt
            # but JSON extraction is the primary way.
            raise ValueError("No text found in OpenCode output for the last message turn")

        # Clean up the message
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
