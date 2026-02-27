"""
Agent base class.

Every node in the ecology (ESP8266 bridge, Pi coordinator, Hailo vision
processor, etc.) subclasses Agent, overrides sense() and interpret(), and
gets MQTT + Postgres wiring for free.

Lifecycle:
    agent = MyAgent(name="lab2-thermal", location="Lab 2 north wall")
    agent.start()          # registers in DB, connects MQTT, starts loop
    ...
    agent.stop()
"""

import json
import logging
import os
import signal
import threading
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

from mqtt_bus import MQTTBus, ObservationMessage, QueryMessage, AlertMessage

# ── Embedding model (lazy singleton) ─────────────────────────────────────────

_embedder = None
_embedder_lock = threading.Lock()


def _get_embedder():
    """Load all-MiniLM-L6-v2 once; reuse across all agents in this process."""
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                from sentence_transformers import SentenceTransformer
                log.info("Loading embedding model all-MiniLM-L6-v2 ...")
                _embedder = SentenceTransformer("all-MiniLM-L6-v2")
                log.info("Embedding model ready (384-dim)")
    return _embedder

log = logging.getLogger(__name__)

DB_DSN = os.getenv("SENSOR_DB_DSN", "dbname=sensor_ecology user=sean host=localhost")


# ── Database helpers ──────────────────────────────────────────────────────────

def get_conn():
    conn = psycopg2.connect(DB_DSN)
    psycopg2.extras.register_uuid(conn)
    return conn


def _register_agent(conn, agent_id: str, agent_type: str, name: str,
                    capabilities: dict, location: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO agents (agent_id, agent_type, name, capabilities, location_context)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (agent_id) DO UPDATE
                SET last_active_ts = NOW(),
                    capabilities   = EXCLUDED.capabilities
        """, (
            uuid.UUID(agent_id),
            agent_type,
            name,
            json.dumps(capabilities),
            location,
        ))
    conn.commit()
    log.info(f"[{name}] registered in DB as {agent_type}")


def _store_observation(conn, obs: ObservationMessage,
                       embedding: Optional[list] = None) -> uuid.UUID:
    obs_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO observations
                (observation_id, agent_id, observed_at, observation_type,
                 confidence, semantic_summary, embedding, raw_data)
            VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s)
        """, (
            obs_id,
            uuid.UUID(obs.agent_id),
            obs.ts,
            obs.observation_type,
            obs.confidence,
            obs.semantic_summary,
            _vec_literal(embedding),
            json.dumps(obs.raw_data) if obs.raw_data else None,
        ))
    conn.commit()
    return obs_id


def _store_coordination(conn, initiator_id: str, responding_ids: list,
                        coord_type: str, summary: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO coordination_events
                (initiating_agent, responding_agents, coordination_type,
                 conversation_summary)
            VALUES (%s, %s, %s, %s)
        """, (
            uuid.UUID(initiator_id),
            [uuid.UUID(r) for r in responding_ids] if responding_ids else [],
            coord_type,
            summary,
        ))
    conn.commit()


def _vec_literal(embedding: Optional[list]) -> Optional[str]:
    """Convert a float list to Postgres vector literal '[1.0, 2.0, ...]'."""
    if embedding is None:
        return None
    return "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"


def find_similar(conn, embedding: list, threshold: float = 0.75,
                 limit: int = 10, exclude_agent_id: Optional[str] = None) -> list:
    """Return observations semantically similar to the given embedding."""
    vec = _vec_literal(embedding)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if exclude_agent_id:
            cur.execute("""
                SELECT * FROM similar_observations(%s::vector, %s, %s)
                WHERE agent_id != %s
            """, (vec, threshold, limit, uuid.UUID(exclude_agent_id)))
        else:
            cur.execute(
                "SELECT * FROM similar_observations(%s::vector, %s, %s)",
                (vec, threshold, limit)
            )
        return cur.fetchall()


# ── Agent base class ──────────────────────────────────────────────────────────

class Agent(ABC):
    """
    Subclass this for each agent type. Override:
      - agent_type  (class attribute)
      - capabilities (class attribute)
      - sense()     → raw sensor data dict, or None
      - interpret() → ObservationMessage, or None to stay silent
      - on_query()  → optional: respond to queries from other agents
    """

    agent_type: str = "generic"
    capabilities: dict = {}

    def __init__(self, name: str, location: str = "",
                 sense_interval: int = 30,
                 agent_id: Optional[str] = None):
        self.agent_id = agent_id or str(uuid.uuid4())
        self.name = name
        self.location = location
        self.sense_interval = sense_interval

        self._db = get_conn()
        self._bus = MQTTBus(self.agent_id, self.name)
        self._stop_event = threading.Event()

    # ── Subclass interface ────────────────────────────────────────────────────

    @abstractmethod
    def sense(self) -> Optional[dict]:
        """
        Read from hardware or network. Return raw data dict, or None if
        nothing to report this cycle.
        """

    @abstractmethod
    def interpret(self, raw: dict) -> Optional[ObservationMessage]:
        """
        Convert raw sensor data into an interpretation.
        Return None to stay silent this cycle.
        """

    def on_query(self, query: QueryMessage) -> Optional[dict]:
        """
        Override to respond to queries from other agents.
        Return a dict payload, or None to decline.
        """
        return None

    def embed(self, text: str) -> Optional[list]:
        """Return a 384-dim embedding for text using all-MiniLM-L6-v2."""
        return _get_embedder().encode(text).tolist()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        _register_agent(
            self._db, self.agent_id, self.agent_type,
            self.name, self.capabilities, self.location
        )
        self._bus.connect()
        self._bus.on_query(self._handle_query)

        # Catch Ctrl-C gracefully
        signal.signal(signal.SIGINT, lambda s, f: self.stop())
        signal.signal(signal.SIGTERM, lambda s, f: self.stop())

        log.info(f"[{self.name}] starting sense loop every {self.sense_interval}s")
        self._loop()

    def stop(self):
        log.info(f"[{self.name}] stopping")
        self._stop_event.set()
        self._bus.disconnect()
        self._db.close()

    # ── Internal loop ─────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                raw = self.sense()
                if raw is not None:
                    obs = self.interpret(raw)
                    if obs is not None:
                        # Store embedding if available
                        embedding = self.embed(obs.semantic_summary)
                        _store_observation(self._db, obs, embedding)
                        self._bus.publish_observation(obs)
            except Exception as e:
                log.error(f"[{self.name}] sense/interpret error: {e}", exc_info=True)

            self._stop_event.wait(self.sense_interval)

    def _handle_query(self, query: QueryMessage):
        log.debug(f"[{self.name}] received query: {query.query_type} "
                  f"from {query.from_agent_id}")
        response = self.on_query(query)
        if response is not None:
            self._bus.publish_response(response)
            _store_coordination(
                self._db,
                initiator_id=query.from_agent_id,
                responding_ids=[self.agent_id],
                coord_type="query",
                summary=f"{query.query_type} → response from {self.name}",
            )
        else:
            _store_coordination(
                self._db,
                initiator_id=query.from_agent_id,
                responding_ids=[],
                coord_type="refusal",
                summary=f"{self.name} declined query: {query.query_type}",
            )

    # ── Convenience: raise an alert ───────────────────────────────────────────

    def alert(self, alert_type: str, description: str,
              severity: str = "warning", context: dict = None):
        self._bus.publish_alert(AlertMessage(
            from_agent_id=self.agent_id,
            alert_type=alert_type,
            description=description,
            severity=severity,
            context=context or {},
        ))
