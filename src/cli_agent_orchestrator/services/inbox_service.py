"""Inbox service with watchdog for automatic message delivery."""

import logging
import re
import subprocess
from pathlib import Path

from watchdog.events import FileModifiedEvent, FileSystemEventHandler

from cli_agent_orchestrator.clients.database import get_pending_messages, update_message_status
from cli_agent_orchestrator.constants import INBOX_SERVICE_TAIL_LINES, TERMINAL_LOG_DIR
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import terminal_service

logger = logging.getLogger(__name__)


def _get_log_tail(terminal_id: str, lines: int = 5) -> str:
    """Get last N lines from terminal log file."""
    log_path = TERMINAL_LOG_DIR / f"{terminal_id}.log"
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), str(log_path)], capture_output=True, text=True, timeout=1
        )
        return result.stdout
    except Exception:
        return ""


def _has_idle_pattern(terminal_id: str) -> bool:
    """Check if log tail contains idle pattern without expensive tmux calls."""
    tail = _get_log_tail(terminal_id)
    if not tail:
        return False

    try:
        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            return False
        idle_pattern = provider.get_idle_pattern_for_log()
        return bool(re.search(idle_pattern, tail))
    except Exception:
        return False


def check_and_send_pending_messages(terminal_id: str) -> bool:
    """Check for pending messages and send if terminal is ready.

    Args:
        terminal_id: Terminal ID to check messages for

    Returns:
        bool: True if a message was sent, False otherwise

    Raises:
        ValueError: If provider not found for terminal
    """
    # Check for pending messages
    messages = get_pending_messages(terminal_id, limit=1)
    if not messages:
        return False

    message = messages[0]

    # Get provider and check status
    provider = provider_manager.get_provider(terminal_id)
    if provider is None:
        raise ValueError(f"Provider not found for terminal {terminal_id}")
    status = provider.get_status(tail_lines=INBOX_SERVICE_TAIL_LINES)

    if status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
        logger.debug(f"Terminal {terminal_id} not ready (status={status})")
        return False

    # Send message
    try:
        terminal_service.send_input(terminal_id, message.message)
        update_message_status(message.id, MessageStatus.DELIVERED)
        logger.info(f"Delivered message {message.id} to terminal {terminal_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send message {message.id} to {terminal_id}: {e}")
        update_message_status(message.id, MessageStatus.FAILED)
        raise


class LogFileHandler(FileSystemEventHandler):
    """Handler for terminal log file changes."""

    def on_modified(self, event):
        """Handle file modification events."""
        if isinstance(event, FileModifiedEvent) and event.src_path.endswith(".log"):
            log_path = Path(event.src_path)
            terminal_id = log_path.stem
            logger.debug(f"Log file modified: {terminal_id}.log")
            self._handle_log_change(terminal_id)

    def _handle_log_change(self, terminal_id: str):
        """Handle log file change and attempt message delivery."""
        try:
            # 1. Check for Auto-Relay (Worker reporting back to Supervisor)
            self._handle_auto_relay(terminal_id)

            # 2. Check for pending messages (Supervisor tasking Worker)
            messages = get_pending_messages(terminal_id, limit=1)
            if not messages:
                logger.debug(f"No pending messages for {terminal_id}, skipping")
                return

            # Fast check: does log tail have idle pattern?
            if not _has_idle_pattern(terminal_id):
                logger.debug(
                    f"Terminal {terminal_id} not idle (no idle pattern in log tail), skipping"
                )
                return

            # Attempt delivery
            check_and_send_pending_messages(terminal_id)

        except Exception as e:
            logger.error(f"Error handling log change for {terminal_id}: {e}")

    def _handle_auto_relay(self, terminal_id: str):
        """Automatically relay COMPLETED results to supervisor if present."""
        try:
            provider = provider_manager.get_provider(terminal_id)
            if not provider:
                return

            status = provider.get_status(tail_lines=INBOX_SERVICE_TAIL_LINES)
            if status != TerminalStatus.COMPLETED:
                return

            # Get metadata to find supervisor (delegating_agent_id)
            metadata = terminal_service.get_terminal(terminal_id)
            supervisor_id = metadata.get("delegating_agent_id")
            
            if not supervisor_id:
                return

            # Check if this task result has already been relayed
            # We use a simple in-memory set to prevent spamming the supervisor
            # until the worker is assigned a new task.
            if not hasattr(self, "_relayed_terminals"):
                self._relayed_terminals = {}
            
            # Key on terminal_id + initial_message hash to know if it's the same task
            current_task_key = f"{terminal_id}:{metadata.get('initial_message', 'none')}"
            if self._relayed_terminals.get(terminal_id) == current_task_key:
                return

            logger.info(f"Auto-Relaying results from {terminal_id} to supervisor {supervisor_id}")
            
            # Extract final answer
            output = terminal_service.get_output(
                terminal_id, mode=terminal_service.OutputMode.LAST
            )
            
            if output and output.strip():
                # Relay results back to supervisor's input (bridge)
                # Success = True if the message was accepted by the supervisor's provider
                terminal_service.send_input(supervisor_id, f"Subagent {terminal_id} results:\n\n{output}")
                
                # Mark as relayed for this task
                self._relayed_terminals[terminal_id] = current_task_key
            else:
                logger.warning(f"Terminal {terminal_id} completed but no output extracted")

        except Exception as e:
            logger.error(f"Auto-relay failed for {terminal_id}: {e}")
