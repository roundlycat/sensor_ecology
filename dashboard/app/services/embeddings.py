"""
Embedding service — calls the local Ollama endpoint used by the ingestion pipeline,
keeping the query vector in the same space as stored perceptual_events.embedding.
"""

import httpx

from app.config import EMBEDDING_MODEL, OLLAMA_URL


async def embed_text(text: str) -> list[float]:
    """Return an embedding for a query string via Ollama."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            OLLAMA_URL,
            json={"model": EMBEDDING_MODEL, "prompt": text},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
