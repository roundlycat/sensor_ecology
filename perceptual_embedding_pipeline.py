"""
Perceptual Embedding Pipeline
==============================
Transforms fused sensor readings into embeddings for motif-space proximity queries.

Architecture:
    SensorReading rows  ->  EventDetector  ->  FeaturePacker  ->  Embedder
                                                                        |
                                                                 pgvector store
                                                                        |
                                                               MotifResonanceClassifier

Design principle: the embedding should capture *what the event means perceptually*,
not just what the numbers were. This means serialising feature snapshots as structured
natural language descriptions before passing to an embedding model — this preserves
semantic alignment with your linguistic motif space, which is the whole point.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

import asyncpg
import numpy as np


logger = logging.getLogger(__name__)

async def kanban_hook(
    pool:        asyncpg.Pool,
    board_id:    str,
    event_label: str,
    domain,
    readings:    list,
) -> None:
    """Create a kanban card from a pipeline event."""
    import json
    from uuid import UUID

    # Only card-worthy events
    CARD_WORTHY = {
        "presence_detected", "thermal_approach",
        "presence_departed", "thermal_retreat", "thermal_motion"
    }
    if event_label not in CARD_WORTHY:
        return

    # Extract useful values from readings for description
    by_ch = {r.channel: r.raw_value for r in readings}
    max_t  = by_ch.get("max_temp_c",      "?")
    mean_t = by_ch.get("mean_temp_c",     "?")
    score  = by_ch.get("presence_score",  "?")
    delta  = by_ch.get("frame_delta_rms", "?")

    description = (
        f"Thermal field event: {event_label}. "
        f"Max temp {max_t}°C, ambient mean {mean_t}°C. "
        f"Presence score {score}, frame delta RMS {delta}. "
        f"Domain: {domain.value}."
    )

    payload = json.dumps({
        "max_temp_c":     max_t,
        "mean_temp_c":    mean_t,
        "presence_score": score,
        "frame_delta":    delta,
    })

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM create_sensor_alert_card($1,$2,$3,$4,$5::jsonb)",
            UUID(board_id),
            "pi5-thermal",
            event_label,
            description,
            payload,
        )
        logger.info("Kanban card created: %s [%s]", event_label, row["id"])


# ---------------------------------------------------------------------------
# Domain types (mirror your SQL enums)
# ---------------------------------------------------------------------------

class SensorDomain(str, Enum):
    ENVIRONMENTAL_FIELD = "environmental_field"
    EMBODIED_STATE      = "embodied_state"
    RELATIONAL_CONTACT  = "relational_contact"
    HIGH_BANDWIDTH      = "high_bandwidth"


class FusionConfidence(str, Enum):
    HIGH      = "high"
    MODERATE  = "moderate"
    LOW       = "low"
    SYNTHETIC = "synthetic"


class ResonanceType(str, Enum):
    RECURRENCE = "recurrence"
    CANDIDATE  = "candidate"
    WEAK_ECHO  = "weak_echo"


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------

@dataclass
class SensorReading:
    sensor_label: str
    channel: str
    raw_value: float
    unit: str
    domain: SensorDomain
    quality_flag: int = 0
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    db_id: Optional[int] = None


@dataclass
class AgentState:
    """Metabolic context at event time — joins embodied_state readings."""
    power_mW:      Optional[float] = None
    temp_c:        Optional[float] = None
    cpu_load_pct:  Optional[int]   = None


@dataclass
class PerceptualEvent:
    agent_node_id:      UUID
    domain:             SensorDomain
    confidence:         FusionConfidence
    feature_snapshot:   dict
    source_reading_ids: list[int]
    agent_state:        AgentState
    event_label:        Optional[str]    = None
    is_cross_domain:    bool             = False
    domains_involved:   list[SensorDomain] = field(default_factory=list)
    event_start:        datetime         = field(default_factory=lambda: datetime.now(timezone.utc))
    event_end:          Optional[datetime] = None
    embedding:          Optional[list[float]] = None
    id:                 UUID             = field(default_factory=uuid4)


@dataclass
class MotifResonance:
    perceptual_event_id:     UUID
    motif_id:                UUID
    cosine_distance:         float
    is_nearest:              bool
    resonance_type:          ResonanceType
    distance_threshold_used: float
    source_conversation_id:  Optional[str] = None


# ---------------------------------------------------------------------------
# Feature packing
# Converts a raw sensor snapshot into a structured description for embedding.
# Natural language serialisation preserves semantic alignment with the
# linguistic motif space.
# ---------------------------------------------------------------------------

class FeaturePacker:
    """
    Produces two outputs per event:
      1. feature_snapshot  — compact JSONB for the database column
      2. embedding_text    — natural language description for the embedder

    The embedding_text approach means a 'thermal spike during high CPU load'
    will land near 'cognitive strain' or 'effort' in motif space — which is
    exactly the cross-domain convergence we want to be able to detect.
    """

    DOMAIN_TEMPLATES: dict[SensorDomain, str] = {
        SensorDomain.ENVIRONMENTAL_FIELD: (
            "Environmental field event: {summary}. "
            "Temperature {temperature:.1f}°C, humidity {humidity:.0f}%, "
            "pressure {pressure:.1f} hPa{voc_clause}."
        ),
        SensorDomain.EMBODIED_STATE: (
            "Embodied state event: {summary}. "
            "Power draw {power:.0f} mW, board temperature {temp:.1f}°C, "
            "CPU load {cpu:.0f}%{imu_clause}."
        ),
        SensorDomain.RELATIONAL_CONTACT: (
            "Relational contact event: {summary}. "
            "Contact modality: {modality}. Proximity {proximity:.0f} mm. "
            "{pattern_clause}"
        ),
        SensorDomain.HIGH_BANDWIDTH: (
            "High-bandwidth perception event: {summary}. "
            "Channel: {channel}. Signal quality: {quality}. {detail_clause}"
        ),
    }

    def pack(
        self,
        readings: list[SensorReading],
        domain: SensorDomain,
        agent_state: AgentState,
        event_label: Optional[str] = None,
    ) -> tuple[dict, str]:
        """
        Returns (feature_snapshot, embedding_text).
        """
        by_channel = {r.channel: r.raw_value for r in readings if r.quality_flag == 0}
        snapshot = {
            "domain":       domain.value,
            "label":        event_label,
            "channels":     by_channel,
            "agent_power":  agent_state.power_mW,
            "agent_temp":   agent_state.temp_c,
            "agent_cpu":    agent_state.cpu_load_pct,
            "n_readings":   len(readings),
        }

        text = self._render_text(domain, by_channel, agent_state, event_label)
        return snapshot, text

    def _render_text(
        self,
        domain: SensorDomain,
        channels: dict[str, float],
        state: AgentState,
        label: Optional[str],
    ) -> str:
        summary = label or "unlabelled event"

        if domain == SensorDomain.ENVIRONMENTAL_FIELD:
            voc = channels.get("voc_index")
            voc_clause = f", VOC index {voc:.0f}" if voc is not None else ""
            text = self.DOMAIN_TEMPLATES[domain].format(
                summary=summary,
                temperature=channels.get("temperature", 0),
                humidity=channels.get("humidity", 0),
                pressure=channels.get("pressure", 1013),
                voc_clause=voc_clause,
            )

        elif domain == SensorDomain.EMBODIED_STATE:
            accel = channels.get("acceleration_magnitude")
            imu_clause = (
                f", acceleration {accel:.2f} g" if accel is not None else ""
            )
            text = self.DOMAIN_TEMPLATES[domain].format(
                summary=summary,
                power=state.power_mW or 0,
                temp=state.temp_c or 0,
                cpu=state.cpu_load_pct or 0,
                imu_clause=imu_clause,
            )

        elif domain == SensorDomain.RELATIONAL_CONTACT:
            modality = "piezo" if "piezo_amplitude" in channels else "proximity"
            proximity = channels.get("proximity_raw", 0)
            pattern = channels.get("tap_pattern_label", "")
            pattern_clause = f"Tap pattern: {pattern}." if pattern else ""
            text = self.DOMAIN_TEMPLATES[domain].format(
                summary=summary,
                modality=modality,
                proximity=proximity,
                pattern_clause=pattern_clause,
            )

        elif domain == SensorDomain.HIGH_BANDWIDTH and "presence_score" in channels:
            # MLX90640 thermal camera — serialise as language that resonates with
            # warmth / presence / approach / threshold motifs in the corpus.
            presence  = channels.get("presence_score", 0.0)
            max_temp  = channels.get("max_temp_c", 0.0)
            mean_temp = channels.get("mean_temp_c", 0.0)
            cx        = channels.get("thermal_centroid_x")
            cy        = channels.get("thermal_centroid_y")
            delta     = channels.get("frame_delta_rms", 0.0)

            if presence > 0.15:
                presence_desc = "significant warm presence"
            elif presence > 0.05:
                presence_desc = "faint warmth detected"
            elif presence > 0.01:
                presence_desc = "trace thermal signature"
            else:
                presence_desc = "ambient thermal field — no warm body"

            position_clause = ""
            if cx is not None and presence > 0.02:
                h = "left" if cx < 0.35 else "right" if cx > 0.65 else "centre"
                v = "upper" if cy < 0.35 else "lower" if cy > 0.65 else "mid"
                position_clause = f" Warmth concentrated at {v}-{h} of frame."

            motion_clause = (
                " Significant thermal motion — body approaching or withdrawing."
                if delta > 1.5 else
                " Low thermal motion." if delta < 0.3 else ""
            )

            text = (
                f"Thermal field event: {summary}. "
                f"Field shows {presence_desc} (score {presence:.2f}). "
                f"Maximum temperature {max_temp:.1f}\u00b0C, "
                f"ambient mean {mean_temp:.1f}\u00b0C."
                f"{position_clause}{motion_clause}"
            )

        else:  # HIGH_BANDWIDTH generic
            channel = channels.get("channel_label", "unknown")
            quality = channels.get("signal_quality", "unknown")
            detail  = channels.get("detail", "")
            text = self.DOMAIN_TEMPLATES[domain].format(
                summary=summary,
                channel=channel,
                quality=quality,
                detail_clause=detail,
            )

        # Append metabolic context to every description — this is what allows
        # cross-domain motif convergence between physical events and internal states
        if state.cpu_load_pct is not None and state.cpu_load_pct > 70:
            text += " Agent under high cognitive load at time of event."
        if state.power_mW is not None and state.power_mW > 4000:
            text += " Agent in high-power consumption state."

        return text


# ---------------------------------------------------------------------------
# Embedder
# Wraps your embedding model. Swap the client for a local model if needed.
# ---------------------------------------------------------------------------

class Embedder:
    """
    Async embedder. Defaults to OpenAI text-embedding-3-small (1536 dims).
    For local deployment on Pi 5, swap _embed_remote for _embed_local using
    a quantised model via llama.cpp or Ollama's /api/embeddings endpoint.
    """

    EMBEDDING_DIM = 768

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        use_local: bool = False,
        local_endpoint: str = "http://localhost:11434/api/embeddings",
        local_model: str = "nomic-embed-text",
    ):
        self.model         = model
        self.use_local     = use_local
        self.local_endpoint = local_endpoint
        self.local_model   = local_model
        if not use_local:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI()
        else:
            self._client = None

    async def embed(self, text: str) -> list[float]:
        if self.use_local:
            return await self._embed_local(text)
        return await self._embed_remote(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch embed — more efficient for catchup/replay scenarios."""
        if self.use_local:
            return [await self._embed_local(t) for t in texts]
        response = await self._client.embeddings.create(
            model=self.model,
            input=texts,
        )
        return [item.embedding for item in response.data]

    async def _embed_remote(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(model=self.model, input=text)
        return response.data[0].embedding

    async def _embed_local(self, text: str) -> list[float]:
        """Ollama-compatible local embedding endpoint."""
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.local_endpoint,
                json={"model": self.local_model, "prompt": text},
            ) as resp:
                data = await resp.json()
                return data["embedding"]


