"""
Thermal Publisher
=================
Runs on the **thermal Pi** (the one with the MLX90640 attached).
Reads 32×24 frames and publishes them to the main Pi's MQTT broker on:

    thermal/{NODE_NAME}/frame

The main Pi's HighBandwidthPoller subscribes to this topic and handles
event detection, feature extraction, and embedding.

Wiring (MLX90640 default I2C address 0x33):
    SDA → GPIO 2 (pin 3)
    SCL → GPIO 3 (pin 5)
    VCC → 3.3 V
    GND → GND

Dependencies (thermal Pi only):
    pip install adafruit-circuitpython-mlx90640 paho-mqtt

Environment variables:
    MQTT_BROKER_HOST      IP or hostname of main Pi running Mosquitto (required)
    MQTT_BROKER_PORT      default 1883
    THERMAL_NODE_NAME     identifier for this thermal node, default "pi5-thermal"
    THERMAL_INTERVAL_S    seconds between published frames, default 2.0
    THERMAL_MIN_DELTA     minimum RMS change (°C) to force an immediate publish,
                          regardless of interval; default 0.4

Run:
    python thermal_publisher.py
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [thermal-pub] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

BROKER_HOST      = os.getenv("MQTT_BROKER_HOST", "localhost")
BROKER_PORT      = int(os.getenv("MQTT_BROKER_PORT", "1883"))
NODE_NAME        = os.getenv("THERMAL_NODE_NAME", "pi5-thermal")
PUBLISH_INTERVAL = float(os.getenv("THERMAL_INTERVAL_S", "2.0"))
MIN_DELTA        = float(os.getenv("THERMAL_MIN_DELTA", "0.4"))

TOPIC = f"thermal/{NODE_NAME}/frame"
FRAME_SIZE = 768   # MLX90640 is 32 × 24


# ---------------------------------------------------------------------------
# Frame delta helper
# ---------------------------------------------------------------------------

def _rms_delta(a: list[float], b: list[float]) -> float:
    """RMS difference between two frames."""
    return (sum((x - y) ** 2 for x, y in zip(a, b)) / FRAME_SIZE) ** 0.5


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------

class ThermalPublisher:

    def __init__(self):
        self._sensor   = None
        self._client   = mqtt.Client(
            client_id=f"thermal-pub-{NODE_NAME}",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._connected = False
        self._running   = True
        self._prev_frame: Optional[list[float]] = None

    # -----------------------------------------------------------------------
    # Hardware init
    # -----------------------------------------------------------------------

    def _init_sensor(self) -> None:
        """Initialise the MLX90640.  Raises on failure — let it propagate."""
        import board
        import busio
        import adafruit_mlx90640

        i2c = busio.I2C(board.SCL, board.SDA, frequency=400_000)
        self._sensor = adafruit_mlx90640.MLX90640(i2c)
        # 2 Hz gives one full frame every 500 ms; fast enough for presence
        # detection without hammering the I2C bus.
        self._sensor.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_2_HZ
        log.info("MLX90640 initialised at 2 Hz on I2C")

    def _read_frame(self) -> list[float]:
        """Block until the next frame is ready and return it."""
        frame = [0.0] * FRAME_SIZE
        self._sensor.getFrame(frame)
        return frame

    # -----------------------------------------------------------------------
    # MQTT
    # -----------------------------------------------------------------------

    def _connect_mqtt(self) -> None:
        self._client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
        self._client.loop_start()
        # Wait briefly for the connect callback
        deadline = time.monotonic() + 5.0
        while not self._connected and time.monotonic() < deadline:
            time.sleep(0.1)
        if not self._connected:
            raise RuntimeError(
                f"Could not connect to MQTT broker at {BROKER_HOST}:{BROKER_PORT}"
            )

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self._connected = True
            log.info("Connected to broker %s:%d", BROKER_HOST, BROKER_PORT)
        else:
            log.error("Broker refused connection: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            self._connected = False
            log.warning("Unexpected MQTT disconnect (rc=%s), will retry", reason_code)

    def _publish_frame(self, frame: list[float]) -> None:
        payload = json.dumps({
            "node":  NODE_NAME,
            "ts":    datetime.now(timezone.utc).isoformat(),
            "frame": frame,
        })
        result = self._client.publish(TOPIC, payload, qos=0)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("Publish failed (rc=%d)", result.rc)

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def run(self) -> None:
        self._init_sensor()
        self._connect_mqtt()

        log.info(
            "Publishing to %s every %.1f s (min-delta=%.2f °C RMS)",
            TOPIC, PUBLISH_INTERVAL, MIN_DELTA,
        )

        last_publish = 0.0

        while self._running:
            try:
                frame = self._read_frame()
            except Exception as exc:
                log.error("Frame read error: %s", exc)
                time.sleep(1.0)
                continue

            now = time.monotonic()
            elapsed = now - last_publish
            interval_due = elapsed >= PUBLISH_INTERVAL

            # Always publish on interval; also publish early if there's
            # significant thermal motion — so fast approaches aren't missed.
            force_early = False
            if self._prev_frame is not None:
                delta = _rms_delta(frame, self._prev_frame)
                force_early = delta >= MIN_DELTA and elapsed >= 0.4

            if interval_due or force_early:
                self._publish_frame(frame)
                self._prev_frame = frame[:]
                last_publish = now
                if force_early and not interval_due:
                    log.debug("Early publish triggered by thermal motion")

    def stop(self) -> None:
        self._running = False
        self._client.loop_stop()
        self._client.disconnect()
        log.info("Thermal publisher stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    pub = ThermalPublisher()

    def _handle_signal(signum, frame):
        log.info("Signal %d received, shutting down", signum)
        pub.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        pub.run()
    except KeyboardInterrupt:
        pub.stop()


if __name__ == "__main__":
    main()
