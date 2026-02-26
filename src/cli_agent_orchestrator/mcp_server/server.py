"""CLI Agent Orchestrator MCP Server implementation."""

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Dict, Optional, Tuple

import requests
from fastmcp import FastMCP, Context
from pydantic import Field

from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import PUBLIC_URL, DEFAULT_PROVIDER
from cli_agent_orchestrator.mcp_server.models import HandoffResult
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.terminal_service import (
    create_terminal,
    get_terminal,
    list_workers as list_workers_service,
)
from cli_agent_orchestrator.utils.terminal import generate_session_name, async_wait_until_terminal_status

logger = logging.getLogger(__name__)

# Environment variable to enable/disable working_directory parameter
ENABLE_WORKING_DIRECTORY = os.getenv("CAO_ENABLE_WORKING_DIRECTORY", "false").lower() == "true"

# Create MCP server
mcp = FastMCP(
    "cao-mcp-server",
    instructions="""
    # CLI Agent Orchestrator MCP Server

    This server provides tools to facilitate terminal delegation within CLI Agent Orchestrator sessions.

    ## Best Practices

    - Use specific agent profiles and providers
    - Provide clear and concise messages
    - Ensure you're running within a CAO terminal (CAO_TERMINAL_ID must be set)
    """,
)


def _resolve_tmux_window_index(tmux_session_name: str, tmux_window_name: str) -> Optional[int]:
    """Resolve the numeric tmux window index by querying tmux directly.

    Window names are generated as '{profile}-{uuid4[:4]}' (e.g. 'analyst-ab12'),
    so we can't parse the index from the name. Instead we query tmux for all
    windows in the session and match by name.
    """
    try:
        windows = tmux_client.get_session_windows(tmux_session_name)
        for w in windows:
            if w["name"] == tmux_window_name:
                return int(w["index"])
    except Exception as e:
        logger.warning(f"Failed to resolve tmux window index for {tmux_session_name}:{tmux_window_name}: {e}")
    return None


def _extract_session_id_from_pane(tmux_session_name: str, tmux_window_name: str) -> Optional[str]:
    """Extract OpenCode sessionID from the JSON event stream in a tmux pane.

    OpenCode emits JSON events with a 'sessionID' field (e.g. 'ses_abc123...').
    This is a process-level value — not a tmux environment variable — so we
    parse it from the pane output rather than using 'tmux show-environment'.

    The pane output may wrap long JSON lines across multiple visual lines,
    so we collapse everything before matching.
    """
    try:
        output = tmux_client.get_history(tmux_session_name, tmux_window_name, tail_lines=200)
        if not output:
            return None
        # Collapse all whitespace (line wraps, newlines) into a single stream
        # so we can match JSON fields that span visual line boundaries
        collapsed = re.sub(r'\s+', '', output)
        # Also strip ANSI escape codes
        collapsed = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', collapsed)
        # Look for sessionID in JSON events
        match = re.search(r'"sessionID"\s*:\s*"(ses_[A-Za-z0-9_]+)"', collapsed)
        if match:
            return match.group(1)
    except Exception as e:
        logger.debug(f"Failed to extract sessionID from pane {tmux_session_name}:{tmux_window_name}: {e}")
    return None


