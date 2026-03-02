"""
mqtt_kanban_bridge.py
Listens to your existing MQTT sensor streams and creates kanban cards
for events that warrant attention. Drop this alongside your existing
sensor_ingestion_layer.py and run it as a separate service.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import asyncpg
import paho.mqtt.client as mqtt
from uuid import UUID

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER_HOST", "localhost")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", 1883))
THERMAL_NODE_NAME = os.environ.get("THERMAL_NODE_NAME", "pi5-thermal")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://sean@localhost/sensor_ecology")
KANBAN_BOARD_ID = os.environ.get("KANBAN_BOARD_ID")  # UUID of your board

# Which events create cards vs just log
CARD_WORTHY_EVENTS = {
    # thermal camera (MLX90640) events
    "thermal_approach":    ("high",   "Thermal approach detected"),
    "presence_detected":   ("medium", "Presence detected in monitored area"),
    "presence_departed":   ("low",    "Presence departed"),
    "thermal_retreat":     ("low",    "Thermal retreat detected"),
    # ESP32 agent events
    "impact":              ("high",   "Physical impact detected"),
    "equipment_running":   ("medium", "Equipment running detected"),
}

# Cooldown per node+event to avoid card spam (seconds)
COOLDOWN_SECONDS = 300

# ─── State ────────────────────────────────────────────────────────────────────

@dataclass
class BridgeState:
    pool: asyncpg.Pool
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue
    last_card: dict  # (node, event_type) -> timestamp

state: Optional[BridgeState] = None

# ─── MQTT callbacks ───────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("Connected to MQTT broker")
        client.subscribe(f"thermal/+/frame")
        client.subscribe(f"thermal/+/event")
        client.subscribe(f"sensors/+/event")
        client.subscribe(f"agents/+/interpretation")
    else:
        log.error(f"MQTT connection failed: rc={rc}")


def on_message(client, userdata, msg):
    """Runs in paho thread — push to asyncio queue."""
    try:
        payload = json.loads(msg.payload.decode())
        state.loop.call_soon_threadsafe(
            state.queue.put_nowait,
            {"topic": msg.topic, "payload": payload}
        )
    except Exception as e:
        log.warning(f"Failed to parse message on {msg.topic}: {e}")

# ─── Event processing ─────────────────────────────────────────────────────────

async def process_messages():
    while True:
        msg = await state.queue.get()
        topic: str = msg["topic"]
        payload: dict = msg["payload"]

        try:
            parts = topic.split("/")
            # thermal/{node}/event  or  sensors/{node}/event
            if len(parts) == 3 and parts[2] == "event":
                await handle_sensor_event(parts[1], payload)

            # thermal/{node}/frame — extract events from frame metadata
            elif len(parts) == 3 and parts[2] == "frame":
                await handle_thermal_frame(parts[1], payload)

            # agents/{node_name}/interpretation — ESP32 semantic observations
            elif len(parts) == 3 and parts[0] == "agents" and parts[2] == "interpretation":
                await handle_agent_interpretation(parts[1], payload)

        except Exception as e:
            log.exception(f"Error processing {topic}: {e}")

        state.queue.task_done()


async def handle_thermal_frame(node_name: str, payload: dict):
    """Extract meaningful events from a thermal frame and conditionally create cards."""
    event_label = payload.get("event_label")
    if not event_label or event_label not in CARD_WORTHY_EVENTS:
        return

    priority, description_template = CARD_WORTHY_EVENTS[event_label]

    # Build rich description from frame data
    max_temp  = payload.get("max_temp_c", "?")
    mean_temp = payload.get("mean_temp_c", "?")
    score     = payload.get("presence_score", "?")
    cx        = payload.get("thermal_centroid_x", "?")
    cy        = payload.get("thermal_centroid_y", "?")

    description = (
        f"Thermal field event: {event_label}. "
        f"Max temp {max_temp}°C, ambient mean {mean_temp}°C. "
        f"Presence score {score}. "
        f"Centroid at ({cx}, {cy})."
    )

    await maybe_create_card(node_name, event_label, description, priority, payload)


async def handle_sensor_event(node_name: str, payload: dict):
    """Handle explicit event messages from sensor nodes."""
    event_type = payload.get("event_type", "unknown")
    description = payload.get("description", f"Sensor event from {node_name}")

    priority, _ = CARD_WORTHY_EVENTS.get(event_type, ("medium", ""))

    await maybe_create_card(node_name, event_type, description, priority, payload)


async def handle_agent_interpretation(node_name: str, payload: dict):
    """Handle agents/+/interpretation messages from ESP32 nodes."""
    obs    = payload.get("observation", "unknown")
    conf   = float(payload.get("confidence", 0.0))
    sensor = payload.get("sensor", "unknown")
    raw    = payload.get("raw", {})

    if obs not in CARD_WORTHY_EVENTS:
        return

    priority, title = CARD_WORTHY_EVENTS[obs]
    description = (
        f"{title}. Node: {node_name}, sensor: {sensor}. "
        f"Confidence: {conf:.0%}. Raw: {raw}."
    )
    await maybe_create_card(node_name, obs, description, priority, payload)


async def maybe_create_card(
    node_name: str,
    event_type: str,
    description: str,
    priority: str,
    raw_payload: dict
):
    if not KANBAN_BOARD_ID:
        log.debug("KANBAN_BOARD_ID not set — skipping card creation")
        return

    # Cooldown check
    key = (node_name, event_type)
    import time
    now = time.time()
    last = state.last_card.get(key, 0)
    if now - last < COOLDOWN_SECONDS:
        log.debug(f"Cooldown active for {key} — skipping")
        return

    state.last_card[key] = now

    async with state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM create_sensor_alert_card($1,$2,$3,$4,$5::jsonb,$6)",
            UUID(KANBAN_BOARD_ID),
            node_name,
            event_type,
            description,
            json.dumps(raw_payload),
            priority,
        )
        log.info(f"Created card [{row['id']}] {priority} — {node_name}/{event_type}")

# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    global state

    pool = await asyncpg.create_pool(DATABASE_URL)
    loop = asyncio.get_event_loop()
    queue = asyncio.Queue()

    state = BridgeState(pool=pool, loop=loop, queue=queue, last_card={})

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT)
    client.loop_start()

    log.info(f"MQTT kanban bridge started — broker {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
    log.info(f"Board ID: {KANBAN_BOARD_ID or 'NOT SET — cards disabled'}")

    await process_messages()  # runs forever


if __name__ == "__main__":
    asyncio.run(main())
