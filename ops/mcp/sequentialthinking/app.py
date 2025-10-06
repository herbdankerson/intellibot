"""Sequential Thinking MCP server backed by FastMCP."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from fastmcp.server import FastMCP
from fastmcp.server.context import Context
from fastmcp.tools.tool import ToolResult, TextContent

LOGGER = logging.getLogger("sequentialthinking")
logging.basicConfig(level=logging.INFO)

SERVER_NAME = "sequential-thinking"

mcp = FastMCP(
    SERVER_NAME,
    instructions=(
        "Assist other agents by structuring deliberate multi-step thinking. "
        "Each call receives the current thought and metadata; respond with JSON summarizing "
        "the thinking state so orchestrators can persist and inspect progress."
    ),
    stateless_http=True,
)


@dataclass
class ThoughtRecord:
    thought: str
    thought_number: int
    total_thoughts: int
    is_revision: bool
    revises_thought: Optional[int]
    branch_from_thought: Optional[int]
    branch_id: Optional[str]
    needs_more_thoughts: Optional[bool]
    next_thought_needed: bool


@dataclass
class SessionState:
    history: List[ThoughtRecord] = field(default_factory=list)
    branches: Dict[str, List[ThoughtRecord]] = field(default_factory=dict)

    def register(self, record: ThoughtRecord) -> None:
        self.history.append(record)
        if record.branch_id:
            self.branches.setdefault(record.branch_id, []).append(record)

    def serialize(self) -> Dict[str, object]:
        return {
            "thoughtHistory": [
                {
                    "thought": item.thought,
                    "thoughtNumber": item.thought_number,
                    "totalThoughts": item.total_thoughts,
                    "isRevision": item.is_revision,
                    "revisesThought": item.revises_thought,
                    "branchFromThought": item.branch_from_thought,
                    "branchId": item.branch_id,
                    "needsMoreThoughts": item.needs_more_thoughts,
                    "nextThoughtNeeded": item.next_thought_needed,
                }
                for item in self.history
            ],
            "branches": {
                branch_id: [
                    {
                        "thought": item.thought,
                        "thoughtNumber": item.thought_number,
                        "totalThoughts": item.total_thoughts,
                        "isRevision": item.is_revision,
                        "revisesThought": item.revises_thought,
                        "branchFromThought": item.branch_from_thought,
                        "branchId": item.branch_id,
                        "needsMoreThoughts": item.needs_more_thoughts,
                        "nextThoughtNeeded": item.next_thought_needed,
                    }
                    for item in items
                ]
                for branch_id, items in self.branches.items()
            },
        }


_STATE: Dict[str, SessionState] = {}


def _get_state(session_id: str) -> SessionState:
    state = _STATE.get(session_id)
    if state is None:
        state = SessionState()
        _STATE[session_id] = state
    return state


@mcp.tool(
    name="sequentialthinking",
    description=(
        "Maintain a chain of deliberate thoughts. Accepts the current thought metadata and returns "
        "a structured summary so orchestrators can decide on the next step."
    ),
)
async def sequentialthinking_tool(
    thought: str,
    nextThoughtNeeded: bool,
    thoughtNumber: int,
    totalThoughts: int,
    context: Context,
    isRevision: Optional[bool] = None,
    revisesThought: Optional[int] = None,
    branchFromThought: Optional[int] = None,
    branchId: Optional[str] = None,
    needsMoreThoughts: Optional[bool] = None,
) -> ToolResult:
    """Record a thought and return session-aware metadata."""

    if not thought:
        raise ValueError("thought must be a non-empty string")

    if thoughtNumber < 1:
        raise ValueError("thoughtNumber must be >= 1")

    if totalThoughts < 1:
        raise ValueError("totalThoughts must be >= 1")

    is_revision = bool(isRevision)
    record = ThoughtRecord(
        thought=thought,
        thought_number=int(thoughtNumber),
        total_thoughts=int(totalThoughts),
        is_revision=is_revision,
        revises_thought=int(revisesThought) if revisesThought is not None else None,
        branch_from_thought=int(branchFromThought) if branchFromThought is not None else None,
        branch_id=branchId,
        needs_more_thoughts=needsMoreThoughts,
        next_thought_needed=bool(nextThoughtNeeded),
    )

    session_id = context.session_id
    state = _get_state(session_id)
    state.register(record)

    payload = {
        "status": "ok",
        "sessionId": session_id,
        "thoughtNumber": record.thought_number,
        "totalThoughts": max(record.total_thoughts, record.thought_number),
        "nextThoughtNeeded": record.next_thought_needed,
        "needsMoreThoughts": record.needs_more_thoughts,
        "historyLength": len(state.history),
        "thought": record.thought,
        "isRevision": record.is_revision,
        "revisesThought": record.revises_thought,
        "branchFromThought": record.branch_from_thought,
        "branchId": record.branch_id,
        "history": state.serialize()["thoughtHistory"],
        "branches": state.serialize()["branches"],
    }

    # Reset session state when no more thoughts are needed.
    if not record.next_thought_needed:
        _STATE.pop(session_id, None)

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return ToolResult(content=[TextContent(type="text", text=text)])


async def main() -> None:
    await mcp.run_sse_async(host="0.0.0.0", port=8080, path="/mcp/")


if __name__ == "__main__":
    asyncio.run(main())
