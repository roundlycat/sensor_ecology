"""
Quick query tool — inspect what the system has learned so far.
Run from the command line to see current state.
"""

import sqlite3
import time
from pathlib import Path
from baseline import compute_correlations, DB_PATH


def show_status():
    conn = sqlite3.connect(str(DB_PATH))

    # reading count
    total = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    oldest = conn.execute("SELECT MIN(iso) FROM readings").fetchone()[0]
    newest = conn.execute("SELECT MAX(iso) FROM readings").fetchone()[0]
    sources = conn.execute(
        "SELECT source, COUNT(*) FROM readings GROUP BY source"
    ).fetchall()

    print(f"\n{'='*60}")
    print(f"SENSOR ECOLOGY STATUS")
    print(f"{'='*60}")
    print(f"Total readings : {total}")
    print(f"From           : {oldest}")
    print(f"To             : {newest}")
    print(f"\nSources:")
    for source, count in sources:
        print(f"  {source}: {count} readings")

    # current correlations
    print(f"\nCurrent correlations (hour window):")
    snapshots = compute_correlations(conn)
    hour_snaps = [s for s in snapshots if s.window == "hour"]
    hour_snaps.sort(key=lambda s: abs(s.correlation), reverse=True)
    for s in hour_snaps[:10]:
        bar = "█" * int(abs(s.correlation) * 20)
        sign = "+" if s.correlation > 0 else "-"
        print(f"  {s.field_a:12} x {s.field_b:12} {sign}{abs(s.correlation):.2f} {bar}")

    # recent anomalies
    anomalies = conn.execute("""
        SELECT iso, sensor_a, sensor_b, description
        FROM anomalies
        ORDER BY ts DESC
        LIMIT 5
    """).fetchall()

    if anomalies:
        print(f"\nRecent anomalies:")
        for iso, a, b, desc in anomalies:
            print(f"  [{iso}] {desc}")
    else:
        print(f"\nNo anomalies detected yet")

    conn.close()


if __name__ == "__main__":
    show_status()
