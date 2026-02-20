"""MCP server models."""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class HandoffResult(BaseModel):
    """Result of a handoff or assign operation.

    The _pocketcoder_sys_event field acts as a discriminator so Relay can
    distinguish PocketCoder system events from normal tool output when
    parsing the opaque tool_result content string from OpenCode's SSE stream.
    This field is serialized at the top level of the JSON (not as a wrapper envelope)
    so it doesn't break MCP schema validation.
    """

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    pocketcoder_sys_event: str = Field(
        default="handoff_complete",
        alias="_pocketcoder_sys_event",
        description="System event discriminator for Relay to identify PocketCoder handoff results",
    )
    success: bool = Field(description="Whether the handoff was successful")
    message: str = Field(description="A message describing the result of the handoff")
    output: Optional[str] = Field(None, description="The output from the target agent")
    terminal_id: Optional[str] = Field(None, description="The terminal ID used for the handoff")
    subagent_id: Optional[str] = Field(None, description="The OpenCode session ID of the subagent")
    tmux_window_id: Optional[int] = Field(None, description="The numeric tmux window index")
    agent_profile: Optional[str] = Field(None, description="The agent profile used for the handoff")


class CheckTerminalResult(BaseModel):
    """Result of checking a terminal's status and output."""

    success: bool = Field(description="Whether the status check was successful")
    status: str = Field(description="The current status of the terminal (e.g., IDLE, PROCESSING, COMPLETED, ERROR)")
    message: str = Field(description="A message describing the result of the check")
    output: Optional[str] = Field(None, description="The tailed output from the terminal")
