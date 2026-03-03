"""
Resonance Backfill
==================
Runs the motif resonance classifier against all existing perceptual events
that have embeddings but no resonance record yet.

No re-embedding needed — uses the stored pgvector embeddings directly.

Run once:
    python backfill_resonance.py
"""

import asyncio
import logging
import os
from uuid import UUID

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DB_URL = os.environ.get("DATABASE_URL", "postgresql://sean:ecology@localhost/sensor_ecology")

RECURRENCE_THRESHOLD = 0.20
WEAK_ECHO_THRESHOLD  = 0.40
TOP_K = 5


async def classify_event(conn, event_id: UUID, embedding_str: str) -> list[dict]:
    rows = await conn.fetch(
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
        TOP_K,
    )
    if not rows:
        return []

    resonances = []
    for i, row in enumerate(rows):
        dist = float(row["distance"])
        if dist > WEAK_ECHO_THRESHOLD:
            continue

        if dist <= RECURRENCE_THRESHOLD:
            rtype = "recurrence"
        else:
            rtype = "weak_echo"

        resonances.append({
            "motif_id":    row["id"],
            "distance":    dist,
            "is_nearest":  (i == 0),
            "rtype":       rtype,
        })

    # If nothing was close enough, record the nearest as a candidate
    if not resonances and rows:
        nearest = rows[0]
        resonances.append({
            "motif_id":   nearest["id"],
            "distance":   float(nearest["distance"]),
            "is_nearest": True,
            "rtype":      "candidate",
        })

    return resonances


async def main() -> None:
    pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=8)

    # Fetch all embedded events without an existing resonance record
    events = await pool.fetch(
        """
        SELECT id, embedding
        FROM perceptual_events
        WHERE embedding IS NOT NULL
          AND id NOT IN (SELECT perceptual_event_id FROM motif_resonance)
        ORDER BY event_start ASC
        """
    )

    log.info("Events to backfill: %d", len(events))

    processed = 0
    resonated  = 0

    for row in events:
        event_id = row["id"]
        emb_str  = row["embedding"]

        async with pool.acquire() as conn:
            resonances = await classify_event(conn, event_id, emb_str)

            if resonances:
                await conn.executemany(
                    """
                    INSERT INTO motif_resonance (
                        perceptual_event_id, motif_id, cosine_distance,
                        is_nearest, resonance_type, distance_threshold_used
                    ) VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT DO NOTHING
                    """,
                    [
                        (
                            event_id,
                            r["motif_id"],
                            r["distance"],
                            r["is_nearest"],
                            r["rtype"],
                            WEAK_ECHO_THRESHOLD,
                        )
                        for r in resonances
                    ],
                )
                resonated += 1

        processed += 1
        if processed % 100 == 0:
            log.info("  %d / %d processed, %d resonated", processed, len(events), resonated)

    log.info("Done — %d events processed, %d resonated", processed, resonated)
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
