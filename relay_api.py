# relay_api.py
# Thin FastAPI relay between PostgreSQL and the Unity client.
# Runs on the Pi 5 alongside the ingestion layer.
# Unity's UnityWebRequest talks to this; it talks to asyncpg.
#
# Endpoints:
#   GET  /api/events                   - recent perceptual events with resonance
#   GET  /api/events/stream            - SSE stream of live events
#   GET  /api/agent/{node_id}/vitals   - current agent metabolic state
#   GET  /api/motifs/echoes            - query perceptual echoes of a motif
#
# Run: uvicorn relay_api:app --host 0.0.0.0 --port 8765 --reload

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional
from uuid import UUID

import asyncpg
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Startup / shutdown — shared asyncpg pool
# ---------------------------------------------------------------------------

pool: asyncpg.Pool | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"],
        min_size=2,
        max_size=8,
    )
    yield
    await pool.close()

app = FastAPI(title="Agent Perception Relay", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],     # tighten for production
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Response models (Pydantic — clean JSON for Unity)
# ---------------------------------------------------------------------------

class FeatureSnapshotOut(BaseModel):
    domain:        Optional[str]
    label:         Optional[str]
    agent_power:   Optional[float]
    agent_temp:    Optional[float]
    agent_cpu:     Optional[int]
    n_readings:    Optional[int]
    channel_keys:  list[str]      = []
    channel_values: list[float]   = []

    @classmethod
    def from_jsonb(cls, snap: dict) -> "FeatureSnapshotOut":
        channels: dict = snap.get("channels", {})
        return cls(
            domain=snap.get("domain"),
            label=snap.get("label"),
            agent_power=snap.get("agent_power"),
            agent_temp=snap.get("agent_temp"),
            agent_cpu=snap.get("agent_cpu"),
            n_readings=snap.get("n_readings"),
            channel_keys=list(channels.keys()),
            channel_values=list(channels.values()),
        )


class MotifResonanceOut(BaseModel):
    id:                      str
    perceptual_event_id:     str
    motif_id:                str
    cosine_distance:         float
    is_nearest:              bool
    resonance_type:          str
    observed_at:             str


class PerceptualEventOut(BaseModel):
    id:                  str
    agent_node_id:       str
    domain:              str
    event_label:         Optional[str]
    confidence:          str
    event_start:         str
    is_cross_domain:     bool
    domains_involved:    list[str]
    agent_power_mw:      Optional[float]
    agent_temp_c:        Optional[float]
    agent_cpu_load_pct:  Optional[int]
    feature_snapshot:    FeatureSnapshotOut
    nearest_resonance:   Optional[MotifResonanceOut]


class AgentVitalsOut(BaseModel):
    node_name:       str
    power_mw:        Optional[float]
    temp_c:          Optional[float]
    cpu_load_pct:    Optional[int]
    last_heartbeat:  Optional[str]
    is_online:       bool


