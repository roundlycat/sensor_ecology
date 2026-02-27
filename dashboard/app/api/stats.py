from fastapi import APIRouter

from app.db import queries

router = APIRouter()


@router.get("/")
async def dashboard_stats():
    return await queries.get_dashboard_stats()
