-- =============================================================================
-- Motif-Aware Perceptual Event Schema Extension
-- Extends existing motif tracking infrastructure with physical perception
-- Assumes: motifs table, motif_occurrences table, agents/nodes table
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Sensor domain taxonomy
-- Mirrors the four perceptual domains from the Canonical Sensor Spine v1.0
-- ---------------------------------------------------------------------------

CREATE TYPE sensor_domain AS ENUM (
    'environmental_field',   -- BME688, SHT35, TSL2591, AS7341, SGP40
    'embodied_state',        -- ICM-42688-P, INA219/226, thermistors
    'relational_contact',    -- piezo, VCNL4040, capacitive, FSR
    'high_bandwidth'         -- camera, thermal camera, microphone
);

-- Confidence band for fusion quality
CREATE TYPE fusion_confidence AS ENUM (
    'high',      -- multiple sensors in agreement, low noise
    'moderate',  -- partial agreement or single-sensor
    'low',       -- conflicting sensors or high noise floor
    'synthetic'  -- computed / inferred rather than directly measured
);

-- ---------------------------------------------------------------------------
-- Agent / node registry
-- If you already have this, add missing columns rather than recreating
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_nodes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_name           TEXT NOT NULL UNIQUE,
    node_type           TEXT,                          -- 'pi5', 'esp32', 'virtual', etc.
    location_label      TEXT,                          -- human-readable: 'workshop', 'desk'
    latitude            DOUBLE PRECISION,
    longitude           DOUBLE PRECISION,
    registered_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat_at   TIMESTAMPTZ,
    metadata            JSONB DEFAULT '{}'
);

-- ---------------------------------------------------------------------------
-- Raw sensor readings
-- Normalized, typed, one row per sensor per reading cycle
-- ---------------------------------------------------------------------------

CREATE TABLE sensor_readings (
    id              BIGSERIAL PRIMARY KEY,
    agent_node_id   UUID NOT NULL REFERENCES agent_nodes(id) ON DELETE CASCADE,
    domain          sensor_domain NOT NULL,
    sensor_label    TEXT NOT NULL,     -- e.g. 'BME688', 'ICM-42688-P', 'INA219'
    channel         TEXT,              -- e.g. 'temperature', 'voc_index', 'current_mA'
    raw_value       DOUBLE PRECISION,
    unit            TEXT,              -- SI units: 'degC', 'hPa', 'mA', 'lux', etc.
    quality_flag    SMALLINT DEFAULT 0,  -- 0=ok, 1=suspect, 2=failed
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sensor_readings_node_time
    ON sensor_readings (agent_node_id, recorded_at DESC);

CREATE INDEX idx_sensor_readings_domain_time
    ON sensor_readings (domain, recorded_at DESC);

-- ---------------------------------------------------------------------------
-- Fused perceptual events
-- Core join point between hardware and motif space
-- ---------------------------------------------------------------------------

CREATE TABLE perceptual_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_node_id       UUID NOT NULL REFERENCES agent_nodes(id) ON DELETE CASCADE,

    -- Perceptual framing
    domain              sensor_domain NOT NULL,
    event_label         TEXT,              -- human-readable tag: 'thermal_spike', 'tap_sequence'
    confidence          fusion_confidence NOT NULL DEFAULT 'moderate',

    -- Source readings that contributed to this event (array of FK ids)
    source_reading_ids  BIGINT[],

    -- Raw feature vector before embedding (compact snapshot of fused values)
    feature_snapshot    JSONB NOT NULL DEFAULT '{}',

    -- Embedding for motif-space proximity queries
    embedding           vector(768),       -- nomic-embed-text (local Ollama)

    -- Temporal
    event_start         TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_end           TIMESTAMPTZ,       -- null = instantaneous
    duration_ms         INTEGER GENERATED ALWAYS AS (
                            EXTRACT(MILLISECONDS FROM (event_end - event_start))::INTEGER
                        ) STORED,

    -- Cross-domain flag: did this event involve more than one sensor domain?
    is_cross_domain     BOOLEAN NOT NULL DEFAULT FALSE,
    domains_involved    sensor_domain[],

    -- Agent's internal state at time of event (metabolic context)
    agent_power_mW      DOUBLE PRECISION,
    agent_temp_c        DOUBLE PRECISION,
    agent_cpu_load_pct  SMALLINT,

    metadata            JSONB DEFAULT '{}'
);

CREATE INDEX idx_perceptual_events_node_time
    ON perceptual_events (agent_node_id, event_start DESC);

CREATE INDEX idx_perceptual_events_domain
    ON perceptual_events (domain, event_start DESC);

-- Vector index for motif-space nearest-neighbour queries
-- Use HNSW for fast approximate search on the Pi; ivfflat if memory is tight
CREATE INDEX idx_perceptual_events_embedding
    ON perceptual_events USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------------------
-- Motif resonance log
-- Records the nearest motif(s) found at the moment an event was observed
-- This is the empirical record of physical<->linguistic motif convergence
-- ---------------------------------------------------------------------------

