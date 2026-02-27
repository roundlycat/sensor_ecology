"""
Motif Drift Updater
===================
Periodically recomputes motif centroids as perceptual events accrete.

For each motif with a centroid embedding, new resonating perceptual events
(resonance_type = 'recurrence' or 'weak_echo') since the last drift update
are folded into the centroid via an exponential moving average (EMA).

The linguistic origin is preserved through a max-drift cap: the centroid
cannot move further than MAX_DRIFT_COSINE from its initial linguistic
embedding. This ensures motifs remain semantically grounded even after
thousands of physical resonances.

A perceptual_motif_drift record is written for every motif that moves,
providing the Unity graph with a time-series of centroid displacement.

Run standalone:
    python drift_updater.py

Or as a long-running loop (used by the systemd service):
    python drift_updater.py --loop --interval 3600

Environment variables:
    DATABASE_URL        asyncpg connection string (required)
    DRIFT_INTERVAL_S    seconds between full passes (default 3600)
    DRIFT_ALPHA_RECURRENCE   EMA weight for 'recurrence' events (default 0.05)
    DRIFT_ALPHA_WEAK_ECHO    EMA weight for 'weak_echo' events (default 0.025)
    DRIFT_MAX_COSINE    max cosine distance from linguistic anchor (default 0.30)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import asyncpg
import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults — override via environment variables in main()
# ---------------------------------------------------------------------------

DEFAULT_ALPHA_RECURRENCE = 0.05    # one recurrence nudges centroid ~5%
DEFAULT_ALPHA_WEAK_ECHO  = 0.025   # weak echoes move it half as fast
DEFAULT_MAX_DRIFT_COSINE = 0.30    # centroid may drift at most 0.30 cosine
                                   # from its linguistic origin before clamping
DEFAULT_INTERVAL_S       = 3600    # one pass per hour


# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------

def _vec_to_array(pgvec: str) -> np.ndarray:
    """Convert pgvector string '[0.1,0.2,...]' to a float32 numpy array."""
    return np.fromstring(pgvec.strip("[]"), sep=",", dtype=np.float32)


def _array_to_pgvec(arr: np.ndarray) -> str:
    """Convert numpy array to pgvector literal string."""
    return "[" + ",".join(f"{v:.8f}" for v in arr) + "]"


def _normalise(arr: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(arr)
    return arr / norm if norm > 1e-10 else arr


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine distance in [0, 2]. 0 = identical directions, 1 = orthogonal."""
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom < 1e-10:
        return 1.0
    return float(1.0 - np.dot(a, b) / denom)


def ema_step(centroid: np.ndarray, event_vec: np.ndarray, alpha: float) -> np.ndarray:
    """
    Single EMA update: blend event_vec into centroid with weight alpha.
    Result is L2-normalised so it remains a unit vector on the sphere.
    """
    return _normalise((1.0 - alpha) * centroid + alpha * event_vec)


def clamp_to_anchor(
    centroid: np.ndarray,
    anchor: np.ndarray,
    max_drift: float,
) -> np.ndarray:
    """
    If cosine distance from anchor exceeds max_drift, project centroid back
    along the geodesic toward anchor until it sits exactly at max_drift.

    Uses bisection (20 iterations ≈ 1e-6 precision) rather than a closed-form
    SLERP to avoid numerical issues near antipodal points.
    """
    if cosine_distance(centroid, anchor) <= max_drift:
        return centroid

    lo, hi = 0.0, 1.0
    for _ in range(20):
        t = (lo + hi) * 0.5
        candidate = _normalise((1.0 - t) * anchor + t * centroid)
        if cosine_distance(candidate, anchor) < max_drift:
            lo = t
        else:
            hi = t
    return _normalise((1.0 - lo) * anchor + lo * centroid)


# ---------------------------------------------------------------------------
# Drift updater
# ---------------------------------------------------------------------------

