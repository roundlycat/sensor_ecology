# Relational Autonomous Agent Stack

Pi 5 · Whitehorse YT · 2026

A sensor ecology that bridges physical perception and linguistic motif space. Sensor events
are fused, embedded as natural language descriptions, and matched against a corpus of
linguistic motifs from an 18-month conversation archive. The result is an AR visualisation
in which linguistic attractors float in space around the agent, pulsing when physical events
resonate with them.

---

## Architecture

```
Hardware → Sensor Ingestion → Embedding Pipeline → PostgreSQL / pgvector
                                                           ↓
Unity AR ← PerceptualEventBus ← EventClient ← FastAPI Relay (port 8765)
```

**Four sensor domains:**

| Domain | Hardware | Poll rate |
|--------|----------|-----------|
| Environmental field | BME688, SHT35 | 10 s |
| Embodied state | ICM-42688-P, INA219, Pi 5 CPU/thermal | 2 s |
| Relational contact | VCNL4040, piezo via ADS1115 | 50 ms |
| High-bandwidth | Camera, thermal camera | Planned |

Key design principle: sensor events are serialised as structured natural language before
embedding. A pressure drop and the word *threshold* land in the same vector neighbourhood.
This is what makes cross-domain resonance meaningful.

---

## Prerequisites

- Raspberry Pi 5 (Linux dev machine works; pollers fall back to simulation without hardware)
- PostgreSQL 15+ with the `pgvector` extension
- Python 3.11+
- Ollama with `nomic-embed-text` running locally (768-dim embeddings)
- Unity 2022 LTS+ with AR Foundation 5.x, TextMeshPro, Input System

**Python packages:**

```bash
pip install fastapi uvicorn asyncpg aiohttp pydantic numpy psutil

# Hardware libraries — fail gracefully on dev machine, required on Pi:
pip install adafruit-circuitpython-bme680 adafruit-circuitpython-sht31d \
            adafruit-circuitpython-vcnl4040 adafruit-circuitpython-ads1x15 \
            adafruit-circuitpython-ina219

# Only needed if USE_LOCAL_EMBEDDER=false (remote OpenAI fallback):
pip install openai
```

---

## Setup

### 1. PostgreSQL + pgvector

```bash
# Debian / Ubuntu
sudo apt install postgresql-15-pgvector

createdb sensor_ecology
psql sensor_ecology -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql sensor_ecology -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'
```

### 2. Run schema migrations

Order matters — `schema.sql` defines the `motifs` table that `perceptual_schema_extension.sql`
references.

```bash
psql sensor_ecology -f schema.sql
psql sensor_ecology -f perceptual_schema_extension.sql
```

### 3. Register your agent node

Run once to get a stable node UUID:

```sql
INSERT INTO agent_nodes (node_name, node_type, location_label)
VALUES ('pi5-primary', 'pi5', 'desk')
RETURNING id;
```

Note the UUID — it becomes `AGENT_NODE_ID`.

### 4. Ollama + nomic-embed-text

```bash
# Install Ollama: https://ollama.com/download
curl -fsSL https://ollama.com/install.sh | sh

ollama pull nomic-embed-text

# Verify — should print 768:
curl -s http://localhost:11434/api/embeddings \
  -d '{"model":"nomic-embed-text","prompt":"test"}' | jq '.embedding | length'
```

### 5. Environment variables

```bash
export DATABASE_URL="postgresql://sean@localhost/sensor_ecology"
export AGENT_NODE_ID="<uuid from step 3>"
export USE_LOCAL_EMBEDDER="true"    # false = OpenAI (requires OPENAI_API_KEY)
```

Add to `/etc/environment` or a `.env` file for persistence.

### 6. Start the relay API

```bash
uvicorn relay_api:app --host 0.0.0.0 --port 8765
```

Verify:

```bash
curl http://localhost:8765/api/motifs
# {"motifs":[],"total":0,"bootstrap":true}
```

