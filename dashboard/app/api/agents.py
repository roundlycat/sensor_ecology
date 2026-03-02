from fastapi import APIRouter, HTTPException

from app.db import queries
from app.models.agent import Agent

router = APIRouter()


@router.get("/", response_model=list[Agent])
async def list_agents():
    return await queries.get_all_agents()


@router.get("/meta/types")
async def agent_types():
    return await queries.get_agent_types()


@router.get("/nodes")
async def agent_nodes():
    """Agent nodes in the perceptual system (agent_nodes table)."""
    return await queries.get_agent_nodes()


@router.get("/{agent_id}", response_model=Agent)
async def get_agent(agent_id: str):
    row = await queries.get_agent_by_id(agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    return row
