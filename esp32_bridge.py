"""
ESP32 Bridge — Pi-side receiver for hardware sensor agents.

.. deprecated::
    This bridge is retained for legacy ESP32 hardware only.
    It uses ``all-MiniLM-L6-v2`` (384-dim embeddings), which is incompatible
    with the project's 768-dim motif space (``nomic-embed-text`` via Ollama).
    Observations written here cannot be compared against motif centroids or
    perceptual events.  New hardware nodes should use ``sensor_ingestion_layer``
    + ``perceptual_embedding_pipeline``, which both operate in 768-dim.

Listens for MQTT messages from ESP32 nodes (or any external agent that can't
reach Postgres directly), generates embeddings, and stores observations to the
sensor ecology database.

Topic contracts:
    agents/{agent_id}/registration   — agent boot / LWT
    agents/{agent_id}/observation    — interpreted sensor reading

Run:
    python3 esp32_bridge.py
"""

import json
import logging
import os
import signal
import sys
import threading
import uuid
import warnings
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import paho.mqtt.client as mqtt
from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [bridge] %(levelname)s %(message)s",
)

BROKER_HOST  = os.getenv("MQTT_BROKER_HOST", "localhost")
BROKER_PORT  = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_USER    = os.getenv("MQTT_USER", "")
MQTT_PASS    = os.getenv("MQTT_PASS", "")
# Set SENSOR_DB_DSN in the environment (or a .env file) — never hardcode credentials.
DB_DSN = os.getenv("SENSOR_DB_DSN", "dbname=sensor_ecology user=sean host=/var/run/postgresql")

# Deterministic UUID namespace for ESP32 agents (fixed, never change)
_ESP32_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def _agent_uuid(mqtt_agent_id: str) -> uuid.UUID:
    """Stable UUID derived from the ESP32's string ID (e.g. mac address)."""
    return uuid.uuid5(_ESP32_NS, mqtt_agent_id)


def _vec_literal(embedding: list) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"


