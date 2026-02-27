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
            SELECT
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
        ),
        pe AS (
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
                pe.domain::text || ': ' || pe.event_label AS semantic_summary,
                pe.feature_snapshot::text::jsonb     AS raw_data
            FROM perceptual_events pe
            JOIN agent_nodes an ON an.id = pe.agent_node_id
            WHERE ($1::text IS NULL OR pe.domain::text   = $1)
              AND ($2::text IS NULL OR pe.event_label     = $2)
              AND ($3::text IS NULL OR pe.event_start    >= $3::timestamptz)
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
        ORDER BY a.last_active_ts DESC NULLS LAST
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


# ── Semantic search ────────────────────────────────────────────────────────────

async def find_similar_observations(
    embedding: list[float],
    threshold: float = 0.70,
    limit: int = 10,
) -> list[dict]:
    """
    Call the pgvector similar_observations() function defined in schema.sql.
    The embedding is passed as a vector literal string and cast inside the SQL
    to avoid asyncpg type-inference issues with the custom vector type.
    """
    pool = get_pool()
    vec_literal = "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"
    sql = f"""
        SELECT
            s.observation_id,
            s.agent_id,
            a.name        AS agent_name,
            a.agent_type,
            s.observed_at,
            s.observation_type,
            s.semantic_summary,
            s.confidence,
            s.similarity
        FROM similar_observations('{vec_literal}'::vector, $1, $2) s
        JOIN agents a USING (agent_id)
        ORDER BY s.similarity DESC
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