# ---------------------------------------------------------------------------
# Motif resonance classifier
# Queries pgvector for nearest motifs to a new perceptual event
# ---------------------------------------------------------------------------

class MotifResonanceClassifier:
    """
    Queries the motifs table for the nearest neighbours to a perceptual event.
    Thresholds should be tuned against your corpus — start conservative.
    """

    RECURRENCE_THRESHOLD = 0.20   # very close — clear recurrence
    WEAK_ECHO_THRESHOLD  = 0.40   # detectable but uncertain

    def __init__(self, db_pool: asyncpg.Pool, top_k: int = 5):
        self.pool  = db_pool
        self.top_k = top_k

    async def classify(
        self,
        event: PerceptualEvent,
        distance_threshold: float = WEAK_ECHO_THRESHOLD,
    ) -> list[MotifResonance]:
        if event.embedding is None:
            raise ValueError("Event must be embedded before resonance classification.")

        embedding_str = f"[{','.join(str(v) for v in event.embedding)}]"

        # Adjust this query to match your actual motifs table schema
        rows = await self.pool.fetch(
            """
            SELECT
                id,
                (centroid_embedding <=> $1::vector) AS distance
            FROM motifs
            WHERE centroid_embedding IS NOT NULL
            ORDER BY distance ASC
            LIMIT $2
            """,
            embedding_str,
            self.top_k,
        )

        if not rows:
            return []

        resonances = []
        for i, row in enumerate(rows):
            dist = float(row["distance"])
            if dist > distance_threshold:
                continue

            if dist <= self.RECURRENCE_THRESHOLD:
                rtype = ResonanceType.RECURRENCE
            elif dist <= self.WEAK_ECHO_THRESHOLD:
                rtype = ResonanceType.WEAK_ECHO
            else:
                rtype = ResonanceType.CANDIDATE

            resonances.append(MotifResonance(
                perceptual_event_id=event.id,
                motif_id=UUID(str(row["id"])),
                cosine_distance=dist,
                is_nearest=(i == 0),
                resonance_type=rtype,
                distance_threshold_used=distance_threshold,
            ))

        # If nothing was close enough, emit a candidate record using the nearest
        if not resonances and rows:
            nearest = rows[0]
            resonances.append(MotifResonance(
                perceptual_event_id=event.id,
                motif_id=UUID(str(nearest["id"])),
                cosine_distance=float(nearest["distance"]),
                is_nearest=True,
                resonance_type=ResonanceType.CANDIDATE,
                distance_threshold_used=distance_threshold,
            ))

        return resonances


