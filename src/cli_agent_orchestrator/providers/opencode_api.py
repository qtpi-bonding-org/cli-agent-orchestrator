"""OpenCode API provider implementation.
@pocketcoder-core: OpenCode API Provider. Directly communicates with OpenCode HTTP server.
"""

import logging
import requests
import os
from typing import Optional, Dict, Any

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider

logger = logging.getLogger(__name__)

# Constants
OPENCODE_URL = os.environ.get("OPENCODE_URL", "http://opencode:3000")

class OpenCodeApiProvider(BaseProvider):
    """Provider for OpenCode HTTP API integration."""

    def __init__(self, terminal_id: str, session_name: str, window_name: str, agent_profile: str):
        super().__init__(terminal_id, session_name, window_name)
        self._agent_profile = agent_profile
        self._initialized = False
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def initialize(self) -> bool:
        """Initialize OpenCode API provider."""
        # Check if the server is healthy
        try:
            resp = self._session.get(f"{OPENCODE_URL}/health", timeout=5)
            if resp.status_code == 200:
                self._initialized = True
                return True
        except Exception as e:
            logger.error(f"OpenCode API health check failed: {e}")
        
        return False

    def send_input(self, message: str) -> None:
        """Send prompt to the opencode server via prompt_async."""
        # The session_name in CAO corresponds to the sessionID in OpenCode
        url = f"{OPENCODE_URL}/session/{self.session_name}/prompt_async"
        
        payload = {
            "parts": [{"type": "text", "text": message}],
            "agent": self._agent_profile
        }
        
        try:
            resp = self._session.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info(f"Sent async prompt to OpenCode session {self.session_name}")
        except Exception as e:
            logger.error(f"Failed to send async prompt to OpenCode: {e}")
            raise

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Always return IDLE for the OpenCode API.
        
        OpenCode internally handles message batching and queuing.
        This implements a 'Fire and Forget' strategy.
        """
        return TerminalStatus.IDLE

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Fetch the last assistant message from the OpenCode API.
        
        Args:
            script_output: Ignored (we use the API instead of Tmux output)

        Returns:
            str: The last message content from the session
        """
        url = f"{OPENCODE_URL}/session/{self.session_name}/messages"
        
        try:
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            messages = resp.json()
            
            # Find the latest assistant message
            for msg_entry in reversed(messages):
                info = msg_entry.get("info", {})
                if info.get("role") == "assistant":
                    parts = msg_entry.get("parts", [])
                    text_parts = [p.get("text", "") for p in parts if p.get("type") == "text"]
                    return "".join(text_parts).strip()
            
            return "No assistant message found in session history."
        except Exception as e:
            logger.error(f"Failed to fetch messages from OpenCode API: {e}")
            return f"Error fetching message: {e}"

    def get_idle_pattern_for_log(self) -> str:
        """This provider doesn't use logs for status detection."""
        return ".*"

    def exit_cli(self) -> str:
        """No interactive CLI to exit."""
        return ""

    def cleanup(self) -> None:
        """Clean up resources."""
        self._session.close()
        self._initialized = False