class ESP32Bridge:
    def __init__(self):
        warnings.warn(
            "ESP32Bridge is deprecated: it produces 384-dim embeddings (all-MiniLM-L6-v2) "
            "that are incompatible with the 768-dim motif space. "
            "Migrate to sensor_ingestion_layer + perceptual_embedding_pipeline.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._db = self._open_db()
        log.info("Loading embedding model (all-MiniLM-L6-v2, 384-dim) …")
        self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
        log.info("Embedding model ready")

        self._client = mqtt.Client(
            client_id="esp32-bridge",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self._client.on_connect    = self._on_connect
        self._client.on_message    = self._on_message
        self._client.on_disconnect = self._on_disconnect

        # Cache: mqtt string id → UUID so we don't hit Postgres every message
        self._uuid_cache: dict[str, uuid.UUID] = {}
        self._stop = threading.Event()

    # ── Database ──────────────────────────────────────────────────────────────

    def _open_db(self):
        conn = psycopg2.connect(DB_DSN)
        psycopg2.extras.register_uuid(conn)
        return conn

    def _register_agent(self, mqtt_id: str, agent_type: str, name: str,
                        capabilities: dict, location: str) -> uuid.UUID:
        agent_uuid = _agent_uuid(mqtt_id)
        with self._db.cursor() as cur:
            cur.execute("""
                INSERT INTO agents
                    (agent_id, agent_type, name, capabilities, location_context)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (agent_id) DO UPDATE
                    SET last_active_ts = NOW(),
                        capabilities   = EXCLUDED.capabilities
            """, (agent_uuid, agent_type, name, json.dumps(capabilities), location))
        self._db.commit()
        self._uuid_cache[mqtt_id] = agent_uuid
        return agent_uuid

    def _get_agent_uuid(self, mqtt_id: str) -> uuid.UUID:
        """Return cached UUID, auto-registering a minimal agent if unseen."""
        if mqtt_id not in self._uuid_cache:
            self._register_agent(
                mqtt_id=mqtt_id,
                agent_type="esp32_node",
                name=mqtt_id,
                capabilities={"sensors": []},
                location="unspecified",
            )
        return self._uuid_cache[mqtt_id]

    def _store_observation(self, agent_uuid: uuid.UUID, payload: dict):
        summary     = payload.get("semantic_summary", "")
        obs_type    = payload.get("observation_type", "unknown")
        confidence  = float(payload.get("confidence", 0.5))
        raw_data    = payload.get("raw_data", {})
        observed_at = datetime.now(timezone.utc)
        obs_id      = uuid.uuid4()

        embedding = self._embedder.encode(summary).tolist()
        vec       = _vec_literal(embedding)

        with self._db.cursor() as cur:
            cur.execute("""
                INSERT INTO observations
                    (observation_id, agent_id, observed_at, observation_type,
                     confidence, semantic_summary, embedding, raw_data)
                VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s)
            """, (obs_id, agent_uuid, observed_at, obs_type,
                  confidence, summary, vec,
                  json.dumps(raw_data) if raw_data else None))
            cur.execute(
                "UPDATE agents SET last_active_ts = NOW() WHERE agent_id = %s",
                (agent_uuid,)
            )
        self._db.commit()
        return obs_id

    # ── MQTT callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc, props):
        if rc == 0:
            client.subscribe("agents/+/registration", qos=1)
            client.subscribe("agents/+/observation",  qos=1)
            log.info("Connected to broker — subscribed to agent/# topics")
        else:
            log.error(f"Broker refused connection: rc={rc}")

    def _on_disconnect(self, client, userdata, flags, rc, props):
        if rc != 0:
            log.warning(f"Unexpected disconnect (rc={rc})")

    def _on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        if len(parts) != 3 or parts[0] != "agents":
            return
        _, mqtt_agent_id, msg_type = parts

        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.warning(f"Bad payload on {msg.topic}: {e}")
            return

        try:
            if msg_type == "registration":
                self._handle_registration(mqtt_agent_id, payload)
            elif msg_type == "observation":
                self._handle_observation(mqtt_agent_id, payload)
        except Exception as e:
            log.error(f"Error handling {msg.topic}: {e}", exc_info=True)
            # Reconnect DB if connection was lost
            try:
                self._db = self._open_db()
            except Exception:
                pass

    def _handle_registration(self, mqtt_id: str, payload: dict):
        status = payload.get("status", "online")
        agent_uuid = self._register_agent(
            mqtt_id=mqtt_id,
            agent_type=payload.get("agent_type", "esp32_node"),
            name=payload.get("agent_name", mqtt_id),
            capabilities={"sensors": payload.get("capabilities", [])},
            location=payload.get("location", "unspecified"),
        )
        log.info(f"[reg] {mqtt_id} → {agent_uuid} ({status})")

    def _handle_observation(self, mqtt_id: str, payload: dict):
        agent_uuid = self._get_agent_uuid(mqtt_id)
        obs_id = self._store_observation(agent_uuid, payload)
        summary = payload.get("semantic_summary", "")
        obs_type = payload.get("observation_type", "?")
        log.info(f"[obs] {mqtt_id} | {obs_type} | {summary[:70]}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        signal.signal(signal.SIGINT,  lambda s, f: self.stop())
        signal.signal(signal.SIGTERM, lambda s, f: self.stop())

        if MQTT_USER:
            self._client.username_pw_set(MQTT_USER, MQTT_PASS)
        self._client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
        self._client.loop_start()
        log.info(f"ESP32 bridge running (broker={BROKER_HOST}:{BROKER_PORT})")
        self._stop.wait()

    def stop(self):
        log.info("Bridge stopping …")
        self._stop.set()
        self._client.loop_stop()
        self._client.disconnect()
        self._db.close()
        log.info("Bridge stopped")


if __name__ == "__main__":
    bridge = ESP32Bridge()
    bridge.start()