CREATE TABLE motif_resonance (
    id                      BIGSERIAL PRIMARY KEY,
    perceptual_event_id     UUID NOT NULL REFERENCES perceptual_events(id) ON DELETE CASCADE,

    motif_id                UUID NOT NULL REFERENCES motifs(id) ON DELETE CASCADE,

    -- How close was the event embedding to this motif's centroid?
    cosine_distance         DOUBLE PRECISION NOT NULL,
    is_nearest              BOOLEAN NOT NULL DEFAULT FALSE,   -- TRUE for the closest match only

    -- Was this a genuine recurrence or a candidate new motif?
    resonance_type          TEXT NOT NULL CHECK (resonance_type IN (
                                'recurrence',    -- within threshold of existing motif
                                'candidate',     -- novel enough to potentially seed a new motif
                                'weak_echo'      -- detectable but below recurrence threshold
                            )),

    -- Threshold used at time of classification (lets you retro-adjust)
    distance_threshold_used DOUBLE PRECISION,

    -- Optional: which linguistic/conversational context was this motif drawn from?
    source_conversation_id  TEXT,           -- FK to your conversation tracking if applicable

    observed_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_motif_resonance_event
    ON motif_resonance (perceptual_event_id);

CREATE INDEX idx_motif_resonance_motif_time
    ON motif_resonance (motif_id, observed_at DESC);

-- ---------------------------------------------------------------------------
-- Cross-agent relational events
-- When one agent perceives another (thermal, proximity, tap language, etc.)
-- ---------------------------------------------------------------------------

CREATE TABLE relational_contact_events (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    observer_node_id        UUID NOT NULL REFERENCES agent_nodes(id),
    observed_node_id        UUID REFERENCES agent_nodes(id),  -- null if unidentified
    perceptual_event_id     UUID NOT NULL REFERENCES perceptual_events(id),

    contact_modality        TEXT NOT NULL,   -- 'thermal', 'proximity', 'tap', 'visual', 'acoustic'
    contact_quality         fusion_confidence NOT NULL,

    -- Metabolic reading of the observed agent at time of contact
    observed_thermal_c      DOUBLE PRECISION,
    observed_cpu_load_pct   SMALLINT,

    -- Did the observer recognise a motif in what it perceived?
    recognized_motif_id     UUID REFERENCES motifs(id) ON DELETE SET NULL,
    recognition_confidence  DOUBLE PRECISION,

    contacted_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata                JSONB DEFAULT '{}'
);

CREATE INDEX idx_relational_contact_observer_time
    ON relational_contact_events (observer_node_id, contacted_at DESC);

CREATE INDEX idx_relational_contact_observed
    ON relational_contact_events (observed_node_id, contacted_at DESC);

-- ---------------------------------------------------------------------------
-- Motif drift tracking (physical dimension)
-- Extends your existing linguistic drift tracking to physical perception
-- Records how a motif's centroid moves as new perceptual events accrete
-- ---------------------------------------------------------------------------

CREATE TABLE perceptual_motif_drift (
    id                  BIGSERIAL PRIMARY KEY,
    motif_id            UUID NOT NULL REFERENCES motifs(id) ON DELETE CASCADE,
    agent_node_id       UUID REFERENCES agent_nodes(id),  -- null = global/aggregate

    -- Centroid before and after incorporating new perceptual events
    centroid_before     vector(768),
    centroid_after      vector(768),
    drift_magnitude     DOUBLE PRECISION GENERATED ALWAYS AS (
                            -- placeholder; compute in application layer
                            -- pgvector doesn't support column expressions across vector ops natively
                            NULL
                        ) STORED,

    -- What triggered the drift update?
    trigger_event_id    UUID REFERENCES perceptual_events(id),
    n_events_included   INTEGER,               -- running count used to compute new centroid

    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_perceptual_motif_drift_motif_time
    ON perceptual_motif_drift (motif_id, computed_at DESC);

-- ---------------------------------------------------------------------------
-- Convenience view: recent events with nearest motif
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_recent_perceptual_events AS
SELECT
    pe.id,
    an.node_name,
    pe.domain,
    pe.event_label,
    pe.confidence,
    pe.event_start,
    pe.is_cross_domain,
    pe.domains_involved,
    pe.agent_cpu_load_pct,
    pe.agent_temp_c,
    pe.agent_power_mW,
    mr.motif_id         AS nearest_motif_id,
    mr.cosine_distance  AS nearest_motif_distance,
    mr.resonance_type
FROM
    perceptual_events pe
    JOIN agent_nodes an ON an.id = pe.agent_node_id
    LEFT JOIN motif_resonance mr
        ON mr.perceptual_event_id = pe.id
        AND mr.is_nearest = TRUE
ORDER BY
    pe.event_start DESC;

-- ---------------------------------------------------------------------------
-- Suggested function: find perceptual events near a motif embedding
-- Call this when a linguistic motif is updated to check for physical echoes
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION find_perceptual_echoes(
    query_embedding     vector(768),
    distance_threshold  DOUBLE PRECISION DEFAULT 0.25,
    result_limit        INTEGER DEFAULT 20
)
RETURNS TABLE (
    event_id            UUID,
    node_name           TEXT,
    domain              sensor_domain,
    event_label         TEXT,
    event_start         TIMESTAMPTZ,
    cosine_distance     DOUBLE PRECISION
)
LANGUAGE sql STABLE AS $$
    SELECT
        pe.id,
        an.node_name,
        pe.domain,
        pe.event_label,
        pe.event_start,
        (pe.embedding <=> query_embedding) AS cosine_distance
    FROM
        perceptual_events pe
        JOIN agent_nodes an ON an.id = pe.agent_node_id
    WHERE
        pe.embedding IS NOT NULL
        AND (pe.embedding <=> query_embedding) < distance_threshold
    ORDER BY
        cosine_distance ASC
    LIMIT result_limit;
$$;
