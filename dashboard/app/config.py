"""
Configuration — loaded from environment variables or a .env file.
Copy .env.example → .env and fill in your values.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

DB_DSN: str = os.environ.get(
    "DB_DSN",
    "postgresql://sean:ecology@localhost/sensor_ecology",
)

DASHBOARD_HOST: str = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT: int = int(os.environ.get("DASHBOARD_PORT", "8000"))

EMBEDDING_MODEL: str = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
