"""Property tests for OpenCode Attach provider status detection.

**Validates: Requirements 5.1, 5.2, 5.3**
"""

import re
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.opencode_attach import (
    IDLE_PATTERN,
    PROCESSING_PATTERN,
    OpenCodeAttachProvider,
)


class TestProperty1AttachTUIStatusDetectionConsistency:
    """Property 1: Attach TUI Status Detection Consistency.

    For any pane output string:
    - If the string matches the IDLE regex pattern (contains "agents" or "commands"
      at end of line) and does not match the PROCESSING pattern, the provider
      SHALL return IDLE status.
    - If the string matches the PROCESSING regex pattern (contains "esc interrupt"
      or "esc again to interrupt"), the provider SHALL return PROCESSING status.
    - The PROCESSING pattern takes precedence over the IDLE pattern.
    """

    @given(
        st.text(
            alphabet=st.characters(
                whitelist_categories=('L', 'N', 'Zs', 'Pd', 'Ps', 'Pe'),
                whitelist_characters=' agents commands\n\r\t ',
            ),
            min_size=1,
            max_size=200,
        )
    )
    @settings(max_examples=100)
    def test_idle_pattern_without_processing_returns_idle(self, pane_output: str):
        """Property: IDLE pattern match without PROCESSING match → IDLE status.

        When pane output contains 'agents' or 'commands' at end of line
        and does NOT contain 'esc interrupt' or 'esc again to interrupt',
        the provider SHALL return IDLE status.
        """
        # Ensure the output matches IDLE pattern but NOT PROCESSING pattern
        idle_markers = ["agents\n", "commands\n", "agents\r\n", "commands\r\n"]
        has_idle_marker = any(marker in pane_output for marker in idle_markers)

        # Add IDLE marker if not present, ensuring no PROCESSING marker
        if not has_idle_marker:
            pane_output = pane_output + "\nagents"

        # Verify no PROCESSING pattern
        if PROCESSING_PATTERN.search(pane_output):
            pane_output = re.sub(PROCESSING_PATTERN, "", pane_output)

        # Verify IDLE pattern is present
        assert IDLE_PATTERN.search(pane_output), f"Test setup failed: no IDLE pattern in '{pane_output}'"
        assert not PROCESSING_PATTERN.search(pane_output), f"Test setup failed: PROCESSING pattern present in '{pane_output}'"

        # Create mock and patch get_history
        mock_get_history = MagicMock(return_value=pane_output)
        with patch.object(
            __import__('cli_agent_orchestrator.providers.opencode_attach', fromlist=['tmux_client']).tmux_client,
            'get_history',
            mock_get_history
        ):
            provider = OpenCodeAttachProvider("test123", "test-session", "window-0", "poco")
            status = provider.get_status()

        assert status == TerminalStatus.IDLE, f"Expected IDLE for pane output: {repr(pane_output)}"

    @given(
        st.text(
            alphabet=st.characters(
                whitelist_categories=('L', 'N', 'Zs', 'Pd', 'Ps', 'Pe'),
                whitelist_characters=' esc interrupt again to \n\r\t',
            ),
            min_size=1,
            max_size=200,
        )
    )
    @settings(max_examples=100)
    def test_processing_pattern_returns_processing(self, pane_output: str):
        """Property: PROCESSING pattern match → PROCESSING status.

        When pane output contains 'esc interrupt' or 'esc again to interrupt',
        the provider SHALL return PROCESSING status.
        """
        # Ensure the output matches PROCESSING pattern
        processing_markers = [
            "esc interrupt",
            "esc again to interrupt",
            "esc  interrupt",
            "esc   again   to   interrupt",
        ]
        has_processing_marker = any(marker in pane_output for marker in processing_markers)

        # Add PROCESSING marker if not present
        if not has_processing_marker:
            pane_output = pane_output + " esc interrupt"

        # Verify PROCESSING pattern is present
        assert PROCESSING_PATTERN.search(pane_output), f"Test setup failed: no PROCESSING pattern in '{pane_output}'"

        # Create mock and patch get_history
        mock_get_history = MagicMock(return_value=pane_output)
        with patch.object(
            __import__('cli_agent_orchestrator.providers.opencode_attach', fromlist=['tmux_client']).tmux_client,
            'get_history',
            mock_get_history
        ):
            provider = OpenCodeAttachProvider("test123", "test-session", "window-0", "poco")
            status = provider.get_status()

        assert status == TerminalStatus.PROCESSING, f"Expected PROCESSING for pane output: {repr(pane_output)}"

    @given(
        st.text(
            alphabet=st.characters(
                whitelist_categories=('L', 'N', 'Zs', 'Pd', 'Ps', 'Pe'),
                whitelist_characters=' agents commands esc interrupt again to \n\r\t',
            ),
            min_size=1,
            max_size=200,
        )
    )
    @settings(max_examples=100)
    def test_processing_takes_precedence_over_idle(self, pane_output: str):
        """Property: PROCESSING takes precedence over IDLE.

        When pane output matches BOTH IDLE and PROCESSING patterns,
        the provider SHALL return PROCESSING status (not IDLE).
        """
        # Ensure output matches both patterns
        idle_marker = "agents\n"
        processing_marker = "esc interrupt"

        # Build output with both markers
        pane_output = f"some text {idle_marker} more text {processing_marker}"

        # Verify both patterns are present
        assert IDLE_PATTERN.search(pane_output), f"Test setup failed: no IDLE pattern in '{pane_output}'"
        assert PROCESSING_PATTERN.search(pane_output), f"Test setup failed: no PROCESSING pattern in '{pane_output}'"

        # Create mock and patch get_history
        mock_get_history = MagicMock(return_value=pane_output)
        with patch.object(
            __import__('cli_agent_orchestrator.providers.opencode_attach', fromlist=['tmux_client']).tmux_client,
            'get_history',
            mock_get_history
        ):
            provider = OpenCodeAttachProvider("test123", "test-session", "window-0", "poco")
            status = provider.get_status()

        assert status == TerminalStatus.PROCESSING, f"Expected PROCESSING to take precedence over IDLE for: {repr(pane_output)}"

    @given(
        st.text(
            alphabet=st.characters(
                whitelist_categories=('L', 'N', 'Zs', 'Pd', 'Ps', 'Pe'),
                whitelist_characters=' abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 \n\r\t',
            ),
            min_size=1,
            max_size=200,
        )
    )
    @settings(max_examples=100)
    def test_no_pattern_match_returns_idle(self, pane_output: str):
        """Property: No pattern match → IDLE status (default).

        When pane output matches NEITHER IDLE nor PROCESSING pattern,
        the provider SHALL return IDLE status (default fallback).
        """
        # Ensure output does NOT match either pattern
        idle_markers = ["agents", "commands"]
        processing_markers = ["esc interrupt", "esc again to interrupt"]

        # Remove any patterns that might be present
        for marker in idle_markers:
            pane_output = pane_output.replace(marker, "xxx")
        for marker in processing_markers:
            pane_output = re.sub(re.escape(marker), "yyy", pane_output)

        # Verify no patterns are present
        assert not IDLE_PATTERN.search(pane_output), f"Test setup failed: IDLE pattern present in '{pane_output}'"
        assert not PROCESSING_PATTERN.search(pane_output), f"Test setup failed: PROCESSING pattern present in '{pane_output}'"

        # Create mock and patch get_history
        mock_get_history = MagicMock(return_value=pane_output)
        with patch.object(
            __import__('cli_agent_orchestrator.providers.opencode_attach', fromlist=['tmux_client']).tmux_client,
            'get_history',
            mock_get_history
        ):
            provider = OpenCodeAttachProvider("test123", "test-session", "window-0", "poco")
            status = provider.get_status()

        assert status == TerminalStatus.IDLE, f"Expected IDLE as default for pane output: {repr(pane_output)}"


