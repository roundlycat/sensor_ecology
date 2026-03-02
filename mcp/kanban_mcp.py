"""
kanban_mcp.py — MCP server for the digital kanban board
Wraps the PostgreSQL schema/functions for Claude Code and other agents.
"""

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

# ─── Config ───────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://sean@localhost/sensor_ecology"
)

# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def app_lifespan():
    pool = await asyncpg.create_pool(DATABASE_URL)
    yield {"pool": pool}
    await pool.close()

mcp = FastMCP("kanban_mcp", lifespan=app_lifespan)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _json(rows) -> str:
    """Serialize asyncpg Records to JSON."""
    return json.dumps([dict(r) for r in rows], default=str, indent=2)

def _row(record) -> str:
    return json.dumps(dict(record), default=str, indent=2)

# ─── Input models ─────────────────────────────────────────────────────────────

class BoardId(BaseModel):
    model_config = ConfigDict(extra="forbid")
    board_id: str = Field(..., description="UUID of the board")

class CreateCardInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    board_id:    str           = Field(..., description="UUID of the board")
    column_id:   str           = Field(..., description="UUID of the target column")
    title:       str           = Field(..., description="Card title", min_length=1, max_length=200)
    description: Optional[str] = Field(None, description="Card description")
    card_type:   str           = Field("task", description="task | sensor_alert | agent_action | observation")
    priority:    str           = Field("medium", description="low | medium | high | critical")
    assigned_to: str           = Field("sean", description="sean | claude_code | copilot | agent | unassigned")
    related_node: Optional[str] = Field(None, description="Sensor node name if relevant")
    actor:       str           = Field("claude_code", description="Who is creating this card")

class MoveCardInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id:       str           = Field(..., description="UUID of the card to move")
    to_column_id:  str           = Field(..., description="UUID of the destination column")
    actor:         str           = Field("claude_code", description="Who is moving the card")
    note:          Optional[str] = Field(None, description="Optional note about why card was moved")

class HandoffNoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: str = Field(..., description="UUID of the card")
    author:  str = Field(..., description="Author name e.g. 'claude_code', 'sean'")
    content: str = Field(..., description="Handoff note content", min_length=1)

class SensorAlertInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    board_id:    str           = Field(..., description="UUID of the board")
    node_name:   str           = Field(..., description="Sensor node e.g. 'pi5-thermal'")
    event_type:  str           = Field(..., description="Event type e.g. 'presence_detected'")
    description: str           = Field(..., description="Human-readable description of the alert")
    payload:     Optional[dict] = Field(None, description="Optional raw event payload")

class StaleCardsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    board_id:        str = Field(..., description="UUID of the board")
    threshold_hours: int = Field(48, description="Hours before a card is considered stale", ge=1)

class SearchCardsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    board_id: str           = Field(..., description="UUID of the board")
    query:    str           = Field(..., description="Search term matched against title and description")
    limit:    int           = Field(20, description="Max results", ge=1, le=100)

# ─── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="kanban_get_board_state",
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
async def kanban_get_board_state(params: BoardId, ctx) -> str:
    """Return the current state of all active cards grouped by column.

    Args:
        params: BoardId containing board_id (UUID)

    Returns:
        JSON array of cards with column name, time in state, overdue flag.
    """
    pool = ctx.request_context.lifespan_state["pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM board_current_state
            WHERE id IN (
                SELECT id FROM cards WHERE board_id = $1
            )
            """,
            UUID(params.board_id)
        )
    return _json(rows)


@mcp.tool(
    name="kanban_list_columns",
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
async def kanban_list_columns(params: BoardId, ctx) -> str:
    """List all columns for a board with their WIP limits and card counts.

    Args:
        params: BoardId containing board_id (UUID)

    Returns:
        JSON array of columns with id, name, position, wip_limit, card_count.
    """
    pool = ctx.request_context.lifespan_state["pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT col.id, col.name, col.position, col.wip_limit,
                   COUNT(c.id) AS card_count
            FROM columns col
            LEFT JOIN cards c ON c.column_id = col.id AND c.completed_at IS NULL
            WHERE col.board_id = $1
            GROUP BY col.id
            ORDER BY col.position
            """,
            UUID(params.board_id)
        )
    return _json(rows)


