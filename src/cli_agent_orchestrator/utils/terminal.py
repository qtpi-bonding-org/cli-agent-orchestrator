"""Session utilities for CLI Agent Orchestrator."""

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING

import httpx

from cli_agent_orchestrator.constants import API_BASE_URL, SESSION_PREFIX
from cli_agent_orchestrator.models.terminal import TerminalStatus

if TYPE_CHECKING:
    from cli_agent_orchestrator.clients.tmux import TmuxClient
    from cli_agent_orchestrator.providers.base import BaseProvider

logger = logging.getLogger(__name__)


def generate_session_name() -> str:
    """Generate a unique session name."""
    session_uuid = uuid.uuid4().hex[:8]
    return session_uuid


def generate_terminal_id() -> str:
    """Generate terminal ID without prefix."""
    return uuid.uuid4().hex[:8]


def generate_window_name(agent_profile: str) -> str:
    """Generate window name from agent profile with unique suffix."""
    return f"{agent_profile}-{uuid.uuid4().hex[:4]}"


def wait_for_shell(
    tmux_client: "TmuxClient",
    session_name: str,
    window_name: str,
    timeout: float = 10.0,
    polling_interval: float = 0.5,
) -> bool:
    """Wait for shell to be ready by checking if output is stable (2 consecutive reads are the same and non-empty)."""
    logger.info(f"Waiting for shell to be ready in {session_name}:{window_name}...")
    start_time = time.time()
    previous_output = None

    while time.time() - start_time < timeout:
        output = tmux_client.get_history(session_name, window_name)

        if output and output.strip() and previous_output is not None and output == previous_output:
            logger.info(f"Shell ready")
            return True

        previous_output = output
        time.sleep(polling_interval)

    logger.warning(f"Timeout waiting for shell to be ready")
    return False


def wait_until_status(
    provider_instance: "BaseProvider",
    target_status: TerminalStatus,
    timeout: float = 30.0,
    polling_interval: float = 1.0,
) -> bool:
    """Wait until provider reaches target status or timeout (Synchronous)."""
    start_time = time.time()

    while time.time() - start_time < timeout:
        status = provider_instance.get_status()
        logger.info(f"Waiting for {target_status}, current status: {status}")
        if status == target_status:
            return True
        time.sleep(polling_interval)

    return False


async def async_wait_until_status(
    provider_instance: "BaseProvider",
    target_status: TerminalStatus,
    timeout: float = 30.0,
    polling_interval: float = 1.0,
) -> bool:
    """Wait until provider reaches target status or timeout (Asynchronous)."""
    start_time = time.time()

    while time.time() - start_time < timeout:
        status = provider_instance.get_status()
        logger.info(f"Waiting for {target_status}, current status: {status}")
        if status == target_status:
            return True
        await asyncio.sleep(polling_interval)

    return False


def wait_until_terminal_status(
    terminal_id: str,
    target_status: TerminalStatus,
    timeout: float = 30.0,
    polling_interval: float = 1.0,
) -> bool:
    """Wait until terminal reaches target status using API endpoint (Synchronous)."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = httpx.get(f"{API_BASE_URL}/terminals/{terminal_id}", timeout=10.0)
            if response.status_code == 200:
                terminal_data = response.json()
                if terminal_data["status"] == target_status.value:
                    return True
        except Exception:
            pass
        time.sleep(polling_interval)
    return False


async def async_wait_until_terminal_status(
    terminal_id: str,
    target_status: TerminalStatus,
    timeout: float = 30.0,
    polling_interval: float = 1.0,
) -> bool:
    """Wait until terminal reaches target status using API endpoint (Asynchronous)."""
    start_time = time.time()
    async with httpx.AsyncClient() as client:
        while time.time() - start_time < timeout:
            try:
                response = await client.get(f"{API_BASE_URL}/terminals/{terminal_id}", timeout=10.0)
                if response.status_code == 200:
                    terminal_data = response.json()
                    if terminal_data["status"] == target_status.value:
                        return True
            except Exception:
                pass
            await asyncio.sleep(polling_interval)
    return False