# ---------------------------------------------------------------------------
# Database writer
# ---------------------------------------------------------------------------

class PerceptualEventWriter:

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def write_event(self, event: PerceptualEvent) -> UUID:
        embedding_str = (
            f"[{','.join(str(v) for v in event.embedding)}]"
            if event.embedding else None
        )

        domains_involved = (
            [d.value for d in event.domains_involved]
            if event.domains_involved else None
        )

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO perceptual_events (
                    id, agent_node_id, domain, event_label, confidence,
                    source_reading_ids, feature_snapshot, embedding,
                    event_start, event_end, is_cross_domain, domains_involved,
                    agent_power_mw, agent_temp_c, agent_cpu_load_pct
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8::vector,
                    $9, $10, $11, $12,
                    $13, $14, $15
                )
                ON CONFLICT (id) DO NOTHING
                """,
                event.id,
                event.agent_node_id,
                event.domain.value,
                event.event_label,
                event.confidence.value,
                event.source_reading_ids,
                json.dumps(event.feature_snapshot),
                embedding_str,
                event.event_start,
                event.event_end,
                event.is_cross_domain,
                domains_involved,
                event.agent_state.power_mW,
                event.agent_state.temp_c,
                event.agent_state.cpu_load_pct,
            )
        return event.id

    async def write_resonances(self, resonances: list[MotifResonance]) -> None:
        if not resonances:
            return
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO motif_resonance (
                    perceptual_event_id, motif_id, cosine_distance,
                    is_nearest, resonance_type, distance_threshold_used,
                    source_conversation_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                [
                    (
                        r.perceptual_event_id,
                        r.motif_id,
                        r.cosine_distance,
                        r.is_nearest,
                        r.resonance_type.value,
                        r.distance_threshold_used,
                        r.source_conversation_id,
                    )
                    for r in resonances
                ],
            )


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# Composes the above components into a single async callable
# ---------------------------------------------------------------------------

class PerceptualEmbeddingPipeline:
    """
    Main entry point. Designed to run continuously on the edge node,
    consuming sensor events as they are detected.

    Usage:
        pipeline = PerceptualEmbeddingPipeline.build(db_url, use_local_embedder=True)
        await pipeline.process(readings, domain, agent_state, label)
    """

    def __init__(
        self,
        pool:        asyncpg.Pool,
        embedder:    Embedder,
        packer:      FeaturePacker,
        classifier:  MotifResonanceClassifier,
        writer:      PerceptualEventWriter,
        agent_node_id: UUID,
        kanban_hook=None,
    ):
        self.pool          = pool
        self.embedder      = embedder
        self.packer        = packer
        self.classifier    = classifier
        self.writer        = writer
        self.agent_node_id = agent_node_id
        self.kanban_hook   = kanban_hook

    @classmethod
    async def build(
        cls,
        pool:               asyncpg.Pool,
        agent_node_id:      UUID,
        kanban_hook=None,
        use_local_embedder: bool = False,
        local_endpoint:     str  = "http://localhost:11434/api/embeddings",
        local_model:        str  = "nomic-embed-text",
    ) -> "PerceptualEmbeddingPipeline":
        embedder   = Embedder(use_local=use_local_embedder,
                              local_endpoint=local_endpoint,
                              local_model=local_model)
        packer     = FeaturePacker()
        classifier = MotifResonanceClassifier(pool)
        writer     = PerceptualEventWriter(pool)
        return cls(pool, embedder, packer, classifier, writer, agent_node_id, kanban_hook)

    async def process(
        self,
        readings:      list[SensorReading],
        domain:        SensorDomain,
        agent_state:   AgentState,
        event_label:   Optional[str]  = None,
        confidence:    FusionConfidence = FusionConfidence.MODERATE,
    ) -> PerceptualEvent:
        """
        Full pipeline: readings -> feature pack -> embed -> store -> classify resonance.
        Returns the completed PerceptualEvent for downstream use.
        """

        # 1. Pack features and generate embedding text
        snapshot, embedding_text = self.packer.pack(
            readings, domain, agent_state, event_label
        )
        logger.debug("Embedding text: %s", embedding_text)

        # 2. Determine if this is a cross-domain event
        domains_seen = list({r.domain for r in readings})
        is_cross = len(domains_seen) > 1

        # 3. Build event (no embedding yet)
        event = PerceptualEvent(
            agent_node_id=self.agent_node_id,
            domain=domain,
            confidence=confidence,
            feature_snapshot=snapshot,
            source_reading_ids=[r.db_id for r in readings if r.db_id is not None],
            agent_state=agent_state,
            event_label=event_label,
            is_cross_domain=is_cross,
            domains_involved=domains_seen,
        )

        # 4. Generate embedding
        event.embedding = await self.embedder.embed(embedding_text)

        # 5. Write event to DB
        await self.writer.write_event(event)

        # 6. Classify motif resonance
        resonances = await self.classifier.classify(event)
        await self.writer.write_resonances(resonances)

        if resonances:
            nearest = resonances[0]
            logger.info(
                "Event %s -> motif %s (distance=%.3f, type=%s)",
                event.id, nearest.motif_id,
                nearest.cosine_distance, nearest.resonance_type.value,
            )
        else:
            logger.info("Event %s -> no resonance found", event.id)
        # 7. Optional kanban card creation
        if self.kanban_hook and event_label and event_label != "thermal_shift":
            try:
                await self.kanban_hook(event_label, domain, readings)
            except Exception as e:
                logger.warning("Kanban hook failed: %s", e)

        return event
        return event

    async def process_batch(
        self,
        batch: list[tuple[list[SensorReading], SensorDomain, AgentState, Optional[str]]],
    ) -> list[PerceptualEvent]:
        """
        Batch processing for catchup / replay scenarios.
        Embeds all events in a single API call to minimise latency.
        """
        packed = [
            self.packer.pack(readings, domain, state, label)
            for readings, domain, state, label in batch
        ]
        snapshots   = [p[0] for p in packed]
        embed_texts = [p[1] for p in packed]

        embeddings = await self.embedder.embed_batch(embed_texts)

        events = []
        for i, (readings, domain, state, label) in enumerate(batch):
            domains_seen = list({r.domain for r in readings})
            event = PerceptualEvent(
                agent_node_id=self.agent_node_id,
                domain=domain,
                confidence=FusionConfidence.MODERATE,
                feature_snapshot=snapshots[i],
                source_reading_ids=[r.db_id for r in readings if r.db_id is not None],
                agent_state=state,
                event_label=label,
                is_cross_domain=len(domains_seen) > 1,
                domains_involved=domains_seen,
                embedding=embeddings[i],
            )
            await self.writer.write_event(event)
            resonances = await self.classifier.classify(event)
            await self.writer.write_resonances(resonances)
            events.append(event)

        return events

    async def close(self) -> None:
        pass  # pool is owned by the caller; close it there


# ---------------------------------------------------------------------------
# Example usage / smoke test
# ---------------------------------------------------------------------------

async def _smoke_test():
    import os

    DB_URL    = os.environ["DATABASE_URL"]
    NODE_ID   = UUID(os.environ["AGENT_NODE_ID"])
    USE_LOCAL = os.environ.get("USE_LOCAL_EMBEDDER", "false").lower() == "true"

    pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10)
    pipeline = await PerceptualEmbeddingPipeline.build(
        pool=pool,
        agent_node_id=NODE_ID,
        use_local_embedder=USE_LOCAL,
    )

    # Simulate a fused environmental event
    readings = [
        SensorReading("BME688",  "temperature",  -18.2, "degC",  SensorDomain.ENVIRONMENTAL_FIELD, db_id=1),
        SensorReading("BME688",  "humidity",      72.0, "%",     SensorDomain.ENVIRONMENTAL_FIELD, db_id=2),
        SensorReading("BME688",  "pressure",     968.0, "hPa",   SensorDomain.ENVIRONMENTAL_FIELD, db_id=3),
        SensorReading("BME688",  "voc_index",    142.0, "index", SensorDomain.ENVIRONMENTAL_FIELD, db_id=4),
        SensorReading("SHT35",   "temperature",  -18.5, "degC",  SensorDomain.ENVIRONMENTAL_FIELD, db_id=5),
    ]

    state = AgentState(power_mW=2800, temp_c=52.0, cpu_load_pct=34)

    event = await pipeline.process(
        readings=readings,
        domain=SensorDomain.ENVIRONMENTAL_FIELD,
        agent_state=state,
        event_label="cold_front_arrival",
        confidence=FusionConfidence.HIGH,
    )

    print(f"Processed event: {event.id}")
    print(f"Embedding dim:   {len(event.embedding)}")

    await pipeline.close()
    await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_smoke_test())
