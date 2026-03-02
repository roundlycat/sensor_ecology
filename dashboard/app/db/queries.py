"""
All database queries for the dashboard.
Each function acquires a connection from the pool, runs its query, and returns
plain Python dicts so callers are not coupled to asyncpg Record objects.

Adding a new query: add a new async function here and call it from an API module.
"""

from typing import Optional

from app.db.connection import get_pool


# ── Observations ───────────────────────────────────────────────────────────────

async def get_recent_observations(
    limit: int = 50,
    agent_type: Optional[str] = None,
    observation_type: Optional[str] = None,
    since: Optional[str] = None,
) -> list[dict]:
    pool = get_pool()
    sql = """
        WITH obs AS (
            SELECT DISTINCT ON (o.agent_id, o.observation_type, date_trunc('second', o.observed_at))
                o.observation_id,
                o.agent_id,
                a.name          AS agent_name,
                a.agent_type,
                o.observed_at,
                o.observation_type,
                o.confidence::float8,
                o.semantic_summary,
                o.raw_data
            FROM observations o
            JOIN agents a USING (agent_id)
            WHERE ($1::text IS NULL OR a.agent_type       = $1)
              AND ($2::text IS NULL OR o.observation_type = $2)
              AND ($3::text IS NULL OR o.observed_at      >= $3::timestamptz)
            ORDER BY o.agent_id, o.observation_type, date_trunc('second', o.observed_at),
                     o.observed_at DESC
        ),
        pe AS (
            SELECT DISTINCT ON (pe.agent_node_id, pe.event_label, date_trunc('second', pe.event_start))
                pe.id                                AS observation_id,
                pe.agent_node_id                     AS agent_id,
                an.node_name                         AS agent_name,
                pe.domain::text                      AS agent_type,
                pe.event_start                       AS observed_at,
                pe.event_label                       AS observation_type,
                CASE pe.confidence
                    WHEN 'high'     THEN 1.0
                    WHEN 'moderate' THEN 0.6
                    WHEN 'low'      THEN 0.3
                    ELSE 0.5
                END::float8                          AS confidence,
                pe.domain::text || ': ' || pe.event_label AS semantic_summary,
                pe.feature_snapshot::text::jsonb     AS raw_data
            FROM perceptual_events pe
            JOIN agent_nodes an ON an.id = pe.agent_node_id
            WHERE ($1::text IS NULL OR pe.domain::text   = $1)
              AND ($2::text IS NULL OR pe.event_label     = $2)
              AND ($3::text IS NULL OR pe.event_start    >= $3::timestamptz)
            ORDER BY pe.agent_node_id, pe.event_label, date_trunc('second', pe.event_start),
                     pe.event_start DESC
        )
        SELECT * FROM obs
        UNION ALL
        SELECT * FROM pe
        ORDER BY observed_at DESC
        LIMIT $4
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, agent_type, observation_type, since, limit)
    return [dict(r) for r in rows]


async def get_latest_observations(limit: int = 10) -> list[dict]:
    """Used by the live SSE feed — no filters, just the most recent."""
    pool = get_pool()
    sql = """
        SELECT * FROM (
            SELECT
                o.observation_id,
                o.agent_id,
                a.name          AS agent_name,
                a.agent_type,
                o.observed_at,
                o.observation_type,
                o.confidence::float8,
                o.semantic_summary
            FROM observations o
            JOIN agents a USING (agent_id)
            UNION ALL
            SELECT
                pe.id                                AS observation_id,
                pe.agent_node_id                     AS agent_id,
                an.node_name                         AS agent_name,
                pe.domain::text                      AS agent_type,
                pe.event_start                       AS observed_at,
                pe.event_label                       AS observation_type,
                CASE pe.confidence
                    WHEN 'high'     THEN 1.0
                    WHEN 'moderate' THEN 0.6
                    WHEN 'low'      THEN 0.3
                    ELSE 0.5
                END::float8                          AS confidence,
                pe.domain::text || ': ' || pe.event_label AS semantic_summary
            FROM perceptual_events pe
            JOIN agent_nodes an ON an.id = pe.agent_node_id
        ) combined
        ORDER BY observed_at DESC
        LIMIT $1
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, limit)
    return [dict(r) for r in rows]


