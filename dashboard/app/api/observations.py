from typing import Optional

from fastapi import APIRouter, Query

from app.db import queries
from app.models.observation import Observation

router = APIRouter()


@router.get("/recent", response_model=list[Observation])
async def recent_observations(
    limit: int = Query(50, ge=1, le=500),
    agent_type: Optional[str] = None,
    observation_type: Optional[str] = None,
    since: Optional[str] = None,
):
    return await queries.get_recent_observations(
        limit=limit,
        agent_type=agent_type,
        observation_type=observation_type,
        since=since,
    )


@router.get("/meta/types")
async def observation_types():
    return await queries.get_observation_types()
