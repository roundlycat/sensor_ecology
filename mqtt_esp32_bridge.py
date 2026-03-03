"""
mqtt_esp32_bridge.py — MQTT → PostgreSQL bridge for ESP32 sensor nodes.

Subscribes to:
  agents/registration          ← node self-registration (retained)
  agents/+/interpretation      ← classified sensor observations
  agents/+/status              ← heartbeat (RSSI, uptime, buf)

Writes to:
  agent_nodes       (upsert on registration / heartbeat)
  perceptual_events (one row per interpretation message)

Run:
  python mqtt_esp32_bridge.py

Env vars (all optional, defaults shown):
  MQTT_HOST      localhost
  MQTT_PORT      1883
  DB_DSN         postgresql://sean:ecology@localhost/sensor_ecology
"""

import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone

import asyncpg
import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("esp32-bridge")

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
DB_DSN    = os.environ.get("DB_DSN", "postgresql://sean:ecology@localhost/sensor_ecology")

TOPICS = [
    ("agents/registration",     0),   # env-node-01 style (no node id in path)
    ("agents/+/registration",   0),   # nodemcu/bme280 style (node id in path)
    ("agents/+/interpretation", 0),
    ("agents/+/observation",    0),   # bme280_node legacy observation format
    ("agents/+/status",         0),
]

# sensor → perceptual domain enum
DOMAIN_MAP = {
    "tcs34725": "environmental_field",
    "mpu6050":  "embodied_state",
}

# observation_type → perceptual domain for legacy observation messages
OBS_DOMAIN_MAP = {
    "bme280_node":      "environmental_field",
    "envirophat_node":  "environmental_field",
    "esp32_node":       "environmental_field",
}


def conf_level(value: float) -> str:
    """Map a float confidence (0–1) to the fusion_confidence enum."""
    if value >= 0.75:
        return "high"
    if value >= 0.40:
        return "moderate"
    return "low"


# Unity node state mapping — observation label → Unity state string.
# "state" drives rendering (idle=dim, active=pulse, error=alert colour).
# "value" (0-1 confidence) drives the scale of the visual response.
_UNITY_STATE_MAP = {
    "idle":             "idle",
    "typing":           "active",
    "footsteps":        "active",
    "impact":           "error",    # high-salience — distinct visual
    "equipment_running":"active",
    "dark":             "error",
    "dim_warm":         "idle",
    "daylight":         "active",
    "overcast":         "idle",
    "screen_dominant":  "active",
    "artificial_warm":  "idle",
}


def normalize_to_unity(agent_id: str, sensor: str,
                       observation: str, confidence: float) -> dict:
    """Map an agent interpretation to Unity's node update schema."""
    return {
        "id":    f"{agent_id}-{sensor}",
        "state": _UNITY_STATE_MAP.get(observation, "idle"),
        "value": round(confidence, 4),
        "obs":   observation,
    }


# ── DB helpers ────────────────────────────────────────────────────────────────

async def upsert_node(pool: asyncpg.Pool, node_name: str, node_type: str,
                      location: str, metadata: dict) -> str:
    """Insert or update agent_nodes; return the node's UUID id."""
    row = await pool.fetchrow(
        """
        INSERT INTO agent_nodes (node_name, node_type, location_label, metadata)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (node_name) DO UPDATE
            SET node_type      = EXCLUDED.node_type,
                location_label = COALESCE(EXCLUDED.location_label, agent_nodes.location_label),
                metadata       = agent_nodes.metadata || EXCLUDED.metadata
        RETURNING id
        """,
        node_name, node_type, location, json.dumps(metadata),
    )
    return str(row["id"])


async def update_heartbeat(pool: asyncpg.Pool, node_name: str, heartbeat: dict):
    """Update last_heartbeat_at and merge heartbeat fields into metadata."""
    await pool.execute(
        """
        UPDATE agent_nodes
        SET last_heartbeat_at = NOW(),
            metadata = metadata || $2::jsonb
        WHERE node_name = $1
        """,
        node_name, json.dumps(heartbeat),
    )


async def insert_event(pool: asyncpg.Pool, node_id: str, domain: str,
                       label: str, confidence: str, feature_snapshot: dict):
    """Insert one row into perceptual_events."""
    await pool.execute(
        """
        INSERT INTO perceptual_events
            (agent_node_id, domain, event_label, event_start,
             confidence, feature_snapshot)
        VALUES ($1::uuid, $2::sensor_domain, $3, NOW(),
                $4::fusion_confidence, $5::jsonb)
        """,
        node_id, domain, label, confidence, json.dumps(feature_snapshot),
    )


async def get_node_id(pool: asyncpg.Pool, node_name: str) -> str | None:
    """Look up a node's UUID by its name; return None if not registered yet."""
    row = await pool.fetchrow(
        "SELECT id FROM agent_nodes WHERE node_name = $1", node_name
    )
    return str(row["id"]) if row else None


# ── Message handlers ──────────────────────────────────────────────────────────

async def handle_registration(pool: asyncpg.Pool, payload: dict):
    agent_id   = payload.get("agent_id", "unknown")
    agent_type = payload.get("agent_type", "sensor_node")
    location   = payload.get("location", "")
    caps       = payload.get("capabilities", [])

    node_id = await upsert_node(
        pool,
        node_name=agent_id,
        node_type=agent_type,
        location=location,
        metadata={"capabilities": caps},
    )
    log.info("Registered node %s → %s", agent_id, node_id)


