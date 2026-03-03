"""
Motif Seeder
============
Seeds the motifs table with linguistic descriptions embedded via Ollama
nomic-embed-text (768-dim), matching the vector space used by the
perceptual embedding pipeline.

Run once to bootstrap motif space:
    python seed_motifs.py

Safe to re-run — uses ON CONFLICT DO NOTHING on label.
"""

import asyncio
import json
import logging
import os

import aiohttp
import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DB_URL         = os.environ.get("DATABASE_URL", "postgresql://sean:ecology@localhost/sensor_ecology")
OLLAMA_URL     = os.environ.get("OLLAMA_URL",   "http://localhost:11434/api/embeddings")
OLLAMA_MODEL   = os.environ.get("OLLAMA_MODEL",  "nomic-embed-text")

# ---------------------------------------------------------------------------
# Motif definitions
# Each entry is (label, embedding_text).
# The text is written in the same natural-language register as FeaturePacker
# output — this is what creates semantic alignment with real sensor events.
# ---------------------------------------------------------------------------

MOTIFS = [
    # ── Thermal / presence ───────────────────────────────────────────────────
    (
        "warm presence approaching",
        "Thermal field event: thermal_approach. Field shows significant warm presence. "
        "A warm living body moves closer, maximum temperature rising, presence score increasing. "
        "Significant thermal motion — body approaching.",
    ),
    (
        "warm presence departing",
        "Thermal field event: thermal_retreat. A warm body withdraws from the field. "
        "Presence score dropping, maximum temperature falling as the body moves away. "
        "The thermal field cools and empties.",
    ),
    (
        "warm body present and still",
        "Thermal field event: presence_detected. Field shows significant warm presence. "
        "A warm living being is nearby and stationary. Low thermal motion. "
        "Warmth concentrated, presence sustained.",
    ),
    (
        "thermal motion — body moving through field",
        "Thermal field event: thermal_motion. Significant thermal motion — body moving "
        "through the field. Presence detected, frame delta high. "
        "The warm region shifts position across the frame.",
    ),
    (
        "ambient thermal field — no presence",
        "Thermal field event: thermal_shift. Ambient thermal field, no warm body detected. "
        "Presence score near zero. Low thermal motion. Background field variation only.",
    ),

    # ── Embodied state — activity ─────────────────────────────────────────────
    (
        "agent idle — deep rest",
        "Embodied state event: idle. The agent is at rest, near stillness. "
        "Power draw low, CPU load minimal, acceleration magnitude near 1g. "
        "Board temperature stable. Sustained quiet.",
    ),
    (
        "agent typing — rhythmic keyboard activity",
        "Embodied state event: typing. The agent is typing, hands moving rhythmically. "
        "Acceleration shows periodic small impulses. CPU load moderate. "
        "Sustained deliberate hand movement at the keyboard.",
    ),
    (
        "physical impact — sudden shock",
        "Embodied state event: impact. A sudden physical shock or strike. "
        "Acceleration magnitude spike, sharp transient impulse detected. "
        "Brief high-energy mechanical event.",
    ),
    (
        "footsteps — rhythmic floor vibration",
        "Embodied state event: footsteps. Rhythmic vibration propagating through the surface. "
        "Periodic acceleration impulses at walking cadence. "
        "Someone moving nearby, vibration conducted through the structure.",
    ),
    (
        "equipment running — steady mechanical hum",
        "Embodied state event: equipment_running. Mechanical equipment operating nearby. "
        "Sustained low-amplitude vibration. Steady-state mechanical activity, "
        "not rhythmic like footsteps — continuous background hum.",
    ),

    # ── Embodied state — thermal ───────────────────────────────────────────────
    (
        "thermal stress — agent running hot",
        "Embodied state event: thermal_stress. Board temperature rising significantly above baseline. "
        "Agent under sustained computational load. CPU temperature elevated. "
        "High-power consumption state. Agent under high cognitive load at time of event.",
    ),
    (
        "thermal recovery — agent cooling down",
        "Embodied state event: thermal_recovery. Board temperature falling back toward baseline. "
        "After a period of high load, the agent is cooling. CPU load decreasing. "
        "Thermal recovery following exertion.",
    ),

    # ── Environmental field ────────────────────────────────────────────────────
    (
        "dark environment — low light",
        "Environmental field event: dark. The environment is dark, minimal light detected. "
        "Temperature stable. Pressure stable. The space is unlit, quiet field conditions.",
    ),
    (
        "dim warm ambient light",
        "Environmental field event: dim_warm. Dim warm artificial light present. "
        "Low illumination, warm colour temperature. "
        "Moderate temperature. The room is dimly lit with warm artificial light.",
    ),
    (
        "warm artificial light — room occupied",
        "Environmental field event: artificial_warm. Warm artificial lighting detected. "
        "Room lit with incandescent or warm-spectrum light. "
        "Temperature moderate to warm. Occupied, active environmental conditions.",
    ),
    (
        "cold front arrival — sharp temperature drop",
        "Environmental field event: cold_front_arrival. Temperature dropping sharply. "
        "Cold air entering the space — door or window opened, or external cold ingress. "
        "Pressure may shift. Rapid temperature fall detected.",
    ),
    (
        "environmental shift — field changing",
        "Environmental field event: environmental_shift. The environmental field is changing. "
        "Temperature, humidity, pressure, or air quality deviating from baseline. "
        "Conditions in flux — the space is transitioning.",
    ),
]


# ---------------------------------------------------------------------------
# Embedding helper
# ---------------------------------------------------------------------------

async def embed(session: aiohttp.ClientSession, text: str) -> list[float]:
    async with session.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL, "prompt": text},
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data["embedding"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=4)

    async with aiohttp.ClientSession() as session:
        inserted = 0
        skipped  = 0

        for label, text in MOTIFS:
            log.info("Embedding: %s", label)
            vec = await embed(session, text)

            if len(vec) != 768:
                log.warning("Unexpected embedding dim %d for '%s' — skipping", len(vec), label)
                continue

            vec_str = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"

            result = await pool.fetchval(
                """
                INSERT INTO motifs (label, centroid_embedding)
                VALUES ($1, $2::vector)
                ON CONFLICT (label) DO NOTHING
                RETURNING id
                """,
                label,
                vec_str,
            )

            if result:
                log.info("  ✓ inserted  (id=%s)", result)
                inserted += 1
            else:
                log.info("  – already exists, skipped")
                skipped += 1

    await pool.close()
    log.info("Done — %d inserted, %d skipped", inserted, skipped)


if __name__ == "__main__":
    asyncio.run(main())
