"""
Sensor Ecology — main entry point.
Runs collector and baseline tracker as concurrent threads.
"""

import threading
import logging
import argparse

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(threadName)s] %(levelname)s %(message)s'
)

from collector import run_collector
from baseline import run_baseline


def main():
    parser = argparse.ArgumentParser(description="Sensor Ecology")
    parser.add_argument("--collect-interval", type=int, default=30,
                        help="Seconds between sensor readings (default 30)")
    parser.add_argument("--baseline-interval", type=int, default=300,
                        help="Seconds between baseline updates (default 300)")
    parser.add_argument("--collector-only", action="store_true",
                        help="Run only the collector (useful for first run)")
    args = parser.parse_args()

    threads = []

    t1 = threading.Thread(
        target=run_collector,
        args=(args.collect_interval,),
        name="Collector",
        daemon=True
    )
    threads.append(t1)

    if not args.collector_only:
        t2 = threading.Thread(
            target=run_baseline,
            args=(args.baseline_interval,),
            name="Baseline",
            daemon=True
        )
        threads.append(t2)

    for t in threads:
        t.start()

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\nShutting down sensor ecology")


if __name__ == "__main__":
    main()