async def get_observation_types() -> list[str]:
    pool = get_pool()
    sql = """
        SELECT DISTINCT observation_type FROM observations
        UNION
        SELECT DISTINCT event_label FROM perceptual_events
        ORDER BY observation_type
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return [r["observation_type"] for r in rows]


# ── Agents ─────────────────────────────────────────────────────────────────────

async def get_all_agents() -> list[dict]:
    pool = get_pool()
    sql = """
        SELECT * FROM (
            SELECT
                a.agent_id,
                a.agent_type,
                a.name,
                a.capabilities,
                a.location_context,
                a.birth_ts,
                a.last_active_ts,
                COUNT(o.observation_id) AS observation_count
            FROM agents a
            LEFT JOIN observations o USING (agent_id)
            GROUP BY a.agent_id
            UNION ALL
            SELECT
                an.id                                   AS agent_id,
                COALESCE(an.node_type, 'sensor_node')   AS agent_type,
                an.node_name                            AS name,
                an.metadata                             AS capabilities,
                an.location_label                       AS location_context,
                an.registered_at                        AS birth_ts,
                an.last_heartbeat_at                    AS last_active_ts,
                COUNT(pe.id)                            AS observation_count
            FROM agent_nodes an
            LEFT JOIN perceptual_events pe ON pe.agent_node_id = an.id
            GROUP BY an.id
        ) combined
        ORDER BY last_active_ts DESC NULLS LAST
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return [dict(r) for r in rows]


async def get_agent_by_id(agent_id: str) -> Optional[dict]:
    pool = get_pool()
    sql = """
        SELECT
            a.agent_id,
            a.agent_type,
            a.name,
            a.capabilities,
            a.location_context,
            a.birth_ts,
            a.last_active_ts
        FROM agents a
        WHERE a.agent_id = $1::uuid
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, agent_id)
    return dict(row) if row else None


async def register_agent_node(
    node_name: str,
    node_type: Optional[str] = None,
    location_label: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    import json as _json
    pool = get_pool()
    sql = """
        INSERT INTO agent_nodes (node_name, node_type, location_label, metadata)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (node_name) DO UPDATE
            SET node_type      = COALESCE(EXCLUDED.node_type,      agent_nodes.node_type),
                location_label = COALESCE(EXCLUDED.location_label, agent_nodes.location_label),
                metadata       = COALESCE(EXCLUDED.metadata,       agent_nodes.metadata)
        RETURNING id, node_name, node_type, location_label, registered_at
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            sql,
            node_name,
            node_type,
            location_label,
            _json.dumps(metadata or {}),
        )
    return dict(row)


async def get_agent_types() -> list[str]:
    pool = get_pool()
    sql = """
        SELECT DISTINCT agent_type FROM agents
        UNION
        SELECT DISTINCT domain::text FROM perceptual_events
        ORDER BY agent_type
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return [r["agent_type"] for r in rows]


# ── Node monitor ──────────────────────────────────────────────────────────────

async def get_all_node_states() -> list[dict]:
    """
    For every agent_node, return its latest embodied_state (motion),
    environmental_field (light), and high_bandwidth (thermal) events.
    Used by the monitor page.
    """
    pool = get_pool()
    sql = """
        WITH latest_motion AS (
            SELECT DISTINCT ON (agent_node_id)
                agent_node_id,
                event_label,
                confidence::text,
                event_start,
                feature_snapshot
            FROM perceptual_events
            WHERE domain = 'embodied_state'
            ORDER BY agent_node_id, event_start DESC
        ),
        latest_light AS (
            SELECT DISTINCT ON (agent_node_id)
                agent_node_id,
                event_label,
                confidence::text,
                event_start,
                feature_snapshot
            FROM perceptual_events
            WHERE domain = 'environmental_field'
            ORDER BY agent_node_id, event_start DESC
        ),
        latest_thermal AS (
            SELECT DISTINCT ON (agent_node_id)
                agent_node_id,
                event_label,
                confidence::text,
                event_start,
                feature_snapshot
            FROM perceptual_events
            WHERE domain = 'high_bandwidth'
            ORDER BY agent_node_id, event_start DESC
        )
        SELECT
            an.id::text                  AS node_id,
            an.node_name,
            an.node_type,
            an.location_label,
            an.last_heartbeat_at,
            an.metadata,
            lm.event_label               AS motion_label,
            lm.confidence                AS motion_confidence,
            lm.event_start               AS motion_at,
            lm.feature_snapshot          AS motion_raw,
            ll.event_label               AS light_label,
            ll.confidence                AS light_confidence,
            ll.event_start               AS light_at,
            ll.feature_snapshot          AS light_raw,
            lt.event_label               AS thermal_label,
            lt.confidence                AS thermal_confidence,
            lt.event_start               AS thermal_at,
            lt.feature_snapshot          AS thermal_raw
        FROM agent_nodes an
        LEFT JOIN latest_motion  lm ON lm.agent_node_id = an.id
        LEFT JOIN latest_light   ll ON ll.agent_node_id = an.id
        LEFT JOIN latest_thermal lt ON lt.agent_node_id = an.id
        ORDER BY an.last_heartbeat_at DESC NULLS LAST
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return [dict(r) for r in rows]


