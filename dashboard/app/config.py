"""
Configuration — loaded from environment variables or a .env file.
Copy .env.example → .env and fill in your values.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")


# ── Sensor ecology database ───────────────────────────────────────────────────
DB_DSN: str = os.environ.get(
    "DB_DSN",
    "postgresql://sean:ecology@localhost/sensor_ecology",
)

DASHBOARD_HOST: str = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT: int = int(os.environ.get("DASHBOARD_PORT", "8000"))

EMBEDDING_MODEL: str = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")


# ── Conversation archive (corpus) database ────────────────────────────────────
# Set CORPUS_DB_DSN to enable cross-corpus resonance features.
# Leave empty to run without them — all other features are unaffected.
#
# Example: postgresql://sean@localhost/conversation_archive
CORPUS_DB_DSN: str = os.environ.get("CORPUS_DB_DSN", "")

# Column / table overrides — match these to your archive schema
CORPUS_TABLE:         str = os.environ.get("CORPUS_TABLE",         "conversation_chunks")
CORPUS_TEXT_COL:      str = os.environ.get("CORPUS_TEXT_COL",      "chunk_text")
CORPUS_SOURCE_COL:    str = os.environ.get("CORPUS_SOURCE_COL",    "source")
CORPUS_CONV_ID_COL:   str = os.environ.get("CORPUS_CONV_ID_COL",   "conversation_id")
CORPUS_EMBEDDING_COL: str = os.environ.get("CORPUS_EMBEDDING_COL", "embedding")
CORPUS_META_COL:      str = os.environ.get("CORPUS_META_COL",      "metadata")

# ── Ollama narrator ───────────────────────────────────────────────────────────
# OLLAMA_HOST: base URL of your Ollama instance (same machine as this dashboard)
# NARRATOR_MODEL: any generative model you have pulled, e.g. llama3.2, phi3.5, mistral
# NARRATOR_INTERVAL_S: seconds between automatic background regenerations
OLLAMA_HOST:         str = os.environ.get("OLLAMA_HOST",         "http://localhost:11434")
NARRATOR_MODEL:      str = os.environ.get("NARRATOR_MODEL",      "llama3.2")
NARRATOR_INTERVAL_S: int = int(os.environ.get("NARRATOR_INTERVAL_S", "300"))