class TestProperty1EdgeCases:
    """Additional edge case tests for Property 1."""

    def test_empty_output_returns_idle(self):
        """Empty pane output should return IDLE status."""
        mock_get_history = MagicMock(return_value="")
        with patch.object(
            __import__('cli_agent_orchestrator.providers.opencode_attach', fromlist=['tmux_client']).tmux_client,
            'get_history',
            mock_get_history
        ):
            provider = OpenCodeAttachProvider("test123", "test-session", "window-0", "poco")
            status = provider.get_status()
        assert status == TerminalStatus.IDLE

    def test_whitespace_only_output_returns_idle(self):
        """Whitespace-only pane output should return IDLE status."""
        mock_get_history = MagicMock(return_value="   \n\t\n   \n")
        with patch.object(
            __import__('cli_agent_orchestrator.providers.opencode_attach', fromlist=['tmux_client']).tmux_client,
            'get_history',
            mock_get_history
        ):
            provider = OpenCodeAttachProvider("test123", "test-session", "window-0", "poco")
            status = provider.get_status()
        assert status == TerminalStatus.IDLE

    def test_commands_at_end_of_line_idle(self):
        """'commands' at end of line should trigger IDLE status."""
        mock_get_history = MagicMock(return_value="Available commands\n")
        with patch.object(
            __import__('cli_agent_orchestrator.providers.opencode_attach', fromlist=['tmux_client']).tmux_client,
            'get_history',
            mock_get_history
        ):
            provider = OpenCodeAttachProvider("test123", "test-session", "window-0", "poco")
            status = provider.get_status()
        assert status == TerminalStatus.IDLE

    def test_agents_at_end_of_line_idle(self):
        """'agents' at end of line should trigger IDLE status."""
        mock_get_history = MagicMock(return_value="Active agents\n")
        with patch.object(
            __import__('cli_agent_orchestrator.providers.opencode_attach', fromlist=['tmux_client']).tmux_client,
            'get_history',
            mock_get_history
        ):
            provider = OpenCodeAttachProvider("test123", "test-session", "window-0", "poco")
            status = provider.get_status()
        assert status == TerminalStatus.IDLE

    def test_esc_interrupt_processing(self):
        """'esc interrupt' should trigger PROCESSING status."""
        mock_get_history = MagicMock(return_value="esc interrupt")
        with patch.object(
            __import__('cli_agent_orchestrator.providers.opencode_attach', fromlist=['tmux_client']).tmux_client,
            'get_history',
            mock_get_history
        ):
            provider = OpenCodeAttachProvider("test123", "test-session", "window-0", "poco")
            status = provider.get_status()
        assert status == TerminalStatus.PROCESSING

    def test_esc_again_to_interrupt_processing(self):
        """'esc again to interrupt' should trigger PROCESSING status."""
        mock_get_history = MagicMock(return_value="esc again to interrupt")
        with patch.object(
            __import__('cli_agent_orchestrator.providers.opencode_attach', fromlist=['tmux_client']).tmux_client,
            'get_history',
            mock_get_history
        ):
            provider = OpenCodeAttachProvider("test123", "test-session", "window-0", "poco")
            status = provider.get_status()
        assert status == TerminalStatus.PROCESSING

    def test_processing_with_idle_still_returns_processing(self):
        """When both patterns are present, PROCESSING takes precedence."""
        mock_get_history = MagicMock(return_value="Active agents\nesc interrupt")
        with patch.object(
            __import__('cli_agent_orchestrator.providers.opencode_attach', fromlist=['tmux_client']).tmux_client,
            'get_history',
            mock_get_history
        ):
            provider = OpenCodeAttachProvider("test123", "test-session", "window-0", "poco")
            status = provider.get_status()
        assert status == TerminalStatus.PROCESSING