def _create_terminal(
    agent_profile: str, working_directory: Optional[str] = None, session_id: Optional[str] = None, initial_message: Optional[str] = None
) -> Tuple[str, str]:
    """Create a new terminal with the specified agent profile.

    Args:
        agent_profile: Agent profile for the terminal
        working_directory: Optional working directory for the terminal

    Returns:
        Tuple of (terminal_id, provider)

    Raises:
        Exception: If terminal creation fails
    """
    provider = DEFAULT_PROVIDER

    # Get current terminal ID from environment (legacy) or session_id (multi-tenant)
    current_terminal_id = os.environ.get("CAO_TERMINAL_ID")

    if not current_terminal_id and session_id:
        try:
            response = requests.get(f"{PUBLIC_URL}/terminals/by-delegating-agent/{session_id}")
            if response.status_code == 200:
                current_terminal_id = response.json().get("id")
                logger.info(f"Resolved terminal {current_terminal_id} from session {session_id}")
        except Exception as e:
            logger.debug(f"Session lookup failed: {e}")

    if current_terminal_id:
        # Get terminal metadata via API
        response = requests.get(f"{PUBLIC_URL}/terminals/{current_terminal_id}")
        response.raise_for_status()
        terminal_metadata = response.json()

        provider = terminal_metadata["provider"]
        if provider == "opencode-api":
            provider = "opencode"
            logger.info(f"Inheriting from opencode-api: forcing subagent to local 'opencode' provider")

        session_name = terminal_metadata["session_name"]

        # If no working_directory specified, get conductor's current directory
        if working_directory is None:
            try:
                response = requests.get(
                    f"{PUBLIC_URL}/terminals/{current_terminal_id}/working-directory"
                )
                if response.status_code == 200:
                    working_directory = response.json().get("working_directory")
                    logger.info(f"Inherited working directory from conductor: {working_directory}")
                else:
                    logger.warning(
                        f"Failed to get conductor's working directory (status {response.status_code}), "
                        "will use server default"
                    )
            except Exception as e:
                logger.warning(
                    f"Error fetching conductor's working directory: {e}, will use server default"
                )

        # Create new terminal in existing session - always pass working_directory
        params = {
            "provider": provider, 
            "agent_profile": agent_profile,
            "delegating_agent_id": current_terminal_id, # Set for auto-relay
            "initial_message": initial_message, # Pass initial task for context
        }
        if working_directory:
            params["working_directory"] = working_directory
    if session_id:
        session_name = session_id
        # Check if session already exists in CAO
        try:
            resp = requests.get(f"{PUBLIC_URL}/sessions/{session_name}")
            if resp.status_code == 200:
                # Session exists, create terminal in it
                params = {"provider": provider, "agent_profile": agent_profile, "delegating_agent_id": session_id}
                if working_directory:
                    params["working_directory"] = working_directory
                
                json_data = {"initial_message": initial_message} if initial_message else None
                response = requests.post(f"{PUBLIC_URL}/sessions/{session_name}/terminals", params=params, json=json_data)
                response.raise_for_status()
                terminal = response.json()
                return terminal["id"], provider
        except Exception as e:
            logger.debug(f"Session {session_name} not found or error: {e}")

        # If not returned, create new session with this name
        params = {
            "provider": provider,
            "agent_profile": agent_profile,
            "session_name": session_name,
            "delegating_agent_id": session_id,
        }
        if working_directory:
            params["working_directory"] = working_directory

        json_data = {"initial_message": initial_message} if initial_message else None
        response = requests.post(f"{PUBLIC_URL}/sessions", params=params, json=json_data)
        response.raise_for_status()
        terminal = response.json()
    elif current_terminal_id:
        # Get terminal metadata via API
        response = requests.get(f"{PUBLIC_URL}/terminals/{current_terminal_id}")
        response.raise_for_status()
        terminal_metadata = response.json()

        provider = terminal_metadata["provider"]
        session_name = terminal_metadata["session_name"]

        # If no working_directory specified, get conductor's current directory
        if working_directory is None:
            try:
                response = requests.get(
                    f"{PUBLIC_URL}/terminals/{current_terminal_id}/working-directory"
                )
                if response.status_code == 200:
                    working_directory = response.json().get("working_directory")
                    logger.info(f"Inherited working directory from conductor: {working_directory}")
            except Exception as e:
                logger.warning(
                    f"Error fetching conductor's working directory: {e}, will use server default"
                )

        # Create new terminal in existing session
        params = {"provider": provider, "agent_profile": agent_profile}
        if working_directory:
            params["working_directory"] = working_directory

        json_data = {"initial_message": initial_message} if initial_message else None
        response = requests.post(f"{PUBLIC_URL}/sessions/{session_name}/terminals", params=params, json=json_data)
        response.raise_for_status()
        terminal = response.json()
    else:
        # Create new session with unique name
        session_name = generate_session_name()
        params = {
            "provider": provider,
            "agent_profile": agent_profile,
            "session_name": session_name,
        }
        if working_directory:
            params["working_directory"] = working_directory

        json_data = {"initial_message": initial_message} if initial_message else None
        response = requests.post(f"{PUBLIC_URL}/sessions", params=params, json=json_data)
        response.raise_for_status()
        terminal = response.json()

    return terminal["id"], provider


def _send_direct_input(terminal_id: str, message: str) -> None:
    """Send input directly to a terminal (bypasses inbox).

    Args:
        terminal_id: Terminal ID
        message: Message to send

    Raises:
        Exception: If sending fails
    """
    response = requests.post(
        f"{PUBLIC_URL}/terminals/{terminal_id}/input", json={"message": message}
    )
    response.raise_for_status()


def _send_to_inbox(receiver_id: str, message: str, sender_id: Optional[str] = None) -> Dict[str, Any]:
    """Send message to another terminal's inbox (queued delivery when IDLE).

    Args:
        receiver_id: Target terminal ID
        message: Message content
        sender_id: Optional explicit sender ID

    Returns:
        Dict with message details

    Raises:
        ValueError: If sender identity cannot be determined
        Exception: If API call fails
    """
    if not sender_id:
        sender_id = os.getenv("CAO_TERMINAL_ID")
    
    if not sender_id:
        raise ValueError("Sender identity not found (no CAO_TERMINAL_ID and no explicit sender)")

    response = requests.post(
        f"{PUBLIC_URL}/terminals/{receiver_id}/inbox/messages",
        params={"sender_id": sender_id, "message": message},
    )
    response.raise_for_status()
    return response.json()


def _get_session_id(ctx: Context) -> Optional[str]:
    """Extract session_id from FastMCP context (query params)."""
    try:
        if hasattr(ctx, "request"):
            return ctx.request.query_params.get("session_id")
    except:
        pass
    return None