async def get_node_recent_events(node_name: str, limit: int = 30) -> list[dict]:
    """Recent events for a single node, newest first."""
    pool = get_pool()
    sql = """
        SELECT
            pe.id::text          AS event_id,
            pe.domain::text,
            pe.event_label,
            pe.confidence::text,
            pe.event_start,
            pe.feature_snapshot
        FROM perceptual_events pe
        JOIN agent_nodes an ON an.id = pe.agent_node_id
        WHERE an.node_name = $1
        ORDER BY pe.event_start DESC
        LIMIT $2
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, node_name, limit)
    return [dict(r) for r in rows]


# ── Semantic search ────────────────────────────────────────────────────────────

async def find_similar_observations(
    embedding: list[float],
    threshold: float = 0.70,
    limit: int = 10,
) -> list[dict]:
    """
    Find perceptual events whose stored embeddings are within `threshold`
    cosine similarity of the query embedding.
    The vector literal is interpolated directly to avoid asyncpg type-inference
    issues with the pgvector custom type.
    """
    pool = get_pool()
    vec_literal = "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"
    sql = f"""
        SELECT
            pe.id                                           AS observation_id,
            pe.agent_node_id                                AS agent_id,
            an.node_name                                    AS agent_name,
            pe.domain::text                                 AS agent_type,
            pe.event_start                                  AS observed_at,
            pe.event_label                                  AS observation_type,
            pe.domain::text || ': ' || pe.event_label       AS semantic_summary,
            CASE pe.confidence
                WHEN 'high'     THEN 1.0
                WHEN 'moderate' THEN 0.6
                WHEN 'low'      THEN 0.3
                ELSE 0.5
            END::float8                                     AS confidence,
            1 - (pe.embedding <=> '{vec_literal}'::vector)  AS similarity
        FROM perceptual_events pe
        JOIN agent_nodes an ON an.id = pe.agent_node_id
        WHERE pe.embedding IS NOT NULL
          AND 1 - (pe.embedding <=> '{vec_literal}'::vector) >= $1
        ORDER BY pe.embedding <=> '{vec_literal}'::vector
        LIMIT $2
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, threshold, limit)
    return [dict(r) for r in rows]


# ── Dashboard stats ────────────────────────────────────────────────────────────

async def get_dashboard_stats() -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        total_obs = await conn.fetchval(
            "SELECT (SELECT COUNT(*) FROM observations) + (SELECT COUNT(*) FROM perceptual_events)"
        )
        active_agents = await conn.fetchval(
            """
            SELECT COUNT(*) FROM (
                SELECT agent_id FROM observations
                WHERE observed_at > NOW() - INTERVAL '1 hour'
                UNION
                SELECT agent_node_id FROM perceptual_events
                WHERE event_start > NOW() - INTERVAL '1 hour'
            ) active
            """
        )
        total_agents = await conn.fetchval(
            "SELECT (SELECT COUNT(*) FROM agents) + (SELECT COUNT(*) FROM agent_nodes "
            "WHERE id NOT IN (SELECT agent_id FROM observations))"
        )
        obs_types = await conn.fetch(
            """
            SELECT observation_type, SUM(count) AS count FROM (
                SELECT observation_type, COUNT(*) AS count FROM observations
                GROUP BY observation_type
                UNION ALL
                SELECT event_label AS observation_type, COUNT(*) AS count FROM perceptual_events
                GROUP BY event_label
            ) combined
            GROUP BY observation_type
            ORDER BY count DESC
            LIMIT 10
            """
        )
        recent_obs = await conn.fetch(
            """
            SELECT agent_name, semantic_summary, observed_at FROM (
                SELECT a.name AS agent_name, o.semantic_summary, o.observed_at
                FROM observations o JOIN agents a USING (agent_id)
                UNION ALL
                SELECT an.node_name AS agent_name,
                       pe.domain::text || ': ' || pe.event_label AS semantic_summary,
                       pe.event_start AS observed_at
                FROM perceptual_events pe
                JOIN agent_nodes an ON an.id = pe.agent_node_id
            ) combined
            ORDER BY observed_at DESC
            LIMIT 5
            """
        )
    return {
        "total_observations": total_obs,
        "active_agents_1h":   active_agents,
        "total_agents":       total_agents,
        "observation_types":  [dict(r) for r in obs_types],
        "recent_observations": [dict(r) for r in recent_obs],
    }
