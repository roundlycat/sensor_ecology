from fastapi import APIRouter

from app.db import queries
from app.models.observation import SearchRequest, SimilarObservation
from app.services.embeddings import embed_text

router = APIRouter()


@router.post("/search", response_model=list[SimilarObservation])
async def semantic_search(req: SearchRequest):
    """
    Embed the query string with all-MiniLM-L6-v2, then find observations
    whose stored embeddings are within `threshold` cosine similarity.
    """
    embedding = embed_text(req.query)
    return await queries.find_similar_observations(
        embedding=embedding,
        threshold=req.threshold,
        limit=req.limit,
    )