async def _resolve_sender_id(session_id: Optional[str]) -> Optional[str]:
    """Resolve the terminal ID of the sender based on session_id."""
    if not session_id:
        return os.getenv("CAO_TERMINAL_ID")
    
    try:
        response = requests.get(f"{PUBLIC_URL}/terminals/by-delegating-agent/{session_id}")
        if response.status_code == 200:
            return response.json().get("id")
    except:
        pass
    return os.getenv("CAO_TERMINAL_ID")


# Implementation functions
async def _handoff_impl(
    agent_profile: str, message: str, timeout: int = 600, working_directory: Optional[str] = None, session_id: Optional[str] = None
) -> HandoffResult:
    """Implementation of handoff logic."""
    start_time = time.time()

    # Initialize enriched fields
    subagent_id: Optional[str] = None
    tmux_window_id: Optional[int] = None
    enriched_agent_profile: Optional[str] = None
    tmux_session_name: Optional[str] = None
    tmux_window_name: Optional[str] = None

    try:
        print(f"🎬 [CAO-MCP] Starting Handoff: profile={agent_profile}, directory={working_directory}")
        # Create terminal
        terminal_id, provider = _create_terminal(agent_profile, working_directory, session_id, initial_message=message)
        print(f"🆕 [CAO-MCP] Created terminal {terminal_id} ({provider})")

        # Get terminal metadata for enriched fields
        terminal_metadata = get_terminal_metadata(terminal_id)
        if terminal_metadata:
            tmux_session_name = terminal_metadata.get("tmux_session")
            tmux_window_name = terminal_metadata.get("tmux_window")
            enriched_agent_profile = terminal_metadata.get("agent_profile")
            # Query actual tmux window index (window names are '{profile}-{uuid}', not 'window-N')
            if tmux_session_name and tmux_window_name:
                tmux_window_id = _resolve_tmux_window_index(tmux_session_name, tmux_window_name)
                print(f"📐 [CAO-MCP] Resolved tmux_window_id={tmux_window_id} for {tmux_window_name}")

        # Wait for terminal to be IDLE before sending message
        if not await async_wait_until_terminal_status(terminal_id, TerminalStatus.IDLE, timeout=30.0):
            return HandoffResult(
                success=False,
                message=f"Terminal {terminal_id} did not reach IDLE status within 30 seconds",
                output=None,
                terminal_id=terminal_id,
                subagent_id=subagent_id,
                tmux_window_id=tmux_window_id,
                agent_profile=enriched_agent_profile,
            )

        await asyncio.sleep(2)  # wait another 2s

        # Send message to terminal
        _send_direct_input(terminal_id, message)

        # Monitor until completion with timeout.
        # While waiting, also poll for subagent_id from the pane's JSON event stream.
        # OpenCode emits {"sessionID": "ses_..."} once it starts — we capture it
        # during the wait rather than as a separate phase, since OpenCode startup
        # can take longer than a fixed poll timeout.
        if tmux_session_name and tmux_window_name:
            print(f"🔍 [CAO-MCP] Will capture subagent_id during execution wait")

        completion_poll_start = time.time()
        while True:
            elapsed = time.time() - completion_poll_start
            if elapsed >= timeout:
                return HandoffResult(
                    success=False,
                    message=f"Handoff timed out after {timeout} seconds",
                    output=None,
                    terminal_id=terminal_id,
                    subagent_id=subagent_id,
                    tmux_window_id=tmux_window_id,
                    agent_profile=enriched_agent_profile,
                )

            # Try to capture subagent_id if we haven't yet
            if not subagent_id and tmux_session_name and tmux_window_name:
                subagent_id = _extract_session_id_from_pane(tmux_session_name, tmux_window_name)
                if subagent_id:
                    print(f"✅ [CAO-MCP] Captured subagent_id: {subagent_id}")

            # Check if terminal completed
            provider_instance = provider_manager.get_provider(terminal_id)
            if provider_instance:
                status = provider_instance.get_status()
                if status == TerminalStatus.COMPLETED:
                    break
                if status == TerminalStatus.ERROR:
                    break

            await asyncio.sleep(1.0)

        # One final attempt to capture subagent_id after completion
        if not subagent_id and tmux_session_name and tmux_window_name:
            subagent_id = _extract_session_id_from_pane(tmux_session_name, tmux_window_name)
            if subagent_id:
                print(f"✅ [CAO-MCP] Captured subagent_id (post-completion): {subagent_id}")
            else:
                logger.warning(f"sessionID not found in pane output for terminal {terminal_id}")

        # Get the response
        response = requests.get(
            f"{PUBLIC_URL}/terminals/{terminal_id}/output", params={"mode": "last"}
        )
        response.raise_for_status()
        output_data = response.json()
        output = output_data["output"]

        # Send provider-specific exit command to cleanup terminal
        response = requests.post(f"{PUBLIC_URL}/terminals/{terminal_id}/exit")
        response.raise_for_status()

        # No nudge for sync handoff — MCP tool response is the canonical back-path.
        # The nudge is only used for async (assign) workflows.

        execution_time = time.time() - start_time

        print(f"✅ [CAO-MCP] Handoff success for {agent_profile}. Result captured.")
        return HandoffResult(
            success=True,
            message=f"Successfully handed off to {agent_profile} ({provider}) in {execution_time:.2f}s",
            output=output,
            terminal_id=terminal_id,
            subagent_id=subagent_id,
            tmux_window_id=tmux_window_id,
            agent_profile=enriched_agent_profile,
        )

    except Exception as e:
        print(f"❌ [CAO-MCP] Handoff Exception: {str(e)}")
        return HandoffResult(
            success=False, message=f"Handoff failed: {str(e)}", output=None, terminal_id=None,
            subagent_id=subagent_id, tmux_window_id=tmux_window_id, agent_profile=enriched_agent_profile,
        )


