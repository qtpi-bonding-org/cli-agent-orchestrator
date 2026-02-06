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

class OpenCodeProvider(BaseProvider):
    """Provider for OpenCode CLI tool integration."""

    def __init__(self, terminal_id: str, session_name: str, window_name: str, agent_profile: str):
        super().__init__(terminal_id, session_name, window_name)
        self._agent_profile = agent_profile
        self._last_step_id = None

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

        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get OpenCode status by analyzing terminal output (JSON stream)."""
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        if not output:
            return TerminalStatus.ERROR

        # OpenCode in JSON mode outputs one JSON object per line.
        # We look at the last few lines to determine state.
        lines = output.strip().split('\n')
        
        # Iterate backwards to find the last valid JSON event
        for line in reversed(lines):
            try:
                line = line.strip()
                if not line:
                    continue
                
                # Try to parse line as JSON
                # Note: The terminal captures raw output, so it might contain echoes of input
                # which are NOT JSON. We skip those.
                if not line.startswith('{') or not line.endswith('}'):
                     continue

                event = json.loads(line)
                event_type = event.get("type")

                # State detection logic based on OpenCode events
                if event_type == "step_finish":
                     return TerminalStatus.IDLE # Waiting for next user input
                
                if event_type == "step_start":
                     return TerminalStatus.PROCESSING
                
                if event_type == "tool_use":
                     return TerminalStatus.PROCESSING

                if event_type == "error":
                     return TerminalStatus.ERROR

            except json.JSONDecodeError:
                continue

        # Fallback: if we haven't seen a clear event recently, check if we are just sitting at a prompt?
        # In JSON mode, there is no prompt. So we assume IDLE if nothing is happening?
        # Actually without a "step_finish", we typically are Processing or just started.
        # Let's assume IDLE if we can't find anything active.
        return TerminalStatus.IDLE

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the final text response from the JSON log."""
        lines = script_output.strip().split('\n')
        final_text = []
        
        # We need to find the text parts associated with the LAST turn.
        # This is tricky without session logic, but assuming we just want the last output:
        
        for line in reversed(lines):
             try:
                if not line.strip(): continue
                if not (line.startswith('{') and line.endswith('}')): continue
                
                event = json.loads(line)
                if event.get("type") == "text":
                     # We found a text part.
                     # In a real implementation, we should accumulate ALL text parts 
                     # belonging to the last step.
                     # For now, let's just grab the last text chunk.
                     part = event.get("part", {})
                     text = part.get("text", "")
                     if text:
                         return text.strip()
             except:
                 continue
                 
        return "No response found."

    def get_idle_pattern_for_log(self) -> str:
        """Return a pattern to search for in logs to detect IDLE."""
        # In JSON mode, we look for step_finish event
        return '"type":"step_finish"'

    def exit_cli(self) -> str:
        """Command to exit."""
        return "/exit"

    def cleanup(self) -> None:
        pass
