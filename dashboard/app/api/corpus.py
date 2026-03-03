"""
API endpoints for cross-corpus resonance and Ollama narrator.

Routes (all under /api/corpus prefix):
    GET  /api/corpus/status           — is corpus available? is narrator running?
    GET  /api/corpus/resonance/{id}   — corpus chunks near a perceptual event
    POST /api/corpus/resonance        — corpus chunks near an arbitrary embedding
    GET  /api/corpus/narrative        — cached field narrative (triggers regen if stale)
    POST /api/corpus/narrative/force  — force immediate regeneration
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db.corpus_connection import corpus_available
from app.db.corpus_queries import (
    find_resonant_chunks,
    find_corpus_echoes_for_event,
)
from app.services.narrator import (
    generate_narrative,
    get_cached_narrative,
)

router = APIRouter()


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
async def corpus_status():
    """Health-check: confirm which features are available."""
    cached = get_cached_narrative()
    return {
        "corpus_available":   corpus_available(),
        "narrator_available": True,  # always attempt — Ollama errors reported in response
        "narrator_model":     cached["model"],
        "narrative_cached":   cached["text"] is not None,
        "narrative_age_s":    (
            round(__import__("time").time() - cached["generated_at"])
            if cached["generated_at"] else None
        ),
        "is_composing":       cached["is_running"],
    }


# ── Corpus resonance ─────────────────────────────────────────────────────────

@router.get("/resonance/{event_id}")
async def event_corpus_resonance(
    event_id: str,
    limit: int = Query(default=6, ge=1, le=20),
    threshold: float = Query(default=0.40, ge=0.0, le=1.0),
):
    """
    Find conversation archive passages that resonate with a perceptual event.

    Looks up the event's embedding from perceptual_events, then queries the
    corpus for nearest neighbours by cosine distance.

    Returns [] (not 404) when the corpus is unavailable — clients should
    treat empty results as 'no corpus' rather than 'no matches'.
    """
    echoes = await find_corpus_echoes_for_event(
        event_id, limit=limit, threshold=threshold
    )
    return {
        "event_id": event_id,
        "corpus_available": corpus_available(),
        "echoes": echoes,
        "count": len(echoes),
    }


class EmbeddingRequest(BaseModel):
    embedding: list[float]
    limit: int = 6
    threshold: float = 0.40


@router.post("/resonance")
async def embedding_corpus_resonance(body: EmbeddingRequest):
    """
    Find corpus passages near an arbitrary embedding vector.
    Useful for querying with a freshly computed embedding that isn't in the DB yet.
    """
    if not body.embedding:
        raise HTTPException(status_code=422, detail="embedding must be non-empty")
    echoes = await find_resonant_chunks(
        body.embedding, limit=body.limit, threshold=body.threshold
    )
    return {
        "corpus_available": corpus_available(),
        "echoes": echoes,
        "count": len(echoes),
    }


# ── Narrator ─────────────────────────────────────────────────────────────────

@router.get("/narrative")
async def field_narrative():
    """
    Return the current cached field narrative, triggering background
    regeneration if the cache is stale (> NARRATOR_INTERVAL_S seconds old).

    Response fields:
        text            str | null  — the generated narrative
        generated_at    float | null — unix timestamp
        is_running      bool        — True while Ollama is generating
        model           str         — model tag in use
    """
    result = await generate_narrative()
    return result


@router.post("/narrative/force")
async def force_narrative():
    """
    Trigger an immediate narrative regeneration regardless of cache age.
    Returns immediately with is_running=True; poll GET /narrative for the result.
    """
    result = await generate_narrative(force=True)
    return {"status": "regenerating", **result}