# Conditional tool registration based on environment variable
if ENABLE_WORKING_DIRECTORY:

    @mcp.tool()
    async def handoff(
        agent_profile: str = Field(
            description='The agent profile to hand off to (e.g., "developer", "analyst")'
        ),
        message: str = Field(description="The message/task to send to the target agent"),
        timeout: int = Field(
            default=600,
            description="Maximum time to wait for the agent to complete the task (in seconds)",
            ge=1,
            le=3600,
        ),
        working_directory: Optional[str] = Field(
            default=None,
            description='Optional working directory where the agent should execute (e.g., "/path/to/workspace/src/Package")',
        ),
        ctx: Context = None,
    ) -> HandoffResult:
        """Hand off a task to another agent via CAO terminal and wait for completion.

        This tool allows handing off tasks to other agents by creating a new terminal
        in the same session. It sends the message, waits for completion, and captures the output.

        ## Usage

        Use this tool to hand off tasks to another agent and wait for the results.
        The tool will:
        1. Create a new terminal with the specified agent profile and provider
        2. Set the working directory for the terminal (defaults to supervisor's cwd)
        3. Send the message to the terminal
        4. Monitor until completion
        5. Return the agent's response
        6. Clean up the terminal with /exit

        ## Working Directory

        - By default, agents start in the supervisor's current working directory
        - You can specify a custom directory via working_directory parameter
        - Directory must exist and be accessible

        ## Requirements

        - Must be called from within a CAO terminal (CAO_TERMINAL_ID environment variable)
        - Target session must exist and be accessible
        - If working_directory is provided, it must exist and be accessible

        Args:
            agent_profile: The agent profile for the new terminal
            message: The task/message to send
            timeout: Maximum wait time in seconds
            working_directory: Optional directory path where agent should execute

        Returns:
            HandoffResult with success status, message, and agent output
        """
        session_id = _get_session_id(ctx) if ctx else None
        result = await _handoff_impl(agent_profile, message, timeout, working_directory, session_id)
        # Return HandoffResult directly — MCP response is the canonical back-path for sync handoff.
        # Relay detects handoff results by checking for terminal_id + success fields in tool_result content.
        return result

else:

    @mcp.tool()
    async def handoff(
        agent_profile: str = Field(
            description='The agent profile to hand off to (e.g., "developer", "analyst")'
        ),
        message: str = Field(description="The message/task to send to the target agent"),
        timeout: int = Field(
            default=600,
            description="Maximum time to wait for the agent to complete the task (in seconds)",
            ge=1,
            le=3600,
        ),
        ctx: Context = None,
    ) -> HandoffResult:
        """Hand off a task to another agent via CAO terminal and wait for completion.

        This tool allows handing off tasks to other agents by creating a new terminal
        in the same session. It sends the message, waits for completion, and captures the output.

        ## Usage

        Use this tool to hand off tasks to another agent and wait for the results.
        The tool will:
        1. Create a new terminal with the specified agent profile and provider
        2. Send the message to the terminal (starts in supervisor's current directory)
        3. Monitor until completion
        4. Return the agent's response
        5. Clean up the terminal with /exit

        ## Requirements

        - Must be called from within a CAO terminal (CAO_TERMINAL_ID environment variable)
        - Target session must exist and be accessible

        Args:
            agent_profile: The agent profile for the new terminal
            message: The task/message to send
            timeout: Maximum wait time in seconds

        Returns:
            HandoffResult with success status, message, and agent output
        """
        session_id = _get_session_id(ctx) if ctx else None
        result = await _handoff_impl(agent_profile, message, timeout, None, session_id)
        # Return HandoffResult directly — MCP response is the canonical back-path for sync handoff.
        return result


