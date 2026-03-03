"""
Connection pool for the conversation archive database.

If CORPUS_DB_DSN is not set (or empty), all corpus features degrade
gracefully — the sensor ecology continues to function normally,
corpus resonance data simply returns empty results.

Expected schema in the conversation archive database:

    CREATE TABLE conversation_chunks (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        source      TEXT,               -- 'claude', 'copilot', 'obsidian', etc.
        conversation_id TEXT,
        chunk_index INTEGER,
        chunk_text  TEXT,
        embedding   vector(768),        -- nomic-embed-text dim
        created_at  TIMESTAMPTZ DEFAULT NOW(),
        metadata    JSONB               -- any extra fields (title, tags, …)
    );

If your table or column names differ, set the env vars:
    CORPUS_TABLE            (default: conversation_chunks)
    CORPUS_TEXT_COL         (default: chunk_text)
    CORPUS_SOURCE_COL       (default: source)
    CORPUS_CONV_ID_COL      (default: conversation_id)
    CORPUS_EMBEDDING_COL    (default: embedding)
    CORPUS_META_COL         (default: metadata)
"""

import asyncpg
from app.config import CORPUS_DB_DSN

_corpus_pool: asyncpg.Pool | None = None


async def init_corpus_pool() -> None:
    global _corpus_pool
    if not CORPUS_DB_DSN:
        return  # corpus features disabled
    try:
        _corpus_pool = await asyncpg.create_pool(
            CORPUS_DB_DSN,
            min_size=1,
            max_size=4,
        )
    except Exception as e:
        # Non-fatal — log and continue without corpus features
        import logging
        logging.getLogger(__name__).warning(
            "Corpus DB unavailable (%s) — cross-corpus features disabled", e
        )
        _corpus_pool = None


async def close_corpus_pool() -> None:
    global _corpus_pool
    if _corpus_pool:
        await _corpus_pool.close()
        _corpus_pool = None


def get_corpus_pool() -> asyncpg.Pool | None:
    """Return the pool, or None if corpus features are disabled."""
    return _corpus_pool


def corpus_available() -> bool:
    return _corpus_pool is not None
