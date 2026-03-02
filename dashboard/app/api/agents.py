from fastapi import APIRouter, HTTPException

from app.db import queries
from app.models.agent import Agent, NodeRegister

router = APIRouter()


@router.get("/", response_model=list[Agent])
async def list_agents():
    return await queries.get_all_agents()


@router.post("/nodes/register", status_code=201)
async def register_node(body: NodeRegister):
    return await queries.register_agent_node(
        node_name=body.node_name,
        node_type=body.node_type,
        location_label=body.location_label,
        metadata=body.metadata,
    )


@router.get("/meta/types")
async def agent_types():
    return await queries.get_agent_types()


@router.get("/{agent_id}", response_model=Agent)
async def get_agent(agent_id: str):
    row = await queries.get_agent_by_id(agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    return row
