"""
All database queries for the dashboard.
Each function acquires a connection from the pool, runs its query, and returns
plain Python dicts so callers are not coupled to asyncpg Record objects.
"""

from typing import Optional

from app.db.connection import get_pool


# ── Observations (unified: legacy + perceptual) ────────────────────────────────

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


# ── Perceptual events (new schema, richer) ────────────────────────────────────

async def get_perceptual_events(
    limit: int = 50,
    domain: Optional[str] = None,
    since: Optional[str] = None,
) -> list[dict]:
    """Rich perceptual events with vitals and nearest resonance."""
    pool = get_pool()
    sql = """
        SELECT
            pe.id::text,
            an.node_name,
            pe.domain::text,
            pe.event_label,
            pe.confidence::text,
            pe.event_start,
            pe.is_cross_domain,
            pe.domains_involved::text[],
            pe.agent_power_mw,
            pe.agent_temp_c,
            pe.agent_cpu_load_pct,
            pe.feature_snapshot,
            mr.cosine_distance   AS nearest_motif_distance,
            mr.resonance_type,
            m.label              AS nearest_motif_label
        FROM perceptual_events pe
        JOIN agent_nodes an ON an.id = pe.agent_node_id
        LEFT JOIN motif_resonance mr
            ON mr.perceptual_event_id = pe.id AND mr.is_nearest = TRUE
        LEFT JOIN motifs m ON m.id = mr.motif_id
        WHERE ($1::text IS NULL OR pe.domain::text = $1)
          AND ($2::text IS NULL OR pe.event_start >= $2::timestamptz)
        ORDER BY pe.event_start DESC
        LIMIT $3
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, domain, since, limit)
    return [dict(r) for r in rows]


async def get_domain_activity(hours: int = 24) -> list[dict]:
    """Event counts by domain over the last N hours, in 1-hour buckets."""
    pool = get_pool()
    sql = """
        SELECT
            date_trunc('hour', event_start)  AS bucket,
            domain::text,
            COUNT(*)                          AS event_count,
            AVG(CASE confidence
                WHEN 'high'     THEN 1.0
                WHEN 'moderate' THEN 0.6
                WHEN 'low'      THEN 0.3
                ELSE 0.5
            END)                              AS avg_confidence
        FROM perceptual_events
        WHERE event_start >= NOW() - ($1 || ' hours')::INTERVAL
        GROUP BY bucket, domain
        ORDER BY bucket ASC, domain
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, str(hours))
    return [dict(r) for r in rows]


async def get_ecology_state() -> dict:
    """
    Current state of the ecology: latest reading per domain,
    Pi vitals, and most recently resonated motif.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        # Latest event per domain
        domain_latest = await conn.fetch("""
            SELECT DISTINCT ON (domain)
                domain::text,
                event_label,
                confidence::text,
                event_start,
                agent_temp_c,
                agent_power_mw,
                agent_cpu_load_pct,
                feature_snapshot
            FROM perceptual_events
            ORDER BY domain, event_start DESC
        """)

        # Most recent Pi vitals (any event with vitals)
        vitals = await conn.fetchrow("""
            SELECT
                agent_temp_c,
                agent_power_mw,
                agent_cpu_load_pct,
                event_start AS recorded_at
            FROM perceptual_events
            WHERE agent_temp_c IS NOT NULL
            ORDER BY event_start DESC
            LIMIT 1
        """)

        # Most recently resonated motif
        latest_resonance = await conn.fetchrow("""
            SELECT
                m.label,
                mr.cosine_distance,
                mr.resonance_type,
                mr.observed_at,
                pe.domain::text
            FROM motif_resonance mr
            JOIN motifs m ON m.id = mr.motif_id
            JOIN perceptual_events pe ON pe.id = mr.perceptual_event_id
            ORDER BY mr.observed_at DESC
            LIMIT 1
        """)

        # Active motifs in the last hour
        active_motifs = await conn.fetch("""
            SELECT
                m.label,
                COUNT(mr.id)            AS echo_count,
                MIN(mr.cosine_distance) AS closest_distance,
                MAX(mr.observed_at)     AS last_resonance,
                pe.domain::text         AS dominant_domain
            FROM motif_resonance mr
            JOIN motifs m ON m.id = mr.motif_id
            JOIN perceptual_events pe ON pe.id = mr.perceptual_event_id
            WHERE mr.observed_at >= NOW() - INTERVAL '1 hour'
            GROUP BY m.label, pe.domain
            ORDER BY echo_count DESC
            LIMIT 5
        """)

        # Total event counts today
        totals = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE event_start >= NOW() - INTERVAL '24 hours')  AS today,
                COUNT(*) FILTER (WHERE event_start >= NOW() - INTERVAL '1 hour')    AS last_hour,
                COUNT(*) FILTER (WHERE event_start >= NOW() - INTERVAL '5 minutes') AS last_5min
            FROM perceptual_events
        """)

    return {
        "domain_latest":     [dict(r) for r in domain_latest],
        "vitals":            dict(vitals) if vitals else None,
        "latest_resonance":  dict(latest_resonance) if latest_resonance else None,
        "active_motifs":     [dict(r) for r in active_motifs],
        "totals":            dict(totals) if totals else {},
    }


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