# Implementation function for assign
def _assign_impl(
    agent_profile: str, message: str, working_directory: Optional[str] = None, session_id: Optional[str] = None
) -> HandoffResult:
    """Implementation of assign logic."""
    # Initialize enriched fields (no wait loop, so no subagent_id capture)
    subagent_id: Optional[str] = ""
    tmux_window_id: Optional[int] = None
    enriched_agent_profile: Optional[str] = None
    tmux_session_name: Optional[str] = None
    tmux_window_name: Optional[str] = None

    try:
        # Create terminal
        terminal_id, provider = _create_terminal(agent_profile, working_directory, session_id, initial_message=message)
        
        # Get terminal metadata for enriched fields
        terminal_metadata = get_terminal_metadata(terminal_id)
        if terminal_metadata:
            tmux_session_name = terminal_metadata.get("tmux_session")
            tmux_window_name = terminal_metadata.get("tmux_window")
            enriched_agent_profile = terminal_metadata.get("agent_profile")
            # Query actual tmux window index (window names are '{profile}-{uuid}', not 'window-N')
            if tmux_session_name and tmux_window_name:
                tmux_window_id = _resolve_tmux_window_index(tmux_session_name, tmux_window_name)

        # Send message immediately
        _send_direct_input(terminal_id, message)

        # Wait a few seconds to capture subagent_id (OpenCode session ID)
        # This is critical for the Relay to map the subagent back to the chat.
        if tmux_session_name and tmux_window_name:
            print(f"🔍 [CAO-MCP] Polling for subagent_id for {terminal_id}...")
            poll_start = time.time()
            while time.time() - poll_start < 5.0:
                subagent_id = _extract_session_id_from_pane(tmux_session_name, tmux_window_name)
                if subagent_id:
                    print(f"✅ [CAO-MCP] Captured subagent_id: {subagent_id}")
                    break
                time.sleep(0.5)

        print(f"✅ [CAO-MCP] Assign success: terminal_id={terminal_id}")
        return HandoffResult(
            success=True,
            message=f"Task assigned to {agent_profile} (terminal: {terminal_id})",
            terminal_id=terminal_id,
            subagent_id=subagent_id,
            tmux_window_id=tmux_window_id,
            agent_profile=enriched_agent_profile,
        )

    except Exception as e:
        print(f"❌ [CAO-MCP] Assign Exception: {str(e)}")
        return HandoffResult(
            success=False,
            message=f"Assignment failed: {str(e)}",
            terminal_id=None,
            subagent_id=subagent_id,
            tmux_window_id=tmux_window_id,
            agent_profile=enriched_agent_profile,
        )


def _check_terminal_impl(terminal_id: str, tail_lines: int) -> "CheckTerminalResult":
    from cli_agent_orchestrator.mcp_server.models import CheckTerminalResult
    try:
        # Fetch status
        resp_status = requests.get(f"{PUBLIC_URL}/terminals/{terminal_id}")
        resp_status.raise_for_status()
        status_data = resp_status.json()
        current_status = status_data.get("status", "UNKNOWN")

        # Fetch output history
        resp_output = requests.get(f"{PUBLIC_URL}/terminals/{terminal_id}/output", params={"mode": "tail", "tail_lines": tail_lines})
        resp_output.raise_for_status()
        output_data = resp_output.json()
        
        return CheckTerminalResult(
            success=True,
            status=current_status,
            message=f"Successfully fetched terminal {terminal_id} status",
            output=output_data.get("output", "")
        )
    except Exception as e:
        logger.error(f"Failed to check terminal {terminal_id}: {e}")
        return CheckTerminalResult(
            success=False,
            status="ERROR",
            message=f"Failed to check terminal: {str(e)}",
            output=None
        )


@mcp.tool()
async def check_terminal(
    terminal_id: str = Field(description="The 8-character terminal ID to check (e.g., from a previous assign)"),
    tail_lines: int = Field(default=100, description="Number of recent terminal lines to capture", ge=1, le=1000),
    ctx: Context = None,
):
    """Check the status and tail the logs of a background terminal.

    Use this tool to monitor the progress of a task you previously assigned to another agent.
    You will receive the terminal's execution state (IDLE, PROCESSING, COMPLETED, ERROR) 
    and the most recent lines of output from its terminal history.

    Args:
        terminal_id: Terminal ID to check
        tail_lines: How many recent lines to fetch

    Returns:
        CheckTerminalResult with status and output payload.
    """
    from cli_agent_orchestrator.mcp_server.models import CheckTerminalResult
    result = await asyncio.to_thread(_check_terminal_impl, terminal_id, tail_lines)
    return result


