"""OpenCode Attach provider implementation.
@pocketcoder-core: OpenCode Attach Provider. Handles status detection for opencode attach TUI.
Reads TUI visual output from tmux capture-pane instead of parsing JSON from opencode run.
"""

import re
import logging
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider

logger = logging.getLogger(__name__)

# TUI status detection patterns (applied to ANSI-stripped capture-pane output)
# IDLE: TUI shows agent name + model + keybinding hints, no spinner
IDLE_PATTERN = re.compile(r'(agents|commands)\s*$', re.MULTILINE)
# PROCESSING: spinner visible, "esc interrupt" text
PROCESSING_PATTERN = re.compile(r'esc\s+(interrupt|again to interrupt)')
# RETRY: error with retry countdown
RETRY_PATTERN = re.compile(r'\[retrying.*attempt #\d+\]')
# ANSI stripping pattern
ANSI_PATTERN = re.compile(r'\x1b\[[0-9;]*m')


class OpenCodeAttachProvider(BaseProvider):
    """Provider for opencode attach TUI integration."""

    def __init__(self, terminal_id: str, session_name: str, window_name: str, agent_profile: str):
        super().__init__(terminal_id, session_name, window_name)
        self._agent_profile = agent_profile
        self._initialized = False

    def initialize(self) -> bool:
        """Initialize OpenCode Attach provider.

        The attach TUI is already running via SSH from the Proxy,
        so this is a no-op. We just mark ourselves as initialized.
        """
        self._initialized = True
        return True

    def send_input(self, message: str) -> None:
        """Send input to the opencode attach TUI via tmux send-keys.

        Args:
            message: The message to send to the TUI
        """
        tmux_client.send_keys(self.session_name, self.window_name, message)

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get OpenCode Attach TUI status by analyzing terminal pane output.

        Captures the pane, strips ANSI codes, and matches against status patterns:
        - IDLE: (agents|commands) at end of line, no spinner
        - PROCESSING: esc interrupt/again to interrupt visible
        - RETRY: [retrying...attempt #N] pattern
        - ERROR: No IDLE/PROCESSING match + error text

        Args:
            tail_lines: Optional number of lines to capture from pane

        Returns:
            TerminalStatus: Current status of the TUI
        """
        # Capture pane output using get_history (which wraps capture-pane)
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        if not output or not output.strip():
            return TerminalStatus.IDLE

        # Strip ANSI escape codes for pattern matching
        clean_output = ANSI_PATTERN.sub('', output)

        # Check for RETRY pattern first (explicit error with retry)
        if RETRY_PATTERN.search(clean_output):
            return TerminalStatus.ERROR

        # Check for PROCESSING pattern (spinner active, interrupt hint visible)
        # PROCESSING takes precedence over IDLE
        if PROCESSING_PATTERN.search(clean_output):
            return TerminalStatus.PROCESSING

        # Check for IDLE pattern (agent/commands prompt visible, no spinner)
        if IDLE_PATTERN.search(clean_output):
            return TerminalStatus.IDLE

        # Fallback: if no IDLE/PROCESSING match but there's error-like text
        # (contains "error", "failed", "exception", etc.)
        error_indicators = ['error', 'failed', 'exception', 'traceback']
        if any(indicator in clean_output.lower() for indicator in error_indicators):
            return TerminalStatus.ERROR

        # Default to IDLE if we can't determine status (TUI is likely at prompt)
        return TerminalStatus.IDLE

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the last message from the TUI output.

        The opencode attach TUI displays responses in a structured format.
        We extract the last complete response block by looking for
        message patterns in the output.

        Args:
            script_output: Raw terminal output from capture-pane

        Returns:
            str: The last message content from the TUI
        """
        # Strip ANSI codes
        clean_output = ANSI_PATTERN.sub('', script_output)

        lines = clean_output.splitlines()

        # Find the last message block by looking for common message patterns
        # Messages typically appear after agent/model indicators and before the next prompt
        message_parts = []
        in_message = False

        for line in reversed(lines):
            stripped = line.strip()

            # Skip empty lines
            if not stripped:
                continue

            # If we hit an IDLE pattern, we've found the end of the previous message
            if IDLE_PATTERN.search(stripped):
                if message_parts:
                    break
                continue

            # If we're in processing state, skip those lines
            if PROCESSING_PATTERN.search(stripped):
                message_parts = []
                continue

            # Collect non-prompt, non-status lines as message content
            if not in_message and stripped:
                in_message = True

            if in_message and stripped:
                # Skip common TUI elements like keybinding hints
                if 'esc' in stripped.lower() and ('interrupt' in stripped.lower() or 'again' in stripped.lower()):
                    continue
                message_parts.insert(0, stripped)

        result = '\n'.join(message_parts).strip()

        if not result:
            raise ValueError("No message found in OpenCode Attach TUI output")

        return result

    def get_idle_pattern_for_log(self) -> str:
        """Return a pattern to search for in logs to detect IDLE status.

        Returns:
            str: Regex pattern for IDLE state detection
        """
        return r'(agents|commands)\s*$'

    def exit_cli(self) -> str:
        """Get the command to exit the opencode attach TUI.

        Returns:
            str: Escape sequence to send
        """
        return "\x03"  # Ctrl+C to interrupt

    def cleanup(self) -> None:
        """Clean up OpenCode Attach provider resources."""
        self._initialized = False