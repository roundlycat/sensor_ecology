"""
MQTT message bus for the sensor ecology.

Topic structure:
    agents/{agent_id}/observation   -- agent publishes an interpretation
    agents/{agent_id}/query         -- another agent requests info
    agents/{agent_id}/response      -- agent replies (or stays silent)
    collective/patterns             -- emergent insights, open to all
    collective/alerts               -- significant events
"""

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Optional

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

BROKER_HOST = "localhost"
MQTT_USER   = os.getenv("MQTT_USER", "")
MQTT_PASS   = os.getenv("MQTT_PASS", "")
BROKER_PORT = 1883


# ── Message types ─────────────────────────────────────────────────────────────

@dataclass
class ObservationMessage:
    agent_id: str
    observation_type: str
    semantic_summary: str
    confidence: float               # 0.0–1.0
    raw_data: dict = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, payload: str) -> "ObservationMessage":
        return cls(**json.loads(payload))


@dataclass
class QueryMessage:
    from_agent_id: str
    query_type: str                 # 'recent_observations', 'pattern_match', 'status'
    params: dict = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, payload: str) -> "QueryMessage":
        return cls(**json.loads(payload))


@dataclass
class AlertMessage:
    from_agent_id: str
    alert_type: str                 # 'anomaly', 'threshold_exceeded', 'pattern_emerged'
    description: str
    severity: str = "info"          # 'info', 'warning', 'critical'
    context: dict = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, payload: str) -> "AlertMessage":
        return cls(**json.loads(payload))


# ── Bus ───────────────────────────────────────────────────────────────────────

class MQTTBus:
    """
    Thin wrapper around paho-mqtt that speaks the sensor ecology topic structure.
    Each agent creates one of these and uses it to publish and subscribe.
    """

    def __init__(self, agent_id: str, agent_name: str = ""):
        self.agent_id = agent_id
        self.agent_name = agent_name or agent_id
        self._client = mqtt.Client(
            client_id=f"ecology-{agent_id}",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        # topic → list of handler callbacks
        self._handlers: dict[str, list[Callable]] = {}
        self._connected = threading.Event()

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, host: str = BROKER_HOST, port: int = BROKER_PORT):
        if MQTT_USER:
            self._client.username_pw_set(MQTT_USER, MQTT_PASS)
        self._client.connect(host, port, keepalive=60)
        self._client.loop_start()
        if not self._connected.wait(timeout=5):
            raise RuntimeError(f"Could not connect to MQTT broker at {host}:{port}")
        log.info(f"[{self.agent_name}] connected to broker")

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()
        log.info(f"[{self.agent_name}] disconnected from broker")

    # ── Publish helpers ───────────────────────────────────────────────────────

    def publish_observation(self, obs: ObservationMessage):
        topic = f"agents/{self.agent_id}/observation"
        self._publish(topic, obs.to_json())
        log.info(f"[{self.agent_name}] → observation: {obs.observation_type} "
                 f"({obs.confidence:.0%}) — {obs.semantic_summary[:60]}")

    def publish_query(self, target_agent_id: str, query: QueryMessage):
        topic = f"agents/{target_agent_id}/query"
        self._publish(topic, query.to_json())
        log.debug(f"[{self.agent_name}] → query to {target_agent_id}: {query.query_type}")

    def publish_response(self, payload: dict):
        topic = f"agents/{self.agent_id}/response"
        self._publish(topic, json.dumps(payload))

    def publish_alert(self, alert: AlertMessage):
        topic = "collective/alerts"
        self._publish(topic, alert.to_json())
        log.warning(f"[{self.agent_name}] → ALERT [{alert.severity}]: {alert.description[:80]}")

    def publish_pattern(self, payload: dict):
        topic = "collective/patterns"
        self._publish(topic, json.dumps(payload))

    def _publish(self, topic: str, payload: str):
        result = self._client.publish(topic, payload, qos=1)
        result.wait_for_publish(timeout=5)

    # ── Subscribe helpers ─────────────────────────────────────────────────────

    def on_observation(self, handler: Callable[[ObservationMessage], None],
                       agent_id: str = "+"):
        """Subscribe to observations from a specific agent (or all agents with '+')."""
        topic = f"agents/{agent_id}/observation"
        self._subscribe(topic, lambda raw: handler(ObservationMessage.from_json(raw)))

    def on_query(self, handler: Callable[[QueryMessage], None]):
        """Subscribe to queries directed at this agent."""
        topic = f"agents/{self.agent_id}/query"
        self._subscribe(topic, lambda raw: handler(QueryMessage.from_json(raw)))

    def on_alert(self, handler: Callable[[AlertMessage], None]):
        topic = "collective/alerts"
        self._subscribe(topic, lambda raw: handler(AlertMessage.from_json(raw)))

    def on_pattern(self, handler: Callable[[dict], None]):
        topic = "collective/patterns"
        self._subscribe(topic, lambda raw: handler(json.loads(raw)))

    def _subscribe(self, topic: str, handler: Callable[[str], None]):
        self._handlers.setdefault(topic, []).append(handler)
        self._client.subscribe(topic, qos=1)
        log.debug(f"[{self.agent_name}] subscribed to {topic}")

    # ── Internal paho callbacks ───────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self._connected.set()
            # Re-subscribe after reconnect
            for topic in self._handlers:
                client.subscribe(topic, qos=1)
        else:
            log.error(f"[{self.agent_name}] broker refused connection: {reason_code}")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode()
        # Match registered handlers (handle wildcards naively)
        for registered_topic, handlers in self._handlers.items():
            if mqtt.topic_matches_sub(registered_topic, topic):
                for handler in handlers:
                    try:
                        handler(payload)
                    except Exception as e:
                        log.error(f"[{self.agent_name}] handler error on {topic}: {e}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            log.warning(f"[{self.agent_name}] unexpected disconnect, will retry")
        self._connected.clear()
