from fastapi import APIRouter, Query
from app.db import queries

router = APIRouter()


@router.get("/active")
async def active_motifs(limit: int = Query(default=30, ge=1, le=100)):
    """Motifs that have had at least one resonance, ordered by recency."""
    return await queries.get_active_motifs(limit=limit)


@router.get("/{motif_id}/echoes")
async def motif_echoes(
    motif_id: str,
    limit: int = Query(default=20, ge=1, le=100),
):
    """Perceptual events that echoed a specific motif."""
    return await queries.get_motif_echoes(motif_id=motif_id, limit=limit)