@mcp.tool(
    name="kanban_create_card",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def kanban_create_card(params: CreateCardInput, ctx) -> str:
    """Create a new card on the board and record a created event.

    Args:
        params: CreateCardInput with title, column, type, priority, assignment.

    Returns:
        JSON of the newly created card.
    """
    pool = ctx.request_context.lifespan_state["pool"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM create_card($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            UUID(params.board_id),
            UUID(params.column_id),
            params.title,
            params.description,
            params.card_type,
            params.priority,
            params.assigned_to,
            params.related_node,
            params.actor
        )
    return _row(row)


@mcp.tool(
    name="kanban_move_card",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def kanban_move_card(params: MoveCardInput, ctx) -> str:
    """Move a card to a different column, enforcing WIP limits and recording the event.

    Args:
        params: MoveCardInput with card_id, to_column_id, actor, optional note.

    Returns:
        JSON of the generated card_event record.
    """
    pool = ctx.request_context.lifespan_state["pool"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM move_card($1,$2,$3,$4,$5)",
            UUID(params.card_id),
            UUID(params.to_column_id),
            params.actor,
            None,   # position — auto
            params.note
        )
    return _row(row)


@mcp.tool(
    name="kanban_add_handoff_note",
    annotations={"readOnlyHint": False, "destructiveHint": False}
)
async def kanban_add_handoff_note(params: HandoffNoteInput, ctx) -> str:
    """Add a handoff note to a card so the next person or agent has context.

    Args:
        params: HandoffNoteInput with card_id, author, content.

    Returns:
        JSON of the created handoff_note record.
    """
    pool = ctx.request_context.lifespan_state["pool"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM add_handoff_note($1,$2,$3)",
            UUID(params.card_id),
            params.author,
            params.content
        )
    return _row(row)


@mcp.tool(
    name="kanban_create_sensor_alert",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def kanban_create_sensor_alert(params: SensorAlertInput, ctx) -> str:
    """Create a card automatically from a sensor/MQTT event.

    Use this when a sensor node reports something that warrants human attention.

    Args:
        params: SensorAlertInput with board_id, node_name, event_type, description.

    Returns:
        JSON of the created card.
    """
    pool = ctx.request_context.lifespan_state["pool"]
    payload_json = json.dumps(params.payload or {})
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM create_sensor_alert_card($1,$2,$3,$4,$5::jsonb)",
            UUID(params.board_id),
            params.node_name,
            params.event_type,
            params.description,
            payload_json
        )
    return _row(row)


@mcp.tool(
    name="kanban_get_stale_cards",
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
async def kanban_get_stale_cards(params: StaleCardsInput, ctx) -> str:
    """Return cards that have not moved in longer than the threshold.

    Use this to drive ambient alerts — yellow/red light logic.

    Args:
        params: StaleCardsInput with board_id and threshold_hours (default 48).

    Returns:
        JSON array of stale cards with hours_in_state and priority.
    """
    pool = ctx.request_context.lifespan_state["pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM stale_cards($1,$2)",
            UUID(params.board_id),
            params.threshold_hours
        )
    return _json(rows)


@mcp.tool(
    name="kanban_get_card_history",
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
async def kanban_get_card_history(params: BoardId, ctx) -> str:
    """Return the full event history for a card — all moves, comments, agent actions.

    Args:
        params: BoardId — pass card_id as board_id field here.

    Returns:
        JSON array of card_events in chronological order.
    """
    pool = ctx.request_context.lifespan_state["pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ce.*, 
                   fc.name AS from_column_name,
                   tc.name AS to_column_name
            FROM card_events ce
            LEFT JOIN columns fc ON ce.from_column_id = fc.id
            LEFT JOIN columns tc ON ce.to_column_id = tc.id
            WHERE ce.card_id = $1
            ORDER BY ce.created_at ASC
            """,
            UUID(params.board_id)
        )
    return _json(rows)


@mcp.tool(
    name="kanban_search_cards",
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
async def kanban_search_cards(params: SearchCardsInput, ctx) -> str:
    """Search cards by title or description text.

    Args:
        params: SearchCardsInput with board_id, query string, limit.

    Returns:
        JSON array of matching cards with column name.
    """
    pool = ctx.request_context.lifespan_state["pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.*, col.name AS column_name
            FROM cards c
            JOIN columns col ON c.column_id = col.id
            WHERE c.board_id = $1
              AND (c.title ILIKE $2 OR c.description ILIKE $2)
              AND c.completed_at IS NULL
            ORDER BY c.updated_at DESC
            LIMIT $3
            """,
            UUID(params.board_id),
            f"%{params.query}%",
            params.limit
        )
    return _json(rows)


@mcp.tool(
    name="kanban_get_agent_summary",
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
async def kanban_get_agent_summary(params: BoardId, ctx) -> str:
    """Return a concise summary suitable for agent context loading at session start.

    Includes: stale cards, cards assigned to agents, recent events, WIP status.

    Args:
        params: BoardId containing board_id (UUID)

    Returns:
        JSON object with stale, agent_assigned, recent_events, wip_warnings.
    """
    pool = ctx.request_context.lifespan_state["pool"]
    bid = UUID(params.board_id)

    async with pool.acquire() as conn:
        stale = await conn.fetch("SELECT * FROM stale_cards($1, 48)", bid)

        agent_cards = await conn.fetch(
            """
            SELECT c.id, c.title, c.priority, col.name AS column_name
            FROM cards c JOIN columns col ON c.column_id = col.id
            WHERE c.board_id = $1
              AND c.assigned_to IN ('claude_code','copilot','agent')
              AND c.completed_at IS NULL
            ORDER BY c.priority DESC
            """,
            bid
        )

        recent_events = await conn.fetch(
            """
            SELECT ce.event_type, ce.actor, ce.created_at,
                   ca.title AS card_title,
                   tc.name  AS to_column
            FROM card_events ce
            JOIN cards ca ON ce.card_id = ca.id
            LEFT JOIN columns tc ON ce.to_column_id = tc.id
            WHERE ca.board_id = $1
            ORDER BY ce.created_at DESC
            LIMIT 10
            """,
            bid
        )

        wip_warnings = await conn.fetch(
            """
            SELECT col.name, col.wip_limit, COUNT(c.id) AS current_count
            FROM columns col
            LEFT JOIN cards c ON c.column_id = col.id AND c.completed_at IS NULL
            WHERE col.board_id = $1 AND col.wip_limit IS NOT NULL
            GROUP BY col.id
            HAVING COUNT(c.id) >= col.wip_limit
            """,
            bid
        )

    summary = {
        "stale_cards":      [dict(r) for r in stale],
        "agent_assigned":   [dict(r) for r in agent_cards],
        "recent_events":    [dict(r) for r in recent_events],
        "wip_warnings":     [dict(r) for r in wip_warnings],
    }
    return json.dumps(summary, default=str, indent=2)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
