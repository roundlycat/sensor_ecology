from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel


class Agent(BaseModel):
    agent_id: UUID
    agent_type: str
    name: Optional[str] = None
    capabilities: Optional[Any] = None
    location_context: Optional[str] = None
    birth_ts: Optional[datetime] = None
    last_active_ts: Optional[datetime] = None
    observation_count: Optional[int] = None

    model_config = {"from_attributes": True}
