from fastapi import APIRouter, Query
from app.db import queries

router = APIRouter()


@router.get("/")
@router.get("/summary")
async def dashboard_summary():
    """Legacy + canonical dashboard summary stats."""
    return await queries.get_dashboard_stats()


@router.get("/ecology")
async def ecology_state():
    """Current holistic state of the ecology: vitals, per-domain last event, active motifs."""
    return await queries.get_ecology_state()


@router.get("/domain-activity")
async def domain_activity(hours: int = Query(default=24, ge=1, le=168)):
    """Event counts by domain in hourly buckets."""
    return await queries.get_domain_activity(hours=hours)


@router.get("/events")
async def perceptual_events(
    limit: int = Query(default=50, ge=1, le=200),
    domain: str | None = None,
    since: str | None = None,
):
    """Rich perceptual events with vitals and nearest resonance annotation."""
    return await queries.get_perceptual_events(limit=limit, domain=domain, since=since)