`bootstrap: true` is correct when the motifs table is empty — see
[Seeding the Motifs Table](#seeding-the-motifs-table).

### 7. Start sensor ingestion

```bash
python sensor_ingestion_layer.py
```

Without hardware, pollers run in simulation mode and emit synthetic readings. The full
pipeline (feature packing → embedding → resonance classification → DB write) runs normally.
Useful for end-to-end verification before attaching hardware.

---

## Running as systemd Services

Use `esp32-bridge.service` as a template. Create:

**`/etc/systemd/system/relay-api.service`**

```ini
[Unit]
Description=Agent Perception Relay API
After=network-online.target postgresql.service ollama.service

[Service]
User=sean
WorkingDirectory=/home/sean/sensor_ecology
Environment=DATABASE_URL=postgresql://sean@localhost/sensor_ecology
Environment=AGENT_NODE_ID=<your-uuid>
Environment=USE_LOCAL_EMBEDDER=true
ExecStart=/usr/bin/uvicorn relay_api:app --host 0.0.0.0 --port 8765
Restart=on-failure
RestartSec=5
TimeoutStartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/sensor-ingestion.service`**

```ini
[Unit]
Description=Sensor Ingestion Layer
After=network-online.target postgresql.service relay-api.service

[Service]
User=sean
WorkingDirectory=/home/sean/sensor_ecology
Environment=DATABASE_URL=postgresql://sean@localhost/sensor_ecology
Environment=AGENT_NODE_ID=<your-uuid>
Environment=USE_LOCAL_EMBEDDER=true
ExecStart=/usr/bin/python3 sensor_ingestion_layer.py
Restart=on-failure
RestartSec=5
TimeoutStartSec=120
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/drift-updater.service`**

```ini
[Unit]
Description=Motif Drift Updater
After=network-online.target postgresql.service sensor-ingestion.service

[Service]
User=sean
WorkingDirectory=/home/sean/sensor_ecology
Environment=DATABASE_URL=postgresql://sean@localhost/sensor_ecology
Environment=DRIFT_INTERVAL_S=3600
ExecStart=/usr/bin/python3 drift_updater.py --loop
Restart=on-failure
RestartSec=10
TimeoutStartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable relay-api sensor-ingestion drift-updater
sudo systemctl start relay-api sensor-ingestion drift-updater
```

---

## API Reference

All endpoints on `http://<pi-ip>:8765`.

### `GET /api/events`

Recent perceptual events, newest first. Unity polls at ~1 Hz.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `node_id` | UUID | — | Filter to one agent node |
| `domain` | string | — | Filter by sensor domain |
| `since` | ISO datetime | — | Cursor: return only events newer than this |
| `limit` | int | 50 | Max results (≤ 200) |

Response: `{events, total, cursor}`. Pass `cursor` back as `since` on the next request.

### `GET /api/events/stream`

Server-Sent Events stream. Delivers events as they are written to the DB, ~500 ms internal
poll. Connect once and receive push updates — do not poll this endpoint in a loop.

### `GET /api/agent/{node_id}/vitals`

Current metabolic state: power draw (mW), board temperature (°C), CPU load (%), last
heartbeat timestamp, online status (heartbeat within 30 s).

### `GET /api/motifs`

Motif list with physical resonance statistics. Called by `MotifGraphScene` on startup and
every `_refreshIntervalS` (default 30 s).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `min_recurrences` | int | 0 | Exclude motifs below this resonance count |
| `limit` | int | 100 | Max results (≤ 500) |

Response includes `bootstrap: true` when the motifs table itself is empty (not merely
filtered to zero). The Unity AR layer displays a "Seeding in progress" message in this case
rather than an empty scene with no explanation.

### `GET /api/motifs/{motif_id}/echoes`

Physical events that echo a given motif. Used by `MotifNode` to populate the echo panel
when a node is tapped.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `threshold` | float | 0.25 | Max cosine distance to include (≤ 1.0) |
| `limit` | int | 20 | Max results (≤ 100) |

### `GET /api/motifs/{motif_id}/stats`

Domain breakdown for a motif: resonance counts and average cosine distance per sensor
domain. Used by `MotifNode.FetchAndBuildRing` to colour the arc ring around each node.
Returns `{motif_id, domain_breakdown: [{domain, count, avg_distance}]}`.

---

## Unity Setup

For prefab construction, layer setup, material configuration, and full inspector wiring
see [`MOTIF_GRAPH_SCENE_SETUP.md`](MOTIF_GRAPH_SCENE_SETUP.md).

**Pre-flight checklist:**

- [ ] Pi is on the same network; relay is running and responding
- [ ] `_relayHost` in `MotifGraphScene` inspector set to Pi's local IP
- [ ] `_relayPort` set to `8765`
- [ ] `MotifNode` layer created in **Edit → Project Settings → Tags and Layers**
- [ ] `_nodeLayer` mask in `MotifGraphScene` includes the `MotifNode` layer
- [ ] `_motifNodePrefab` assigned (built per MOTIF_GRAPH_SCENE_SETUP.md Step 3)
- [ ] `_resonanceRenderer` reference set (not null)
- [ ] `_statusText` assigned if you want the bootstrap / error state visible in AR

---

## Seeding the Motifs Table

The AR graph shows no nodes until the `motifs` table is populated. `bootstrap: true` in
the `/api/motifs` response confirms this is the reason.

Seed a test entry to verify the full pipeline:

```sql
INSERT INTO motifs (label, source_corpus)
VALUES ('test motif — warmth and effort', 'manual_seed');
```

A `centroid_embedding` must be set before the motif appears in resonance queries. To embed
a label and store it:

```python
# seed_motif.py — run once per motif
import asyncio, aiohttp, asyncpg, os

async def seed(label: str):
    async with aiohttp.ClientSession() as s:
        r = await s.post(
            "http://localhost:11434/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": label},
        )
        emb = (await r.json())["embedding"]

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    vec  = "[" + ",".join(str(v) for v in emb) + "]"
    await pool.execute(
        "UPDATE motifs SET centroid_embedding = $1::vector WHERE label = $2",
        vec, label,
    )
    await pool.close()
    print(f"Seeded: {label}")

asyncio.run(seed("test motif — warmth and effort"))
```

After seeding, tap **Refresh** in the Unity inspector or wait 30 s for the auto-refresh
cycle. The `bootstrap` flag will be `false` and the node will appear in the graph.

---

## Maintenance

### Checking service health

```bash
# Relay
curl http://localhost:8765/api/motifs | jq '{bootstrap, total}'

# Logs
journalctl -u relay-api -f
journalctl -u sensor-ingestion -f

# Recent perceptual events
psql sensor_ecology -c "
  SELECT domain, event_label, confidence, event_start
  FROM perceptual_events
  ORDER BY event_start DESC LIMIT 10;"

# Resonance activity by type
psql sensor_ecology -c "
  SELECT resonance_type, COUNT(*)
  FROM motif_resonance
  GROUP BY resonance_type;"
```

### Monitoring the embedding pipeline

```bash
# Ollama responsive?
curl -s http://localhost:11434/api/embeddings \
  -d '{"model":"nomic-embed-text","prompt":"test"}' | jq '.embedding | length'
# Expected: 768

# Events without embeddings (pipeline stalled or Ollama down)
psql sensor_ecology -c "
  SELECT COUNT(*) FROM perceptual_events WHERE embedding IS NULL;"
```

### Sensor baseline warm-up

The rolling baseline requires at least 3 readings per channel before threshold detection
activates (default window is 20 readings). On a fresh start no events fire for the first
30–200 seconds depending on domain poll rate. This is expected — check logs for
`Poller started:` messages to confirm the pollers are running.

### Per-domain cooldowns

Cooldowns prevent event flooding after a large deviation:

| Domain | Cooldown |
|--------|----------|
| Environmental | 15 s |
| Embodied | 5 s |
| Relational | 1 s |

If events appear to be missing, the cooldown may be suppressing them. Confirmed events
appear in logs as `Event detected: <domain> / <label>`.

### Sensor calibration flag

When BME688 and SHT35 temperature readings diverge by more than 1 °C, the SHT35 row is
stored with `quality_flag = 1`. Check with:

```sql
SELECT recorded_at, raw_value
FROM sensor_readings
WHERE sensor_label = 'SHT35' AND quality_flag = 1
ORDER BY recorded_at DESC LIMIT 20;
```

### Database size

`sensor_readings` grows fastest (one row per channel per poll cycle). Prune periodically:

```sql
-- Keep 7 days of raw readings
DELETE FROM sensor_readings
WHERE recorded_at < NOW() - INTERVAL '7 days';
```

`perceptual_events` holds 768-dim vectors (~3 KB each). HNSW index maintenance runs via
autovacuum. If query latency rises, run `REINDEX INDEX CONCURRENTLY
idx_perceptual_events_embedding;`.

---

## Troubleshooting

**Relay returns 404 on `/api/motifs`**
Verify you are running the current `relay_api.py` — versions prior to the MotifModule
merge did not include this endpoint.

**Unity shows empty AR scene with no status message**
`_statusText` is not assigned in the `MotifGraphScene` inspector. Assign a TextMeshPro
component, or check the Console for `[MotifGraphScene] Bootstrap state:` to confirm the
relay is responding correctly.

**Graph is empty, `bootstrap` is `false`**
Motifs exist in the table but have no `centroid_embedding` set. See
[Seeding the Motifs Table](#seeding-the-motifs-table). Confirm with:

```sql
SELECT label, centroid_embedding IS NOT NULL AS has_embedding FROM motifs;
```

**Events fire but no resonances are recorded**
No motif has a non-null `centroid_embedding`. The classifier returns no rows and writes
nothing to `motif_resonance`. Same fix as above.

**Agent vitals are `null` in all events**
`EmbodiedStatePoller` is not running or is failing silently on hardware init. Check
`journalctl -u sensor-ingestion` for `INA219 init failed` or `IMU init failed`. The poller
falls back to simulation mode (IMU only) in this case — INA219 power readings will be
absent.

**Domain breakdown ring does not appear on motif nodes**
`_ringSegmentPrefab` is not assigned in the `MotifNode` prefab inspector, or
`ProceduralArcMesh` component is missing from the prefab. See MOTIF_GRAPH_SCENE_SETUP.md
Step 2.

---

## Deprecated Components

**`esp32_bridge.py`** — Original ingestion path: MQTT → psycopg2 → `agents` /
`observations` tables using 384-dim MiniLM embeddings. Superseded by
`sensor_ingestion_layer.py` and the perceptual schema. The `agents` and `observations`
tables remain in `schema.sql` for historical queries but are no longer written to.
Do not attempt to bridge 384-dim MiniLM embeddings into the 768-dim nomic-embed-text
motif space — the coordinate systems are incompatible.

**`mqtt_bus.py`** — MQTT abstraction used by the ESP32 bridge. No longer part of the
active ingestion path.

---

## Planned Components

| Component | File | Notes |
|-----------|------|-------|
| ~~Motif Drift Updater~~ | `drift_updater.py` | Built. EMA-based centroid drift with linguistic anchor clamping. |
| High-bandwidth poller | `sensor_ingestion_layer.py` | Camera and thermal camera ingestion to complete the fourth sensor domain. |
| Thermal comms protocol | `thermal_comms.py` | CPU load modulation as inter-agent metabolic signal. Thermal Morse / IR glyphs. |
| Full motif corpus | — | 18-month conversation archive embedded and loaded into the `motifs` table via the motif ecology pipeline. |

---

*Relational Autonomous Agent Stack · Pi 5 · Whitehorse YT · 2026*
