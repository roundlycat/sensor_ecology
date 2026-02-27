"""
Embedding service — lazy singleton over the sentence-transformers model.

The same all-MiniLM-L6-v2 model used by the agents is reused here so that
semantic search queries produce embeddings in the same vector space.
"""

import threading

from app.config import EMBEDDING_MODEL

_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model


def embed_text(text: str) -> list[float]:
    """Return a 384-dim embedding for a query string."""
    return _get_model().encode(text).tolist()
