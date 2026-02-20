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
        # Wait for shell to be ready first
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        # OpenCode is a "one-shot per turn" tool in its current CLI form, 
        # so 'initialize' just ensures the environment is ready for the first 'send_input'.
        # We report IDLE as soon as the shell is ready.
        self._initialized = True
        return True

    def send_input(self, message: str) -> None:
        """Execute opencode run with the provided message."""
        # Use a heredoc to safely pass multi-line messages with quotes
        # We use 'opencode run' which triggers a full reasoning turn.
        # MANDATORY: The agent profile must be passed to select the correct personna.
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
        collapsed_output = clean_output.replace("\n", "").replace("\r", "")
        has_finish_event = any(m in collapsed_output for m in ['"type":"step-finish"', '"type":"step_finish"', '"type": "step-finish"', '"type": "step_finish"'])
        has_error_event = any(m in collapsed_output for m in ['"type":"error"', '"type": "error"'])
        
        # 2. Check for shell prompt at the very end
        # This is the definitive signal that the process has returned control to the shell
        # lenient match for both root/user prompts and simple $ prompts
        last_line = lines[-1] if lines else ""
        at_prompt = bool(lines and re.search(r"(?:[#$]|root@.*[#$])\s*$", last_line))

        if at_prompt:
             # If we just finished a turn, we are COMPLETED until the next turn starts.
             # If we are just sitting at a prompt with no history of finishing, we are IDLE.
             if has_finish_event:
                  return TerminalStatus.COMPLETED
             elif has_error_event:
                  return TerminalStatus.ERROR
             else:
                  return TerminalStatus.IDLE

        # 3. If not at prompt, determine if we are still processing JSON events
        for line in reversed(lines):
            try:
                json_match = re.search(r'(\{.*\})', line)
                if not json_match:
                    continue
                    
                event = json.loads(json_match.group(1))
                event_type = event.get("type", "").replace("-", "_")
                
                if event_type in ["step_start", "text", "call", "result", "tool_use", "step_finish"]:
                     return TerminalStatus.PROCESSING
                
                if event_type == "error":
                     return TerminalStatus.ERROR

            except (json.JSONDecodeError, ValueError):
                continue

        # Default to processing if we are between lines and not at prompt
        return TerminalStatus.PROCESSING

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract agent's final response message by gathering text from the LAST message block."""
        # Clean and collapse for robust JSON detection across line wraps
        clean_output = re.sub(ANSI_CODE_PATTERN, "", script_output)
        clean_output = re.sub(ESCAPE_SEQUENCE_PATTERN, "", clean_output)
        clean_output = re.sub(CONTROL_CHAR_PATTERN, "", clean_output)
        collapsed = clean_output.replace("\n", "").replace("\r", "")
        
        # 1. Extract all valid JSON objects from the stream
        json_objects = []
        decoder = json.JSONDecoder()
        pos = 0
        while pos < len(collapsed):
            try:
                start = collapsed.find('{', pos)
                if start == -1:
                    break
                obj, end = decoder.raw_decode(collapsed[start:])
                json_objects.append(obj)
                pos = start + end
            except json.JSONDecodeError:
                pos = start + 1

        logger.info(f"OpenCode extractor: Found {len(json_objects)} JSON objects in history")
        if json_objects:
             logger.debug(f"First JSON types: {[o.get('type') for o in json_objects[:5]]}")
             logger.debug(f"Last JSON types: {[o.get('type') for o in json_objects[-5:]]}")

        # 2. Find the last sessionID/messageID from a step_finish event
        last_message_id = None
        for event in reversed(json_objects):
            etype = event.get("type", "").replace("-", "_")
            if etype == "step_finish":
                 last_message_id = event.get("messageID") or event.get("part", {}).get("messageID")
                 if last_message_id:
                      logger.info(f"OpenCode extractor: Found last_message_id={last_message_id}")
                      break

        all_text_parts = []
        
        # 3. Gather all text events
        # We look in both event['text'] and event['part']['text']
        for event in json_objects:
            etype = event.get("type", "").replace("-", "_")
            if etype == "text":
                 msg_id = event.get("messageID") or event.get("part", {}).get("messageID")
                 
                 # Only take parts belonging to the final message ID (if we found one)
                 # Otherwise take all text events (as a fallback)
                 if not last_message_id or msg_id == last_message_id:
                      # Try nested first, then flat
                      part_text = event.get("part", {}).get("text")
                      flat_text = event.get("text")
                      
                      text = part_text if part_text is not None else flat_text
                      if text:
                           all_text_parts.append(text)
        
        final_answer = "".join(all_text_parts).strip()
        logger.info(f"OpenCode extractor: Extracted {len(all_text_parts)} text parts for message {last_message_id}. Final length: {len(final_answer)}")
        if final_answer:
             logger.debug(f"Final answer preview: {final_answer[:100]}...")

        if not final_answer:
            # If we couldn't parse JSON text blocks, it likely crashed or spat out raw text.
            # Return the cleaned raw output so the orchestrator can see the error.
            fallback_text = clean_output.strip()
            if not fallback_text:
                fallback_text = script_output.strip()
            return fallback_text

        return final_answer.strip()

    def get_idle_pattern_for_log(self) -> str:
        """Return a pattern to search for in logs to detect IDLE."""
        # In JSON mode, we look for step_finish/step-finish event
        return r'"type":\s*"step[_-]finish"'

    def exit_cli(self) -> str:
        """Command to exit."""
        return "\x03" # Ctrl+C

    def cleanup(self) -> None:
        """Clean up OpenCode provider."""
        self._initialized = False
