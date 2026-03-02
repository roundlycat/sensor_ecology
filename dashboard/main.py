"""
Sensor Ecology Dashboard — entry point.
Run: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db.connection import init_pool, close_pool
from app.api import agents, observations, semantic, live, stats, nodes

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
    await close_pool()


app = FastAPI(title="Sensor Ecology Dashboard", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# ── API routers ────────────────────────────────────────────────────────────────
app.include_router(agents.router,       prefix="/api/agents",       tags=["agents"])
app.include_router(observations.router, prefix="/api/observations",  tags=["observations"])
app.include_router(semantic.router,     prefix="/api/semantic",      tags=["semantic"])
app.include_router(stats.router,        prefix="/api/stats",         tags=["stats"])
app.include_router(live.router,         prefix="/live",              tags=["live"])
app.include_router(nodes.router,        prefix="/api/nodes",         tags=["nodes"])


# ── Page routes ────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    return templates.TemplateResponse("agents.html", {"request": request})


@app.get("/observations", response_class=HTMLResponse)
async def observations_page(request: Request):
    return templates.TemplateResponse("observations.html", {"request": request})


@app.get("/semantic", response_class=HTMLResponse)
async def semantic_page(request: Request):
    return templates.TemplateResponse("semantic.html", {"request": request})


@app.get("/stream", response_class=HTMLResponse)
async def live_page(request: Request):
    return templates.TemplateResponse("live.html", {"request": request})


@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request):
    return templates.TemplateResponse("monitor.html", {"request": request})
