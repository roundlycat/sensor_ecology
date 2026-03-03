from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel


class Observation(BaseModel):
    observation_id: UUID
    agent_id: UUID
    agent_name: str
    agent_type: str
    observed_at: datetime
    observation_type: str
    confidence: Optional[float] = None
    semantic_summary: str
    raw_data: Optional[Any] = None

    model_config = {"from_attributes": True}


class SimilarObservation(BaseModel):
    observation_id: UUID
    agent_id: UUID
    agent_name: str
    agent_type: str
    observed_at: datetime
    observation_type: str
    semantic_summary: str
    confidence: Optional[float] = None
    similarity: float

    model_config = {"from_attributes": True}


class SearchRequest(BaseModel):
    query: str
    threshold: float = 0.70
    limit: int = 10