class EventListOut(BaseModel):
    events:  list[PerceptualEventOut]
    total:   int
    cursor:  Optional[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


async def _fetch_events(
    node_id:    Optional[str],
    domain:     Optional[str],
    since:      Optional[str],
    limit:      int,
) -> list[asyncpg.Record]:
    conditions = ["1=1"]
    params     = []

    if node_id:
        params.append(UUID(node_id))
        conditions.append(f"pe.agent_node_id = ${len(params)}")

    if domain:
        params.append(domain)
        conditions.append(f"pe.domain = ${len(params)}")

    if since:
        params.append(datetime.fromisoformat(since))
        conditions.append(f"pe.event_start > ${len(params)}")

    params.append(limit)
    where = " AND ".join(conditions)

    return await pool.fetch(
        f"""
        SELECT
            pe.id,
            pe.agent_node_id,
            pe.domain,
            pe.event_label,
            pe.confidence,
            pe.event_start,
            pe.is_cross_domain,
            pe.domains_involved,
            pe.agent_power_mw,
            pe.agent_temp_c,
            pe.agent_cpu_load_pct,
            pe.feature_snapshot,
            mr.id              AS res_id,
            mr.motif_id        AS res_motif_id,
            mr.cosine_distance AS res_distance,
            mr.resonance_type  AS res_type,
            mr.observed_at     AS res_observed_at
        FROM perceptual_events pe
        LEFT JOIN motif_resonance mr
            ON mr.perceptual_event_id = pe.id
            AND mr.is_nearest = TRUE
        WHERE {where}
        ORDER BY pe.event_start DESC
        LIMIT ${len(params)}
        """,
        *params,
    )


def _row_to_event(row: asyncpg.Record) -> PerceptualEventOut:
    snap_dict = dict(row["feature_snapshot"]) if row["feature_snapshot"] else {}
    snap = FeatureSnapshotOut.from_jsonb(snap_dict)

    resonance = None
    if row["res_id"]:
        resonance = MotifResonanceOut(
            id=str(row["res_id"]),
            perceptual_event_id=str(row["id"]),
            motif_id=str(row["res_motif_id"]),
            cosine_distance=float(row["res_distance"]),
            is_nearest=True,
            resonance_type=row["res_type"],
            observed_at=_fmt_dt(row["res_observed_at"]),
        )

    return PerceptualEventOut(
        id=str(row["id"]),
        agent_node_id=str(row["agent_node_id"]),
        domain=row["domain"],
        event_label=row["event_label"],
        confidence=row["confidence"],
        event_start=_fmt_dt(row["event_start"]),
        is_cross_domain=row["is_cross_domain"],
        domains_involved=list(row["domains_involved"] or []),
        agent_power_mw=row["agent_power_mw"],
        agent_temp_c=row["agent_temp_c"],
        agent_cpu_load_pct=row["agent_cpu_load_pct"],
        feature_snapshot=snap,
        nearest_resonance=resonance,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/events", response_model=EventListOut)
async def get_events(
    node_id: Optional[str]  = Query(None),
    domain:  Optional[str]  = Query(None),
    since:   Optional[str]  = Query(None),    # ISO datetime cursor
    limit:   int            = Query(50, le=200),
):
    """
    Recent perceptual events, newest first. Unity polls this at ~1Hz.
    Use `since` as a cursor: pass back the event_start of the most recent
    event you received to get only newer events.
    """
    rows   = await _fetch_events(node_id, domain, since, limit)
    events = [_row_to_event(r) for r in rows]
    cursor = events[0].event_start if events else None

    return EventListOut(events=events, total=len(events), cursor=cursor)


@app.get("/api/events/stream")
async def stream_events(
    node_id: Optional[str] = Query(None),
    domain:  Optional[str] = Query(None),
):
    """
    Server-Sent Events stream of live perceptual events.
    Unity connects once and receives push updates — no polling overhead.
    Use the UnitySSEClient for this endpoint.
    """
    async def generator() -> AsyncGenerator[str, None]:
        last_seen: Optional[datetime] = None

        while True:
            since_str = last_seen.isoformat() if last_seen else None
            rows = await _fetch_events(node_id, domain, since_str, limit=20)

            for row in reversed(rows):   # oldest first so client processes in order
                event = _row_to_event(row)
                data  = event.model_dump_json()
                yield f"data: {data}\n\n"

                # Advance cursor
                ts = datetime.fromisoformat(event.event_start)
                if last_seen is None or ts > last_seen:
                    last_seen = ts

            await asyncio.sleep(0.5)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/agent/{node_id}/vitals", response_model=AgentVitalsOut)
async def get_vitals(node_id: str):
    """
    Current metabolic state for a node. Unity reads this to skin the
    agent representation with current thermal/power state.
    """
    row = await pool.fetchrow(
        """
        SELECT
            an.node_name,
            an.last_heartbeat_at,
            sr_power.raw_value  AS power_mw,
            sr_temp.raw_value   AS temp_c,
            sr_cpu.raw_value    AS cpu_load_pct
        FROM agent_nodes an
        LEFT JOIN LATERAL (
            SELECT raw_value FROM sensor_readings
            WHERE agent_node_id = an.id AND channel = 'power_mW'
            ORDER BY recorded_at DESC LIMIT 1
        ) sr_power ON TRUE
        LEFT JOIN LATERAL (
            SELECT raw_value FROM sensor_readings
            WHERE agent_node_id = an.id AND channel = 'cpu_temp_c'
            ORDER BY recorded_at DESC LIMIT 1
        ) sr_temp ON TRUE
        LEFT JOIN LATERAL (
            SELECT raw_value FROM sensor_readings
            WHERE agent_node_id = an.id AND channel = 'cpu_load_pct'
            ORDER BY recorded_at DESC LIMIT 1
        ) sr_cpu ON TRUE
        WHERE an.id = $1
        """,
        UUID(node_id),
    )

    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, "Node not found")

    heartbeat = row["last_heartbeat_at"]
    is_online = (
        heartbeat is not None
        and (datetime.now(timezone.utc) - heartbeat).total_seconds() < 30
    )

    return AgentVitalsOut(
        node_name=row["node_name"],
        power_mw=row["power_mw"],
        temp_c=row["temp_c"],
        cpu_load_pct=int(row["cpu_load_pct"]) if row["cpu_load_pct"] else None,
        last_heartbeat=_fmt_dt(heartbeat),
        is_online=is_online,
    )


@app.get("/api/motifs/{motif_id}/echoes")
async def get_perceptual_echoes(
    motif_id:  str,
    threshold: float = Query(0.25, le=1.0),
    limit:     int   = Query(20, le=100),
):
    """
    Physical perceptual events that echo a given linguistic motif.
    Unity can call this when the user focuses on a motif node to show
    the physical events that resonated with it.
    """
    # Fetch motif centroid embedding
    motif = await pool.fetchrow(
        "SELECT centroid_embedding FROM motifs WHERE id = $1",
        UUID(motif_id),
    )
    if not motif or motif["centroid_embedding"] is None:
        from fastapi import HTTPException
        raise HTTPException(404, "Motif not found or has no embedding")

    rows = await pool.fetch(
        """
        SELECT
            pe.id, an.node_name, pe.domain,
            pe.event_label, pe.event_start,
            (pe.embedding <=> $1::vector) AS cosine_distance
        FROM perceptual_events pe
        JOIN agent_nodes an ON an.id = pe.agent_node_id
        WHERE pe.embedding IS NOT NULL
          AND (pe.embedding <=> $1::vector) < $2
        ORDER BY cosine_distance ASC
        LIMIT $3
        """,
        motif["centroid_embedding"],
        threshold,
        limit,
    )

    return {
        "motif_id": motif_id,
        "threshold": threshold,
        "echoes": [
            {
                "event_id":        str(r["id"]),
                "node_name":       r["node_name"],
                "domain":          r["domain"],
                "event_label":     r["event_label"],
                "event_start":     _fmt_dt(r["event_start"]),
                "cosine_distance": float(r["cosine_distance"]),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Motif list — used by MotifGraphScene to build the AR node graph
# ---------------------------------------------------------------------------

class MotifOut(BaseModel):
    id:                  str
    label:               Optional[str]
    recurrence_count:    int       # how many physical events resonated
    last_resonance_at:   Optional[str]
    dominant_domain:     Optional[str]   # which sensor domain resonates most
    has_embedding:       bool


class MotifListOut(BaseModel):
    motifs:    list[MotifOut]
    total:     int
    bootstrap: bool   # True when the motifs table is empty — corpus not yet seeded


@app.get("/api/motifs", response_model=MotifListOut)
async def list_motifs(
    min_recurrences: int   = Query(0),
    limit:           int   = Query(100, le=500),
):
    """
    List motifs with their physical resonance statistics.
    MotifGraphScene calls this on startup to know which nodes to place
    and how to size/colour them based on physical event history.
    """
    rows = await pool.fetch(
        """
        SELECT
            m.id,
            m.label,
            m.centroid_embedding IS NOT NULL          AS has_embedding,
            COUNT(mr.id)                              AS recurrence_count,
            MAX(mr.observed_at)                       AS last_resonance_at,
            MODE() WITHIN GROUP (ORDER BY pe.domain)  AS dominant_domain
        FROM motifs m
        LEFT JOIN motif_resonance mr
            ON mr.motif_id = m.id
            AND mr.resonance_type = 'recurrence'
        LEFT JOIN perceptual_events pe
            ON pe.id = mr.perceptual_event_id
        GROUP BY m.id, m.label, m.centroid_embedding
        HAVING COUNT(mr.id) >= $1
        ORDER BY recurrence_count DESC
        LIMIT $2
        """,
        min_recurrences,
        limit,
    )

    motifs = [
        MotifOut(
            id=str(r["id"]),
            label=r["label"],
            recurrence_count=r["recurrence_count"],
            last_resonance_at=_fmt_dt(r["last_resonance_at"]),
            dominant_domain=r["dominant_domain"],
            has_embedding=r["has_embedding"],
        )
        for r in rows
    ]

    # Bootstrap = the table itself is empty, not merely filtered-to-zero.
    # An empty list from a non-zero min_recurrences filter is a different condition
    # and shouldn't trigger the bootstrap state in the AR layer.
    count_row = await pool.fetchrow("SELECT COUNT(*) AS n FROM motifs")
    bootstrap = count_row["n"] == 0

    return MotifListOut(motifs=motifs, total=len(motifs), bootstrap=bootstrap)


@app.get("/api/motifs/{motif_id}/stats")
async def get_motif_stats(motif_id: str):
    """
    Per-motif resonance breakdown by sensor domain.
    Used by MotifNode to colour its domain breakdown ring.
    """
    rows = await pool.fetch(
        """
        SELECT
            pe.domain,
            COUNT(*)            AS count,
            AVG(mr.cosine_distance) AS avg_distance
        FROM motif_resonance mr
        JOIN perceptual_events pe ON pe.id = mr.perceptual_event_id
        WHERE mr.motif_id = $1
          AND mr.resonance_type IN ('recurrence', 'weak_echo')
        GROUP BY pe.domain
        ORDER BY count DESC
        """,
        UUID(motif_id),
    )

    return {
        "motif_id": motif_id,
        "domain_breakdown": [
            {
                "domain":        r["domain"],
                "count":         r["count"],
                "avg_distance":  float(r["avg_distance"]),
            }
            for r in rows
        ],
    }
