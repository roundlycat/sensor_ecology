"""
Sensor Ecology Dashboard — entry point.
Run: uvicorn main:app --host 0.0.0.0 --port 9500
"""

<<<<<<< HEAD
=======
import asyncio
>>>>>>> a66c8a5c42b2a9a97d52513986abd37e5a4e5345
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from app.db.connection import init_pool, close_pool
<<<<<<< HEAD
from app.api import agents, observations, semantic, live, stats
from app.api import motifs
=======
from app.db.corpus_connection import init_corpus_pool, close_corpus_pool
from app.api import agents, observations, semantic, live, stats, motifs
from app.api import corpus
from app.services.narrator import start_narrator_loop
>>>>>>> a66c8a5c42b2a9a97d52513986abd37e5a4e5345

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
<<<<<<< HEAD
    await init_pool()
    yield
    await close_pool()
=======
    # Sensor ecology DB (required)
    await init_pool()

    # Conversation corpus DB (optional — logs warning if unavailable)
    await init_corpus_pool()

    # Ollama narrator background loop
    narrator_task = asyncio.create_task(start_narrator_loop())

    yield

    narrator_task.cancel()
    await close_pool()
    await close_corpus_pool()
>>>>>>> a66c8a5c42b2a9a97d52513986abd37e5a4e5345


app = FastAPI(title="Sensor Ecology Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# ── API routers ────────────────────────────────────────────────────────────────
app.include_router(agents.router,       prefix="/api/agents",       tags=["agents"])
app.include_router(observations.router, prefix="/api/observations",  tags=["observations"])
app.include_router(semantic.router,     prefix="/api/semantic",      tags=["semantic"])
app.include_router(stats.router,        prefix="/api/stats",         tags=["stats"])
app.include_router(motifs.router,       prefix="/api/motifs",        tags=["motifs"])
<<<<<<< HEAD
app.include_router(live.router,         prefix="/live",              tags=["live"])

# ── Single page app — all views handled client-side ───────────────────────────
=======
app.include_router(corpus.router,       prefix="/api/corpus",        tags=["corpus"])
app.include_router(live.router,         prefix="/live",              tags=["live"])


# ── SPA — all page routes serve index.html ────────────────────────────────────
>>>>>>> a66c8a5c42b2a9a97d52513986abd37e5a4e5345
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

<<<<<<< HEAD
# Legacy routes redirect to SPA
=======

>>>>>>> a66c8a5c42b2a9a97d52513986abd37e5a4e5345
@app.get("/{path:path}", response_class=HTMLResponse)
async def spa_fallback(request: Request, path: str):
    return templates.TemplateResponse("index.html", {"request": request})
