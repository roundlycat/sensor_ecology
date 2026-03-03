"""
Queries against the conversation corpus (separate pgvector database).

All functions return empty lists / None when the corpus pool is not
available — callers don't need to handle the disabled case explicitly.
"""

from typing import Optional
from app.db.corpus_connection import get_corpus_pool, corpus_available
from app.config import (
    CORPUS_TABLE,
    CORPUS_TEXT_COL,
    CORPUS_SOURCE_COL,
    CORPUS_CONV_ID_COL,
    CORPUS_EMBEDDING_COL,
    CORPUS_META_COL,
)


async def find_resonant_chunks(
    embedding: list[float],
    limit: int = 6,
    threshold: float = 0.40,
) -> list[dict]:
    """
    Return conversation corpus chunks nearest to the given 768-dim embedding.

    Returns [] when corpus is unavailable or no chunks are within threshold.

    Each result dict contains:
        chunk_text      str     — the raw passage
        similarity      float   — cosine similarity (1 - distance)
        source          str     — conversation source label
        conversation_id str     — conversation identifier
        metadata        dict    — any extra metadata stored alongside the chunk
        distance        float   — raw cosine distance (lower = closer)
    """
    if not corpus_available():
        return []

    pool = get_corpus_pool()
    vec_literal = "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"

    # Build column list defensively — only include columns that exist
    sql = f"""
        SELECT
            {CORPUS_TEXT_COL}       AS chunk_text,
            {CORPUS_SOURCE_COL}     AS source,
            {CORPUS_CONV_ID_COL}    AS conversation_id,
            {CORPUS_META_COL}       AS metadata,
            ({CORPUS_EMBEDDING_COL} <=> $1::vector) AS distance
        FROM {CORPUS_TABLE}
        WHERE {CORPUS_EMBEDDING_COL} IS NOT NULL
          AND ({CORPUS_EMBEDDING_COL} <=> $1::vector) <= $2
        ORDER BY distance ASC
        LIMIT $3
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, vec_literal, threshold, limit)

        results = []
        for r in rows:
            dist = float(r["distance"])
            results.append({
                "chunk_text":      r["chunk_text"],
                "source":          r["source"] or "archive",
                "conversation_id": r["conversation_id"],
                "metadata":        dict(r["metadata"]) if r["metadata"] else {},
                "distance":        dist,
                "similarity":      round(1.0 - dist, 4),
            })
        return results

    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Corpus query failed: %s", e)
        return []


async def get_event_embedding(event_id: str) -> Optional[list[float]]:
    """
    Fetch the embedding for a perceptual event from the sensor ecology DB.
    Returns None if the event is not found or has no embedding.
    """
    from app.db.connection import get_pool
    pool = get_pool()
    sql = """
        SELECT embedding::text
        FROM perceptual_events
        WHERE id = $1::uuid
          AND embedding IS NOT NULL
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, event_id)
        if not row:
            return None
        raw = row["embedding"].strip("[]")
        return [float(v) for v in raw.split(",")]
    except Exception:
        return None


async def find_corpus_echoes_for_event(
    event_id: str,
    limit: int = 6,
    threshold: float = 0.40,
) -> list[dict]:
    """
    Convenience wrapper: look up an event's embedding then query the corpus.
    Returns [] when event not found, embedding absent, or corpus unavailable.
    """
    embedding = await get_event_embedding(event_id)
    if not embedding:
        return []
    return await find_resonant_chunks(embedding, limit=limit, threshold=threshold)
