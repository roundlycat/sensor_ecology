"""
Layer 2: Baseline Builder
Tracks rolling correlations between all sensor pairs across sliding windows.
Normal is defined as "what correlations have been stable."
No domain knowledge required — the environment teaches its own patterns.
"""

import sqlite3
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
import math

log = logging.getLogger(__name__)

DB_PATH = Path("sensor_history.db")

# Windows over which we track correlation stability
WINDOWS = {
    "hour":   3600,
    "day":    86400,
    "week":   604800,
}

# Minimum readings needed before we trust a correlation
MIN_SAMPLES = 20

# How much a correlation must shift to flag as anomaly
ANOMALY_THRESHOLD = 0.4


# ── Math helpers (no numpy dependency) ───────────────────────────────────────

def pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    """Pearson correlation coefficient. Returns None if insufficient variance."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


# ── Sensor fields we track correlations across ────────────────────────────────

FIELDS = ["temperature", "pressure", "light", "rgb_r", "rgb_g", "rgb_b",
          "motion", "humidity"]

PAIRS = [(a, b) for i, a in enumerate(FIELDS)
                for b in FIELDS[i+1:]]


# ── Data access ───────────────────────────────────────────────────────────────

def fetch_window(conn: sqlite3.Connection,
                 seconds: int) -> List[Dict]:
    """Fetch all readings within the last N seconds."""
    since = time.time() - seconds
    cur = conn.execute("""
        SELECT ts, temperature, pressure, light,
               rgb_r, rgb_g, rgb_b, motion, humidity
        FROM readings
        WHERE ts >= ?
        ORDER BY ts ASC
    """, (since,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def extract_pairs(rows: List[Dict],
                  field_a: str,
                  field_b: str) -> Tuple[List[float], List[float]]:
    """Extract two parallel lists of values, skipping rows where either is None."""
    xs, ys = [], []
    for row in rows:
        a, b = row.get(field_a), row.get(field_b)
        if a is not None and b is not None:
            xs.append(float(a))
            ys.append(float(b))
    return xs, ys


# ── Correlation snapshot ──────────────────────────────────────────────────────

@dataclass
class CorrelationSnapshot:
    window: str
    field_a: str
    field_b: str
    correlation: float
    sample_count: int
    ts: float


def compute_correlations(conn: sqlite3.Connection) -> List[CorrelationSnapshot]:
    """Compute all pairwise correlations across all windows."""
    snapshots = []
    now = time.time()

    for window_name, seconds in WINDOWS.items():
        rows = fetch_window(conn, seconds)
        if len(rows) < MIN_SAMPLES:
            continue

        for field_a, field_b in PAIRS:
            xs, ys = extract_pairs(rows, field_a, field_b)
            if len(xs) < MIN_SAMPLES:
                continue
            r = pearson(xs, ys)
            if r is None:
                continue
            snapshots.append(CorrelationSnapshot(
                window=window_name,
                field_a=field_a,
                field_b=field_b,
                correlation=r,
                sample_count=len(xs),
                ts=now,
            ))

    return snapshots


# ── Baseline storage ──────────────────────────────────────────────────────────

BASELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS baselines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    window      TEXT NOT NULL,
    field_a     TEXT NOT NULL,
    field_b     TEXT NOT NULL,
    correlation REAL NOT NULL,
    sample_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_baseline_pair
    ON baselines(window, field_a, field_b, ts);
"""


def init_baseline_db(conn: sqlite3.Connection):
    conn.executescript(BASELINE_SCHEMA)
    conn.commit()


def store_snapshots(conn: sqlite3.Connection,
                    snapshots: List[CorrelationSnapshot]):
    conn.executemany("""
        INSERT INTO baselines
            (ts, window, field_a, field_b, correlation, sample_count)
        VALUES
            (:ts, :window, :field_a, :field_b, :correlation, :sample_count)
    """, [s.__dict__ for s in snapshots])
    conn.commit()


def fetch_recent_baseline(conn: sqlite3.Connection,
                           window: str,
                           field_a: str,
                           field_b: str,
                           lookback: int = 5) -> List[float]:
    """Get the last N stored correlations for a pair to judge stability."""
    cur = conn.execute("""
        SELECT correlation FROM baselines
        WHERE window = ? AND field_a = ? AND field_b = ?
        ORDER BY ts DESC
        LIMIT ?
    """, (window, field_a, field_b, lookback))
    return [row[0] for row in cur.fetchall()]


# ── Anomaly detection ─────────────────────────────────────────────────────────

@dataclass
class Anomaly:
    ts: float
    window: str
    field_a: str
    field_b: str
    previous_correlation: float
    current_correlation: float
    delta: float
    description: str


def detect_anomalies(conn: sqlite3.Connection,
                     current: List[CorrelationSnapshot]) -> List[Anomaly]:
    """
    Compare current correlations against recent baseline history.
    Flag pairs where the correlation has shifted significantly.
    """
    anomalies = []

    for snap in current:
        history = fetch_recent_baseline(
            conn, snap.window, snap.field_a, snap.field_b
        )
        if len(history) < 3:
            # not enough history yet to judge
            continue

        mean_historical = sum(history) / len(history)
        delta = abs(snap.correlation - mean_historical)

        if delta >= ANOMALY_THRESHOLD:
            direction = "emerged" if snap.correlation > mean_historical else "decoupled"
            desc = (
                f"{snap.field_a} and {snap.field_b} have {direction} "
                f"over the {snap.window} window. "
                f"Historical correlation: {mean_historical:.2f}, "
                f"current: {snap.correlation:.2f} "
                f"(Δ {delta:.2f}, n={snap.sample_count})"
            )
            anomalies.append(Anomaly(
                ts=snap.ts,
                window=snap.window,
                field_a=snap.field_a,
                field_b=snap.field_b,
                previous_correlation=mean_historical,
                current_correlation=snap.correlation,
                delta=delta,
                description=desc,
            ))
            log.warning(f"ANOMALY: {desc}")

    return anomalies


def store_anomalies(conn: sqlite3.Connection, anomalies: List[Anomaly]):
    from datetime import datetime
    conn.executemany("""
        INSERT INTO anomalies (ts, iso, sensor_a, sensor_b, description)
        VALUES (?, ?, ?, ?, ?)
    """, [(
        a.ts,
        datetime.utcfromtimestamp(a.ts).isoformat(),
        a.field_a,
        a.field_b,
        a.description,
    ) for a in anomalies])
    conn.commit()


# ── Baseline runner ───────────────────────────────────────────────────────────

def run_baseline(interval: int = 300):
    """
    Compute and store correlation snapshots every N seconds.
    Check for anomalies each cycle.
    """
    conn = sqlite3.connect(str(DB_PATH))
    init_baseline_db(conn)
    log.info(f"Baseline tracker running — updating every {interval}s")

    try:
        while True:
            snapshots = compute_correlations(conn)
            if snapshots:
                anomalies = detect_anomalies(conn, snapshots)
                store_snapshots(conn, snapshots)
                if anomalies:
                    store_anomalies(conn, anomalies)
                log.info(
                    f"Computed {len(snapshots)} correlations, "
                    f"{len(anomalies)} anomalies flagged"
                )
            else:
                log.info("Insufficient data for correlations yet — keep collecting")
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("Baseline tracker stopped")
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    run_baseline()
