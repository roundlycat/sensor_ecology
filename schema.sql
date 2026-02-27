-- Sensor Ecology: Agent-Centric Schema
-- Run with: psql -U sean -d sensor_ecology -f schema.sql

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- ── Agents ────────────────────────────────────────────────────────────────────
-- Each physical node or software process is an agent.
-- Agents register themselves; the database doesn't define what they are.

CREATE TABLE IF NOT EXISTS agents (
    agent_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_type      VARCHAR(50) NOT NULL,   -- 'thermal_node', 'vision_processor', 'rfid_reader', 'pi_coordinator'
    name            TEXT,                   -- human-readable label e.g. 'lab2-thermal'
    capabilities    JSONB DEFAULT '{}',     -- what this agent can do/sense
    location_context TEXT,                  -- free text: "Lab 2 north wall", "main entrance"
    birth_ts        TIMESTAMPTZ DEFAULT NOW(),
    last_active_ts  TIMESTAMPTZ DEFAULT NOW()
);

-- ── Observations ──────────────────────────────────────────────────────────────
-- Agents publish interpretations, not raw sensor dumps.
-- The agent decides what is worth saying.

CREATE TABLE IF NOT EXISTS observations (
    observation_id  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id        UUID NOT NULL REFERENCES agents(agent_id),
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    observation_type VARCHAR(100) NOT NULL, -- 'thermal_anomaly', 'occupancy_detected', 'object_identified'
    confidence      FLOAT CHECK (confidence BETWEEN 0 AND 1),
    semantic_summary TEXT NOT NULL,         -- agent's natural language interpretation
    embedding       VECTOR(384),            -- semantic embedding for similarity search
    raw_data        JSONB                   -- optional: agent stores raw data if it chooses
);

CREATE INDEX IF NOT EXISTS idx_obs_agent    ON observations(agent_id);
CREATE INDEX IF NOT EXISTS idx_obs_time     ON observations(observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_type     ON observations(observation_type);
-- Vector similarity index (cosine distance)
CREATE INDEX IF NOT EXISTS idx_obs_embedding ON observations
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);

-- ── Coordination Events ───────────────────────────────────────────────────────
-- Records when agents queried each other or shared patterns.
-- Preserves the "who talked to who" history.

CREATE TABLE IF NOT EXISTS coordination_events (
    event_id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    initiating_agent    UUID NOT NULL REFERENCES agents(agent_id),
    responding_agents   UUID[],
    coordination_type   VARCHAR(50) NOT NULL, -- 'query', 'alert', 'pattern_share', 'refusal'
    conversation_summary TEXT,
    occurred_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_coord_initiator ON coordination_events(initiating_agent);
CREATE INDEX IF NOT EXISTS idx_coord_time      ON coordination_events(occurred_at DESC);

-- ── Emergent Patterns ─────────────────────────────────────────────────────────
-- Patterns nobody programmed — discovered by agents finding unexpected correlations.

CREATE TABLE IF NOT EXISTS emergent_patterns (
    pattern_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    discovered_at           TIMESTAMPTZ DEFAULT NOW(),
    discovering_agents      UUID[],
    pattern_description     TEXT NOT NULL,
    supporting_observations UUID[],
    confidence_evolution    JSONB DEFAULT '[]', -- [{ts, confidence}, ...] history
    embedding               VECTOR(384)
);

CREATE INDEX IF NOT EXISTS idx_pattern_embedding ON emergent_patterns
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);

-- ── Helper: find similar observations ────────────────────────────────────────
-- Usage: SELECT * FROM similar_observations('<embedding>', 0.8, 10);

CREATE OR REPLACE FUNCTION similar_observations(
    query_embedding VECTOR(384),
    similarity_threshold FLOAT DEFAULT 0.75,
    result_limit INT DEFAULT 20
)
RETURNS TABLE (
    observation_id  UUID,
    agent_id        UUID,
    observed_at     TIMESTAMPTZ,
    observation_type VARCHAR(100),
    semantic_summary TEXT,
    confidence      FLOAT,
    similarity      FLOAT
)
LANGUAGE sql STABLE AS $$
    SELECT
        observation_id,
        agent_id,
        observed_at,
        observation_type,
        semantic_summary,
        confidence,
        1 - (embedding <=> query_embedding) AS similarity
    FROM observations
    WHERE embedding IS NOT NULL
      AND 1 - (embedding <=> query_embedding) >= similarity_threshold
    ORDER BY embedding <=> query_embedding
    LIMIT result_limit;
$$;

-- ── Motifs ────────────────────────────────────────────────────────────────────
-- Linguistic motifs extracted from the 18-month conversation corpus.
-- The centroid_embedding is updated by MotifDriftUpdater as new perceptual
-- events accrete. Each row is a named semantic attractor in embedding space.

CREATE TABLE IF NOT EXISTS motifs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    label               TEXT,                       -- short human-readable phrase
    centroid_embedding  VECTOR(768),                -- nomic-embed-text centroid
    source_corpus       TEXT,                       -- e.g. 'conversation_archive_2024'
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_motifs_embedding ON motifs
    USING hnsw (centroid_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Grant all privileges to sean
GRANT ALL ON ALL TABLES IN SCHEMA public TO sean;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO sean;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO sean;