# Conditional tool registration for assign
if ENABLE_WORKING_DIRECTORY:

    @mcp.tool()
    async def assign(
        agent_profile: str = Field(
            description='The agent profile for the worker agent (e.g., "developer", "analyst")'
        ),
        message: str = Field(
            description="The task message to send. Include callback instructions for the worker to send results back."
        ),
        working_directory: Optional[str] = Field(
            default=None, description="Optional working directory where the agent should execute"
        ),
        ctx: Context = None,
    ) -> HandoffResult:
        """Assigns a task to another agent without blocking.

        In the message to the worker agent include instruction to send results back via send_message tool.
        **IMPORTANT**: The terminal id of each agent is available in environment variable CAO_TERMINAL_ID.
        When assigning, first find out your own CAO_TERMINAL_ID value, then include the terminal_id value in the message to the worker agent to allow callback.
        Example message: "Analyze the logs. When done, send results back to terminal ee3f93b3 using send_message tool."

        ## Working Directory

        - By default, agents start in the supervisor's current working directory
        - You can specify a custom directory via working_directory parameter
        - Directory must exist and be accessible

        Args:
            agent_profile: Agent profile for the worker terminal
            message: Task message (include callback instructions)
            working_directory: Optional directory path where agent should execute

        Returns:
            HandoffResult with success status, worker terminal_id, and message
        """
        session_id = _get_session_id(ctx) if ctx else None
        result = await asyncio.to_thread(_assign_impl, agent_profile, message, working_directory, session_id)
        # Return HandoffResult directly for consistency with handoff.
        # Relay detects by checking for terminal_id + success fields.
        return result

else:

    @mcp.tool()
    async def assign(
        agent_profile: str = Field(
            description='The agent profile for the worker agent (e.g., "developer", "analyst")'
        ),
        message: str = Field(
            description="The task message to send. Include callback instructions for the worker to send results back."
        ),
        ctx: Context = None,
    ) -> HandoffResult:
        """Assigns a task to another agent without blocking.

        In the message to the worker agent include instruction to send results back via send_message tool.
        **IMPORTANT**: The terminal id of each agent is available in environment variable CAO_TERMINAL_ID.
        When assigning, first find out your own CAO_TERMINAL_ID value, then include the terminal_id value in the message to the worker agent to allow callback.
        Example message: "Analyze the logs. When done, send results back to terminal ee3f93b3 using send_message tool."

        Args:
            agent_profile: Agent profile for the worker terminal
            message: Task message (include callback instructions)

        Returns:
            HandoffResult with success status, worker terminal_id, and message
        """
        session_id = _get_session_id(ctx) if ctx else None
        result = await asyncio.to_thread(_assign_impl, agent_profile, message, None, session_id)
        # Return HandoffResult directly for consistency with handoff.
        return result


@mcp.tool()
async def send_message(
    receiver_id: str = Field(description="Target terminal ID to send message to"),
    message: str = Field(description="Message content to send"),
    ctx: Context = None,
) -> Dict[str, Any]:
    """Send a message to another terminal's inbox.

    The message will be delivered when the destination terminal is IDLE.
    Messages are delivered in order (oldest first).

    Args:
        receiver_id: Terminal ID of the receiver
        message: Message content to send

    Returns:
        Dict with success status and message details
    """
    print(f"🎬 [CAO-MCP] Tool Call: send_message(receiver_id={receiver_id}, message_len={len(message)})")
    session_id = _get_session_id(ctx) if ctx else None
    sender_id = await _resolve_sender_id(session_id)
    try:
        print(f"📬 [CAO-MCP] Sending message to {receiver_id}...")
        res = _send_to_inbox(receiver_id, message, sender_id=sender_id)
        print(f"✅ [CAO-MCP] Message sent to {receiver_id}")

        return res
    except Exception as e:
        print(f"❌ [CAO-MCP] send_message Exception: {str(e)}")
        return {"success": False, "error": str(e)}


@mcp.tool()
async def check_inbox(
    terminal_id: Optional[str] = Field(
        default=None,
        description="Terminal ID to check inbox for. If not provided, checks your own inbox (CAO_TERMINAL_ID).",
    ),
    limit: int = Field(default=10, description="Maximum number of messages to retrieve"),
    ctx: Context = None,
) -> Dict[str, Any]:
    """Check inbox for messages from subagents or other terminals.

    Use this tool when you receive a notification that a subagent has sent you a message,
    or to poll for async task results.

    Args:
        terminal_id: Terminal ID to check (defaults to your own)
        limit: Max messages to return

    Returns:
        Dict with messages list and count
    """
    target_id = terminal_id or os.getenv("CAO_TERMINAL_ID")
    if not target_id:
        return {"success": False, "error": "No terminal_id provided and CAO_TERMINAL_ID not set"}

    print(f"🎬 [CAO-MCP] Tool Call: check_inbox(terminal_id={target_id}, limit={limit})")
    try:
        response = requests.get(
            f"{PUBLIC_URL}/terminals/{target_id}/inbox/messages",
            params={"limit": limit},
            timeout=5.0,
        )
        response.raise_for_status()
        messages = response.json()
        print(f"📬 [CAO-MCP] Found {len(messages)} inbox messages for {target_id}")
        return {"success": True, "terminal_id": target_id, "messages": messages, "count": len(messages)}
    except Exception as e:
        print(f"❌ [CAO-MCP] check_inbox Exception: {str(e)}")
        return {"success": False, "error": str(e)}