async def get_agent_nodes() -> list[dict]:
    pool = get_pool()
    sql = """
        SELECT
            an.id::text,
            an.node_name,
            an.node_type,
            an.location_label,
            an.registered_at,
            an.last_heartbeat_at,
            COUNT(pe.id)   AS event_count,
            MAX(pe.event_start) AS last_event
        FROM agent_nodes an
        LEFT JOIN perceptual_events pe ON pe.agent_node_id = an.id
        GROUP BY an.id
        ORDER BY last_event DESC NULLS LAST
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return [dict(r) for r in rows]


# ── Motifs ─────────────────────────────────────────────────────────────────────

async def get_active_motifs(limit: int = 30) -> list[dict]:
    """Motifs with resonance activity, ordered by recency."""
    pool = get_pool()
    sql = """
        SELECT
            m.id::text,
            m.label,
            COUNT(mr.id)              AS resonance_count,
            MIN(mr.cosine_distance)   AS min_distance,
            AVG(mr.cosine_distance)   AS avg_distance,
            MAX(mr.observed_at)       AS last_resonance,
            (m.centroid_embedding IS NOT NULL) AS has_embedding,
            -- dominant domain
            (
                SELECT pe2.domain::text
                FROM motif_resonance mr2
                JOIN perceptual_events pe2 ON pe2.id = mr2.perceptual_event_id
                WHERE mr2.motif_id = m.id
                GROUP BY pe2.domain
                ORDER BY COUNT(*) DESC
                LIMIT 1
            ) AS dominant_domain
        FROM motifs m
        LEFT JOIN motif_resonance mr ON mr.motif_id = m.id
        GROUP BY m.id
        HAVING COUNT(mr.id) > 0
        ORDER BY last_resonance DESC NULLS LAST
        LIMIT $1
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, limit)
    return [dict(r) for r in rows]


async def get_motif_echoes(motif_id: str, limit: int = 20) -> list[dict]:
    """Recent perceptual events that echoed a given motif."""
    pool = get_pool()
    sql = """
        SELECT
            pe.id::text,
            an.node_name,
            pe.domain::text,
            pe.event_label,
            pe.event_start,
            mr.cosine_distance,
            mr.resonance_type
        FROM motif_resonance mr
        JOIN perceptual_events pe ON pe.id = mr.perceptual_event_id
        JOIN agent_nodes an ON an.id = pe.agent_node_id
        WHERE mr.motif_id = $1::uuid
        ORDER BY mr.observed_at DESC
        LIMIT $2
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, motif_id, limit)
    return [dict(r) for r in rows]


# ── Semantic search ────────────────────────────────────────────────────────────

async def find_similar_observations(
    embedding: list[float],
    threshold: float = 0.70,
    limit: int = 10,
) -> list[dict]:
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
            LIMIT 10
            """
        )
    return {
        "total_observations": total_obs,
        "active_agents_1h":   active_agents,
        "total_agents":       total_agents,
        "observation_types":  [dict(r) for r in obs_types],
        "recent_observations": [dict(r) for r in recent_obs],
    }
