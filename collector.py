"""
Layer 1: Sensor Stream Collector
Reads all available sensors, timestamps everything, stores to SQLite.
Designed to run on Pi 5 with enviro pHAT, extensible to other sensors.
"""

import sqlite3
import time
import json
import logging
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
log = logging.getLogger(__name__)

DB_PATH = Path("sensor_history.db")
INTERVAL_SECONDS = 30  # read every 30 seconds


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,           -- unix timestamp
    iso         TEXT NOT NULL,           -- human readable
    source      TEXT NOT NULL,           -- sensor source name
    temperature REAL,
    pressure    REAL,
    light       REAL,
    rgb_r       REAL,
    rgb_g       REAL,
    rgb_b       REAL,
    motion      INTEGER,
    humidity    REAL,
    raw         TEXT                     -- json blob for any extra fields
);

CREATE INDEX IF NOT EXISTS idx_ts ON readings(ts);
CREATE INDEX IF NOT EXISTS idx_source ON readings(source);

CREATE TABLE IF NOT EXISTS anomalies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    iso         TEXT NOT NULL,
    sensor_a    TEXT NOT NULL,
    sensor_b    TEXT NOT NULL,
    description TEXT NOT NULL,
    raw_context TEXT                     -- json snapshot of recent readings
);
"""


@dataclass
class Reading:
    ts: float
    iso: str
    source: str
    temperature: Optional[float] = None
    pressure: Optional[float] = None
    light: Optional[float] = None
    rgb_r: Optional[float] = None
    rgb_g: Optional[float] = None
    rgb_b: Optional[float] = None
    motion: Optional[int] = None
    humidity: Optional[float] = None
    raw: Optional[str] = None


# ── Sensor backends ───────────────────────────────────────────────────────────

def read_envirophat() -> Optional[Reading]:
    """Read all channels from enviro pHAT."""
    try:
        from envirophat import light, motion, weather, analog
        r, g, b = light.rgb()
        return Reading(
            ts=time.time(),
            iso=datetime.utcnow().isoformat(),
            source="envirophat",
            temperature=weather.temperature(),
            pressure=weather.pressure(),
            light=light.light(),
            rgb_r=r,
            rgb_g=g,
            rgb_b=b,
            motion=1 if any(abs(v) > 0.1 for v in motion.accelerometer()) else 0,
        )
    except ImportError:
        return None
    except Exception as e:
        log.warning(f"envirophat read error: {e}")
        return None


def read_mlx90640() -> Optional[Reading]:
    """Read MLX90640 thermal camera — returns mean/min/max as fields."""
    try:
        import board
        import busio
        import adafruit_mlx90640
        i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)
        mlx = adafruit_mlx90640.MLX90640(i2c)
        mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_2_HZ
        frame = [0] * 768
        mlx.getFrame(frame)
        return Reading(
            ts=time.time(),
            iso=datetime.utcnow().isoformat(),
            source="mlx90640",
            temperature=sum(frame) / len(frame),   # mean scene temp
            raw=json.dumps({
                "min": min(frame),
                "max": max(frame),
                "mean": sum(frame) / len(frame),
            })
        )
    except ImportError:
        return None
    except Exception as e:
        log.warning(f"mlx90640 read error: {e}")
        return None


def read_simulated() -> Reading:
    """
    Simulated sensor for development/testing when hardware isn't connected.
    Generates plausible Yukon winter readings with gentle drift.
    """
    import math, random
    t = time.time()
    # slow oscillation to simulate day/night cycle
    cycle = math.sin(t / 3600 * math.pi)
    return Reading(
        ts=t,
        iso=datetime.utcnow().isoformat(),
        source="simulated",
        temperature=-5.0 + cycle * 8 + random.gauss(0, 0.3),
        pressure=1013.0 + random.gauss(0, 0.5),
        light=max(0, 500 * (cycle + 1) + random.gauss(0, 20)),
        rgb_r=random.gauss(120, 5),
        rgb_g=random.gauss(130, 5),
        rgb_b=random.gauss(160, 5),
        motion=1 if random.random() < 0.05 else 0,
        humidity=65 + random.gauss(0, 2),
    )


# ── Storage ───────────────────────────────────────────────────────────────────

def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    log.info(f"Database ready at {path}")
    return conn


def store_reading(conn: sqlite3.Connection, r: Reading):
    conn.execute("""
        INSERT INTO readings
            (ts, iso, source, temperature, pressure, light,
             rgb_r, rgb_g, rgb_b, motion, humidity, raw)
        VALUES
            (:ts, :iso, :source, :temperature, :pressure, :light,
             :rgb_r, :rgb_g, :rgb_b, :motion, :humidity, :raw)
    """, asdict(r))
    conn.commit()


# ── Collector loop ────────────────────────────────────────────────────────────

def collect_once(conn: sqlite3.Connection):
    """Try all sensor backends, store whatever responds."""
    readers = [read_envirophat, read_mlx90640]
    found_any = False

    for reader in readers:
        reading = reader()
        if reading:
            store_reading(conn, reading)
            log.info(
                f"[{reading.source}] "
                f"temp={reading.temperature:.1f}°C "
                f"pressure={reading.pressure} "
                f"light={reading.light}"
            )
            found_any = True

    # fall back to simulation if no real hardware responds
    if not found_any:
        reading = read_simulated()
        store_reading(conn, reading)
        log.info(
            f"[{reading.source}] "
            f"temp={reading.temperature:.2f}°C "
            f"pressure={reading.pressure:.1f} "
            f"light={reading.light:.1f} "
            f"humidity={reading.humidity:.1f}%"
        )


def run_collector(interval: int = INTERVAL_SECONDS):
    conn = init_db()
    log.info(f"Collector running — reading every {interval}s")
    log.info("Press Ctrl+C to stop")
    try:
        while True:
            collect_once(conn)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("Collector stopped")
    finally:
        conn.close()


if __name__ == "__main__":
    run_collector()