async def handle_interpretation(pool: asyncpg.Pool, agent_id: str, payload: dict,
                               mqtt_publish=None):
    sensor      = payload.get("sensor", "")
    observation = payload.get("observation", "unknown")
    confidence  = float(payload.get("confidence", 0.5))
    raw         = payload.get("raw", {})

    domain = DOMAIN_MAP.get(sensor)
    if not domain:
        log.warning("Unknown sensor %r — skipping", sensor)
        return

    node_id = await get_node_id(pool, agent_id)
    if not node_id:
        log.warning("Node %r not registered yet — auto-registering", agent_id)
        node_id = await upsert_node(pool, agent_id, "sensor_node", "", {})

    await insert_event(
        pool,
        node_id=node_id,
        domain=domain,
        label=observation,
        confidence=conf_level(confidence),
        feature_snapshot={"confidence_f": round(confidence, 4), **raw},
    )
    log.info("Event  %s / %s → %s (%.0f%%)", agent_id, sensor, observation,
             confidence * 100)

    if mqtt_publish:
        unity = normalize_to_unity(agent_id, sensor, observation, confidence)
        mqtt_publish(f"unity/nodes/{unity['id']}/state", json.dumps(unity))


async def handle_observation(pool: asyncpg.Pool, agent_id: str, payload: dict):
    """Handle legacy agents/+/observation messages (e.g. BME280 NodeMCU)."""
    obs_type   = payload.get("observation_type", "unknown")
    summary    = payload.get("semantic_summary", obs_type)
    confidence = float(payload.get("confidence", 0.5))
    raw_data   = payload.get("raw_data", {})

    # Look up node; get its type for domain mapping (registration arrives first
    # as a retained message, so node_type should already be populated).
    row = await pool.fetchrow(
        "SELECT id::text, node_type FROM agent_nodes WHERE node_name = $1", agent_id
    )
    if row:
        node_id    = row["id"]
        node_type  = row["node_type"] or ""
    else:
        node_type  = "sensor_node"
        node_id    = await upsert_node(pool, agent_id, node_type, "", {})

    domain = OBS_DOMAIN_MAP.get(node_type, "environmental_field")

    await insert_event(
        pool,
        node_id=node_id,
        domain=domain,
        label=obs_type,
        confidence=conf_level(confidence),
        feature_snapshot={"summary": summary, **raw_data},
    )
    log.info("Obs    %s → %s (%.0f%%)", agent_id, obs_type, confidence * 100)


async def handle_status(pool: asyncpg.Pool, agent_id: str, payload: dict):
    heartbeat = {
        "rssi_dbm":    payload.get("rssi_dbm"),
        "uptime_ms":   payload.get("uptime_ms"),
        "buf_pending": payload.get("buf_pending", 0),
        "sensors_ok":  payload.get("sensors_ok", ""),
        "last_hb":     datetime.now(timezone.utc).isoformat(),
    }
    await update_heartbeat(pool, agent_id, heartbeat)
    log.info("Heartbeat %s — rssi=%s uptime=%ss",
             agent_id, heartbeat["rssi_dbm"],
             (heartbeat["uptime_ms"] or 0) // 1000)


# ── Async queue bridge ────────────────────────────────────────────────────────

async def process_messages(pool: asyncpg.Pool, queue: asyncio.Queue,
                           mqtt_publish=None):
    while True:
        topic, payload_bytes = await queue.get()
        try:
            payload = json.loads(payload_bytes)
        except json.JSONDecodeError:
            log.warning("Bad JSON on %s", topic)
            queue.task_done()
            continue

        parts = topic.split("/")
        try:
            if topic == "agents/registration":
                await handle_registration(pool, payload)
            elif len(parts) == 3 and parts[2] == "registration":
                await handle_registration(pool, payload)
            elif len(parts) == 3 and parts[2] == "interpretation":
                await handle_interpretation(pool, parts[1], payload,
                                            mqtt_publish=mqtt_publish)
            elif len(parts) == 3 and parts[2] == "observation":
                await handle_observation(pool, parts[1], payload)
            elif len(parts) == 3 and parts[2] == "status":
                await handle_status(pool, parts[1], payload)
        except Exception:
            log.exception("Error processing %s", topic)
        finally:
            queue.task_done()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=4)
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected to %s:%d", MQTT_HOST, MQTT_PORT)
            for topic, qos in TOPICS:
                client.subscribe(topic, qos)
                log.info("Subscribed: %s", topic)
        else:
            log.error("MQTT connect failed rc=%d", rc)

    def on_message(client, userdata, msg):
        loop.call_soon_threadsafe(queue.put_nowait, (msg.topic, msg.payload))

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    # Graceful shutdown
    stop = asyncio.Event()
    loop.add_signal_handler(signal.SIGINT,  stop.set)
    loop.add_signal_handler(signal.SIGTERM, stop.set)

    processor = asyncio.create_task(
        process_messages(pool, queue, mqtt_publish=client.publish)
    )

    log.info("Bridge running — Ctrl-C to stop")
    await stop.wait()

    log.info("Shutting down…")
    client.loop_stop()
    client.disconnect()
    processor.cancel()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