@mcp.tool()
async def list_workers(ctx: Context = None) -> Dict[str, Any]:
    """List active worker agents for the current session.

    Use this tool to discover background subagents, check their status, 
    and see their initial task assignments (initial_message).

    Returns:
        Dict with success status and list of workers
    """
    print(f"🎬 [CAO-MCP] Tool Call: list_workers")
    session_id = _get_session_id(ctx)
    if not session_id:
        # Fallback: recover session from our own terminal ID if we are a CAO terminal
        current_id = os.environ.get("CAO_TERMINAL_ID")
        if current_id:
            try:
                term = get_terminal(current_id)
                session_id = term.get("session_name")
            except:
                pass

    if not session_id:
        return {"success": False, "error": "Could not determine session_id for worker discovery"}

    try:
        workers = list_workers_service(session_id)
        return {"success": True, "workers": workers}
    except Exception as e:
        return {"success": False, "error": str(e)}


# --- UNIFIED CAO TOOLS (Discover/Request/Done) ---

@mcp.tool()
async def cao_mcp_catalog(query: Optional[str] = None) -> str:
    """Browse or search the Docker MCP Catalog to discover available MCP servers."""
    try:
        # Use direct docker binary to bypass shell bridge noise
        process = subprocess.run(
            ["docker", "mcp", "catalog", "show", "docker-mcp", "--format", "json"],
            capture_output=True, text=True, timeout=10
        )
        stdout = process.stdout.strip()

        if not stdout:
            return "The MCP catalog is empty or unreachable."

        catalog = json.loads(stdout)
        registry = catalog.get("registry", catalog)
        servers = list(registry.items())

        if query:
            q = query.lower()
            filtered = [(name, entry) for name, entry in servers 
                       if q in name.lower() or (entry.get("description") and q in entry["description"].lower())]

            if not filtered:
                return f"No MCP servers found matching '{query}'."

            output = f"### Matching MCP Servers ({len(filtered)})\n\n"
            for name, entry in filtered:
                output += f"- **{name}**: {entry.get('description', 'No description')}\n"
            return output
        else:
            output = f"### All Available MCP Servers ({len(servers)})\n\n"
            for name, entry in servers:
                output += f"- **{name}**: {entry.get('description', 'No description')}\n"
            return output
    except Exception as e:
        logger.error(f"Error browsing MCP catalog: {e}")
        return f"Failed to browse MCP catalog: {e}"


@mcp.tool()
async def cao_mcp_status() -> str:
    """Check currently enabled MCP servers in the gateway (live config)."""
    try:
        # The file is mounted at fixed path in sandbox Dockerfile
        with open("/mcp_config/docker-mcp.yaml", "r") as f:
            config = f.read()
        return f"Currently enabled MCP servers:\n\n```yaml\n{config}\n```"
    except Exception:
        return "No MCP servers are currently enabled (catalog config not found in /mcp_config)."


@mcp.tool()
async def cao_mcp_request(server_name: str, reason: str, ctx: Context = None) -> str:
    """Research and request a new MCP server to be enabled via PocketBase."""
    pb_url = os.getenv("POCKETBASE_URL", "http://pocketbase:8090")
    email = os.getenv("AGENT_EMAIL")
    password = os.getenv("AGENT_PASSWORD")
    session_id = _get_session_id(ctx) if ctx else "unknown"

    if not email or not password:
        return "Error: AGENT_EMAIL/AGENT_PASSWORD not set. Cannot authenticate with PocketBase."

    try:
        # 1. Authenticate with PocketBase
        auth_resp = requests.post(f"{pb_url}/api/collections/users/auth-with-password", 
                                 json={"identity": email, "password": password}, timeout=10)
        auth_resp.raise_for_status()
        token = auth_resp.json()["token"]

        # 2. Auto-Research Catalog
        image = ""
        config_schema = {}
        try:
            process = subprocess.run(
                ["docker", "mcp", "catalog", "show", "docker-mcp", "--format", "json"],
                capture_output=True, text=True, timeout=10
            )
            stdout = process.stdout.strip()
            if stdout:
                catalog = json.loads(stdout)
                registry = catalog.get("registry", catalog)
                entry = registry.get(server_name.lower()) or registry.get(server_name)
                
                if entry:
                    image = entry.get("image", "")
                    # Extract required secrets/envs
                    for s in entry.get("secrets", []):
                        if s.get("env"): config_schema[s["env"]] = f"Secret: {s.get('name', s['env'])}"
                    for e in entry.get("env", []):
                        if e.get("name"): config_schema[e["name"]] = e.get("value", f"Env: {e['name']}")
        except:
            pass # Research is optional best-effort

        # 3. Submit Request
        req_resp = requests.post(
            f"{pb_url}/api/pocketcoder/mcp_request",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "server_name": server_name,
                "reason": reason,
                "session_id": session_id,
                "image": image,
                "config_schema": config_schema
            },
            timeout=10
        )
        req_resp.raise_for_status()
        data = req_resp.json()
        
        status = data.get("status", "submitted")
        return f"✅ MCP server '{server_name}' request submitted (ID: {data.get('id')}, status: {status}). Waiting for user dashboard approval."
    except Exception as e:
        logger.error(f"MCP request failed: {e}")
        return f"❌ Failed to submit MCP request: {e}"


