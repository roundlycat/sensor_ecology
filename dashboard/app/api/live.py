"""
Server-Sent Events live feed.

The client connects to GET /live/feed and receives a stream of JSON arrays,
each containing new observations since the last push.
"""

import asyncio
import json
import uuid
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.db import queries

router = APIRouter()


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    raise TypeError(f"Not serializable: {type(obj)}")


@router.get("/feed")
async def live_feed():
    async def event_stream():
        seen_ids: set[str] = set()
        # Seed with current IDs so we only push *new* arrivals
        try:
            seed = await queries.get_latest_observations(limit=20)
            for item in seed:
                seen_ids.add(str(item["observation_id"]))
        except Exception:
            pass

        while True:
            try:
                batch = await queries.get_latest_observations(limit=20)
                new_items = [
                    o for o in batch
                    if str(o["observation_id"]) not in seen_ids
                ]
                if new_items:
                    for item in new_items:
                        seen_ids.add(str(item["observation_id"]))
                    payload = json.dumps(new_items, default=_json_default)
                    yield f"data: {payload}\n\n"
            except Exception:
                pass
            await asyncio.sleep(3)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
