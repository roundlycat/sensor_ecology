import os
from fastapi import APIRouter
from pathlib import Path

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent.parent

@router.get("")
@router.get("/")
async def list_musings():
    """List and return the contents of all texts in the musings folder."""
    musings_dir = BASE_DIR / "static" / "musings"
    if not musings_dir.exists():
        return {"musings": []}

    musings = []
    # Search for markdown and text files
    files = list(musings_dir.glob("*.txt")) + list(musings_dir.glob("*.md"))
    # Sort files by modification time (newest first)
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as file:
                content = file.read()
            musings.append({
                "filename": f.name,
                "title": f.stem.replace("_", " ").title(),
                "content": content,
                "created_at": f.stat().st_mtime
            })
        except Exception:
            pass

    return {"musings": musings}
