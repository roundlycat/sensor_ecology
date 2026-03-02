from fastapi import APIRouter, HTTPException
from app.db import queries

router = APIRouter()


@router.get("/")
async def list_node_states():
    """All nodes with their latest motion + light state."""
    return await queries.get_all_node_states()


@router.get("/{node_name}/events")
async def node_events(node_name: str, limit: int = 30):
    """Recent perceptual events for a specific node."""
    rows = await queries.get_node_recent_events(node_name, limit)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Node '{node_name}' not found or has no events")
    return rows