@mcp.tool()
async def cao_mcp_inspect(server_name: str, mode: str = "all") -> str:
    """Inspect an MCP server's tools, configuration requirements, and README.
    
    Args:
        server_name: The name of the MCP server to inspect (e.g., 'n8n', 'mysql')
        mode: Filter what information to return ('all', 'tools', 'readme', 'config')
    """
    try:
        process = subprocess.run(
            ["docker", "mcp", "catalog", "show", "docker-mcp", "--format", "json"],
            capture_output=True, text=True, timeout=10
        )
        stdout = process.stdout.strip()
        if not stdout:
            return "Failed to retrieve catalog information."

        catalog = json.loads(stdout)
        registry = catalog.get("registry", catalog)
        requested_name = server_name.lower()

        # Case-insensitive matching
        entry_key = next((k for k in registry if k.lower() == requested_name), None)
        if not entry_key:
            return f"MCP server '{server_name}' not found in catalog."

        data = registry[entry_key]
        output = f"### MCP Server: {entry_key}\n\n"

        if mode in ("all", "readme") and data.get("readme"):
            output += f"#### README\n{data['readme']}\n\n"

        if mode in ("all", "tools") and isinstance(data.get("tools"), list):
            output += f"#### Tools ({len(data['tools'])})\n"
            for t in data["tools"]:
                output += f"- **{t.get('name')}**: {t.get('description', 'No description')}\n"
                for arg in t.get("arguments", []):
                    output += f"  - *{arg.get('name')}* ({arg.get('type')}): {arg.get('desc', 'No description')}\n"
            output += "\n"

        if mode in ("all", "config"):
            config_schema = {}
            # Extract secrets/envs from legacy/v2/v3 schemas
            for s in data.get("secrets", []):
                if s.get("env"): config_schema[s["env"]] = f"Secret: {s.get('name', s['env'])}"
            for e in data.get("env", []):
                if e.get("name"): config_schema[e["name"]] = e.get("value", f"Env: {e['name']}")
            for c in data.get("config", []):
                if isinstance(c.get("properties"), dict):
                    for prop, details in c["properties"].items():
                        config_schema[prop] = details.get("description", f"Configuration: {prop}")

            if config_schema:
                output += "#### Configuration Requirements\n"
                for key, desc in config_schema.items():
                    output += f"- **{key}**: {desc}\n"
                output += "\n"

        return output
    except Exception as e:
        logger.error(f"Error inspecting MCP server '{server_name}': {e}")
        return f"Failed to inspect MCP server '{server_name}': {e}"


@mcp.tool()
async def cao_done(message: str, ctx: Context = None) -> str:
    """Explicitly finish current subagent task and send final results back to Poco."""
    current_id = os.environ.get("CAO_TERMINAL_ID")
    if not current_id:
        return "Error: CAO_TERMINAL_ID not set. This terminal is not being tracked by CAO."

    try:
        # Find who delegated to us
        metadata = requests.get(f"{PUBLIC_URL}/terminals/{current_id}").json()
        supervisor_id = metadata.get("delegating_agent_id")

        if not supervisor_id:
            # Fallback strategy for session-mapped terminals
            session_id = _get_session_id(ctx) if ctx else None
            if session_id:
                resp = requests.get(f"{PUBLIC_URL}/terminals/by-delegating-agent/{session_id}")
                if resp.status_code == 200:
                    supervisor_id = resp.json().get("id")

        if not supervisor_id:
            return "Error: Could not identify your supervisor terminal (Poco). I don't know who to send results to."

        # Send the results
        requests.post(f"{PUBLIC_URL}/terminals/{supervisor_id}/input", params={"message": message})
        
        return f"✅ Results successfully relayed to supervisor terminal {supervisor_id}. You can now exit."
    except Exception as e:
        logger.error(f"cao_done failed: {e}")
        return f"❌ Failed to relay results: {e}"


def main():
    """Main entry point for the MCP server."""
    import os
    
    transport = os.getenv("CAO_MCP_TRANSPORT", "stdio")
    port = int(os.getenv("CAO_MCP_PORT", "9888"))
    
    print(f"🔍 [CAO-MCP] Initializing with transport: {transport}")
    print(f"🔍 [CAO-MCP] Port: {port}")
    print(f"🔍 [CAO-MCP] PID: {os.getpid()}")
    
    if transport == "sse":
        print(f"🚀 [CAO-MCP] Starting SSE Server on 0.0.0.0:{port}")
        print(f"📡 [CAO-MCP] Endpoint will be: http://0.0.0.0:{port}/sse")
        mcp.run(
            transport="sse",
            port=port,
            host="0.0.0.0",
        )
    elif transport == "http":
        print(f"🚀 [CAO-MCP] Starting HTTP Server on 0.0.0.0:{port}")
        mcp.run(transport="http", port=port, host="0.0.0.0")
    else:
        print(f"📟 [CAO-MCP] Starting STDIO Server")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    print("🎬 [CAO-MCP] Process starting...")
    main()
