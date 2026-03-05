"""relay_mcp_server.py
MCP stdio server that wraps the relay_api HTTP endpoints for Claude Desktop.

Exposes sensor ecology data (perceptual events, agent vitals, motifs) as MCP tools.

Configure Claude Desktop (~/.config/Claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "sensor-ecology": {
        "command": "python3",
        "args": ["/home/sean/sensor_ecology/relay_mcp_server.py"],
        "env": { "RELAY_URL": "http://localhost:8765" }
      }
    }
  }

The relay_api itself still needs to be running separately:
  export DATABASE_URL="postgres://user:password@localhost:5432/sensor_ecology"
  uvicorn relay_api:app --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

RELAY_URL = os.environ.get("RELAY_URL", "http://localhost:8765")

server = Server("sensor-ecology-relay")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_events",
            description=(
                "Fetch recent perceptual events from sensor nodes "
                "(light, motion, acoustic, etc.). Results newest-first. "
                "Use 'since' as a cursor to get only new events."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "Filter by agent node UUID (optional)",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Filter by sensor domain, e.g. 'light', 'acoustic' (optional)",
                    },
                    "since": {
                        "type": "string",
                        "description": "ISO-8601 datetime cursor — return only events after this timestamp (optional)",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max events to return (1–200)",
                    },
                },
            },
        ),
        types.Tool(
            name="get_agent_vitals",
            description="Get current metabolic state (power, temperature, CPU) for a sensor node.",
            inputSchema={
                "type": "object",
                "required": ["node_id"],
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "Agent node UUID",
                    },
                },
            },
        ),
        types.Tool(
            name="list_motifs",
            description=(
                "List linguistic motifs with their physical resonance statistics "
                "(how often each motif has been echoed by sensor events)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "min_recurrences": {
                        "type": "integer",
                        "default": 0,
                        "description": "Only return motifs with at least this many recurrences",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max motifs to return",
                    },
                },
            },
        ),
        types.Tool(
            name="get_motif_echoes",
            description=(
                "Find physical sensor events that semantically echo a linguistic motif "
                "(cosine similarity in embedding space)."
            ),
            inputSchema={
                "type": "object",
                "required": ["motif_id"],
                "properties": {
                    "motif_id": {
                        "type": "string",
                        "description": "Motif UUID",
                    },
                    "threshold": {
                        "type": "number",
                        "default": 0.25,
                        "description": "Max cosine distance to include (0–1, lower = more similar)",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                    },
                },
            },
        ),
        types.Tool(
            name="get_motif_stats",
            description="Get per-motif resonance breakdown by sensor domain.",
            inputSchema={
                "type": "object",
                "required": ["motif_id"],
                "properties": {
                    "motif_id": {
                        "type": "string",
                        "description": "Motif UUID",
                    },
                },
            },
        ),
        types.Tool(
            name="get_motif_drift",
            description="Get the chronological trace of semantic drift across motifs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "default": 100,
                        "description": "Max drift records to return",
                    },
                },
            },
        ),
        types.Tool(
            name="relay_health",
            description="Check whether the relay API is reachable and return its base URL.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    async with httpx.AsyncClient(base_url=RELAY_URL, timeout=10.0) as client:
        try:
            result = await _dispatch(client, name, arguments)
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
        except httpx.ConnectError:
            return [
                types.TextContent(
                    type="text",
                    text=(
                        f"Cannot connect to relay at {RELAY_URL}.\n"
                        "Make sure the relay is running:\n"
                        "  export DATABASE_URL='postgres://user:password@localhost:5432/sensor_ecology'\n"
                        "  uvicorn relay_api:app --host 0.0.0.0 --port 8765"
                    ),
                )
            ]
        except httpx.HTTPStatusError as e:
            return [
                types.TextContent(
                    type="text",
                    text=f"HTTP {e.response.status_code}: {e.response.text}",
                )
            ]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error: {type(e).__name__}: {e}")]


async def _dispatch(client: httpx.AsyncClient, name: str, args: dict):
    def clean(d: dict) -> dict:
        """Drop None values so they don't become query params."""
        return {k: v for k, v in d.items() if v is not None}

    if name == "get_events":
        r = await client.get("/api/events", params=clean(args))
        r.raise_for_status()
        return r.json()

    if name == "get_agent_vitals":
        r = await client.get(f"/api/agent/{args['node_id']}/vitals")
        r.raise_for_status()
        return r.json()

    if name == "list_motifs":
        r = await client.get("/api/motifs", params=clean(args))
        r.raise_for_status()
        return r.json()

    if name == "get_motif_echoes":
        motif_id = args.pop("motif_id")
        r = await client.get(f"/api/motifs/{motif_id}/echoes", params=clean(args))
        r.raise_for_status()
        return r.json()

    if name == "get_motif_stats":
        r = await client.get(f"/api/motifs/{args['motif_id']}/stats")
        r.raise_for_status()
        return r.json()

    if name == "get_motif_drift":
        r = await client.get("/api/motifs/drift", params=clean(args))
        r.raise_for_status()
        return r.json()

    if name == "relay_health":
        try:
            r = await client.get("/api/motifs", params={"limit": 1})
            r.raise_for_status()
            return {"status": "ok", "relay_url": RELAY_URL}
        except Exception as e:
            return {"status": "unreachable", "relay_url": RELAY_URL, "error": str(e)}

    return {"error": f"Unknown tool: {name}"}


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