class MotifDriftUpdater:
    """
    Iterates all motifs with a centroid_embedding, finds new resonating
    perceptual events since the last update, and folds them into the centroid
    via an EMA.  Writes perceptual_motif_drift and updates motifs.centroid_embedding.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        alpha_recurrence: float = DEFAULT_ALPHA_RECURRENCE,
        alpha_weak_echo:  float = DEFAULT_ALPHA_WEAK_ECHO,
        max_drift_cosine: float = DEFAULT_MAX_DRIFT_COSINE,
    ):
        self.pool             = pool
        self.alpha_recurrence = alpha_recurrence
        self.alpha_weak_echo  = alpha_weak_echo
        self.max_drift_cosine = max_drift_cosine

    # -----------------------------------------------------------------------
    # Public entry points
    # -----------------------------------------------------------------------

    async def run_once(self) -> int:
        """
        Process all motifs with at least one new resonating event since their
        last drift update.  Returns the number of motifs whose centroids moved.
        """
        motifs = await self._fetch_motifs_to_update()
        if not motifs:
            logger.info("Drift updater: no motifs with new resonances")
            return 0

        updated = 0
        for row in motifs:
            moved = await self._update_motif(row)
            if moved:
                updated += 1

        logger.info("Drift updater: %d / %d motifs updated", updated, len(motifs))
        return updated

    async def run_loop(self, interval_s: float = DEFAULT_INTERVAL_S) -> None:
        """Run run_once() in a continuous loop, sleeping interval_s between passes."""
        logger.info("Drift updater loop started (interval=%.0fs)", interval_s)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                logger.info("Drift updater loop cancelled")
                return
            except Exception:
                logger.exception("Drift updater run_once() failed; will retry next cycle")
            await asyncio.sleep(interval_s)

    # -----------------------------------------------------------------------
    # Internal: fetch candidates
    # -----------------------------------------------------------------------

    async def _fetch_motifs_to_update(self) -> list[asyncpg.Record]:
        """
        Return all motifs that:
          - Have a centroid_embedding (unseeded motifs are skipped)
          - Have at least one resonating event (recurrence/weak_echo) that
            arrived after the motif's last drift update, or have never been
            updated at all (since IS NULL).

        The LEFT JOIN on perceptual_motif_drift uses the latest computed_at
        for each motif as the cursor.
        """
        return await self.pool.fetch(
            """
            SELECT
                m.id,
                m.label,
                m.centroid_embedding,
                d.last_computed_at
            FROM motifs m
            LEFT JOIN LATERAL (
                SELECT MAX(computed_at) AS last_computed_at
                FROM perceptual_motif_drift
                WHERE motif_id = m.id
            ) d ON TRUE
            WHERE m.centroid_embedding IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM motif_resonance mr
                  JOIN perceptual_events pe ON pe.id = mr.perceptual_event_id
                  WHERE mr.motif_id = m.id
                    AND mr.resonance_type IN ('recurrence', 'weak_echo')
                    AND pe.embedding IS NOT NULL
                    AND (d.last_computed_at IS NULL OR mr.observed_at > d.last_computed_at)
              )
            """
        )

    # -----------------------------------------------------------------------
    # Internal: update one motif
    # -----------------------------------------------------------------------

    async def _update_motif(self, motif_row: asyncpg.Record) -> bool:
        """
        Apply EMA updates for all new resonances on one motif.
        Returns True if the centroid actually moved.
        """
        motif_id    = motif_row["id"]
        since       = motif_row["last_computed_at"]          # may be None
        centroid    = _vec_to_array(motif_row["centroid_embedding"])

        async with self.pool.acquire() as conn:
            # Linguistic anchor: the centroid before any physical drift.
            # If this is the first update for this motif, the current
            # centroid (seeded from the corpus) is the anchor.
            anchor = await self._fetch_linguistic_anchor(conn, motif_id, centroid)

            # New resonating events since last update, oldest first
            resonances = await self._fetch_new_resonances(conn, motif_id, since)
            if not resonances:
                return False

            before = centroid.copy()

            # Apply EMA updates in temporal order
            for r in resonances:
                event_vec = _vec_to_array(r["embedding"])
                alpha = (
                    self.alpha_recurrence
                    if r["resonance_type"] == "recurrence"
                    else self.alpha_weak_echo
                )
                centroid = ema_step(centroid, event_vec, alpha)

            # Clamp to max drift from linguistic anchor
            centroid = clamp_to_anchor(centroid, anchor, self.max_drift_cosine)

            drift = cosine_distance(before, centroid)
            if drift < 1e-6:
                # Numerical noise only; don't write a spurious drift record
                return False

            now             = datetime.now(timezone.utc)
            after_str       = _array_to_pgvec(centroid)
            before_str      = _array_to_pgvec(before)
            trigger_id      = resonances[-1]["perceptual_event_id"]
            n_total         = await self._count_total_resonances(conn, motif_id)

            await conn.execute(
                """
                INSERT INTO perceptual_motif_drift (
                    motif_id,
                    centroid_before,
                    centroid_after,
                    trigger_event_id,
                    n_events_included,
                    computed_at
                ) VALUES ($1, $2::vector, $3::vector, $4, $5, $6)
                """,
                motif_id,
                before_str,
                after_str,
                trigger_id,
                n_total,
                now,
            )

            await conn.execute(
                """
                UPDATE motifs
                SET centroid_embedding = $1::vector,
                    updated_at         = $2
                WHERE id = $3
                """,
                after_str,
                now,
                motif_id,
            )

        logger.info(
            "Motif '%s' (%s): drift=%.4f  events_batch=%d  total_resonances=%d",
            motif_row["label"] or str(motif_id)[:8],
            motif_id,
            drift,
            len(resonances),
            n_total,
        )
        return True

    # -----------------------------------------------------------------------
    # Internal: queries
    # -----------------------------------------------------------------------

    async def _fetch_linguistic_anchor(
        self,
        conn: asyncpg.Connection,
        motif_id: UUID,
        fallback: np.ndarray,
    ) -> np.ndarray:
        """
        Return the centroid_before from the oldest drift record for this motif.
        If no drift record exists the motif has never been updated, so the
        current centroid (seeded from the corpus) is the linguistic anchor.
        """
        row = await conn.fetchrow(
            """
            SELECT centroid_before
            FROM perceptual_motif_drift
            WHERE motif_id = $1
              AND centroid_before IS NOT NULL
            ORDER BY computed_at ASC
            LIMIT 1
            """,
            motif_id,
        )
        if row and row["centroid_before"] is not None:
            return _vec_to_array(row["centroid_before"])
        return fallback.copy()

    async def _fetch_new_resonances(
        self,
        conn: asyncpg.Connection,
        motif_id: UUID,
        since: Optional[datetime],
    ) -> list[asyncpg.Record]:
        """
        Resonating perceptual events for this motif that arrived after `since`,
        ordered oldest-first so EMA proceeds in temporal order.
        """
        if since is not None:
            return list(await conn.fetch(
                """
                SELECT
                    pe.embedding,
                    mr.resonance_type,
                    mr.observed_at,
                    mr.perceptual_event_id
                FROM motif_resonance mr
                JOIN perceptual_events pe ON pe.id = mr.perceptual_event_id
                WHERE mr.motif_id = $1
                  AND mr.resonance_type IN ('recurrence', 'weak_echo')
                  AND pe.embedding IS NOT NULL
                  AND mr.observed_at > $2
                ORDER BY mr.observed_at ASC
                """,
                motif_id,
                since,
            ))
        else:
            return list(await conn.fetch(
                """
                SELECT
                    pe.embedding,
                    mr.resonance_type,
                    mr.observed_at,
                    mr.perceptual_event_id
                FROM motif_resonance mr
                JOIN perceptual_events pe ON pe.id = mr.perceptual_event_id
                WHERE mr.motif_id = $1
                  AND mr.resonance_type IN ('recurrence', 'weak_echo')
                  AND pe.embedding IS NOT NULL
                ORDER BY mr.observed_at ASC
                """,
                motif_id,
            ))

    async def _count_total_resonances(
        self,
        conn: asyncpg.Connection,
        motif_id: UUID,
    ) -> int:
        """Running count of all embedded recurrence/weak_echo events for this motif."""
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS n
            FROM motif_resonance mr
            JOIN perceptual_events pe ON pe.id = mr.perceptual_event_id
            WHERE mr.motif_id = $1
              AND mr.resonance_type IN ('recurrence', 'weak_echo')
              AND pe.embedding IS NOT NULL
            """,
            motif_id,
        )
        return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Motif Drift Updater")
    parser.add_argument(
        "--loop", action="store_true",
        help="Run continuously instead of a single pass",
    )
    parser.add_argument(
        "--interval", type=float,
        default=float(os.environ.get("DRIFT_INTERVAL_S", DEFAULT_INTERVAL_S)),
        help="Seconds between passes in loop mode (default %(default)s)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_url = os.environ["DATABASE_URL"]

    alpha_recurrence = float(os.environ.get("DRIFT_ALPHA_RECURRENCE", DEFAULT_ALPHA_RECURRENCE))
    alpha_weak_echo  = float(os.environ.get("DRIFT_ALPHA_WEAK_ECHO",  DEFAULT_ALPHA_WEAK_ECHO))
    max_drift        = float(os.environ.get("DRIFT_MAX_COSINE",       DEFAULT_MAX_DRIFT_COSINE))

    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=4)

    updater = MotifDriftUpdater(
        pool             = pool,
        alpha_recurrence = alpha_recurrence,
        alpha_weak_echo  = alpha_weak_echo,
        max_drift_cosine = max_drift,
    )

    try:
        if args.loop:
            await updater.run_loop(interval_s=args.interval)
        else:
            n = await updater.run_once()
            print(f"Updated {n} motif centroid(s).")
    except KeyboardInterrupt:
        pass
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
