"""
EnviroPhat agent — reads the enviro pHAT and publishes interpretations.
Falls back to simulated data when hardware isn't present.
"""

import logging
import math
import random
import sys
import time
from typing import Optional
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import Agent
from mqtt_bus import ObservationMessage

log = logging.getLogger(__name__)


class EnviroAgent(Agent):
    agent_type = "envirophat_node"
    capabilities = {
        "sensors": ["temperature", "pressure", "light", "rgb", "motion"],
        "hardware": "envirophat",
    }

    def __init__(self, name: str = "envirophat-01",
                 location: str = "unspecified", sense_interval: int = 30):
        super().__init__(name=name, location=location,
                         sense_interval=sense_interval)
        self._prev_temp = None

    # ── sense ─────────────────────────────────────────────────────────────────

    def sense(self) -> Optional[dict]:
        raw = self._read_hardware()
        if raw is None:
            raw = self._read_simulated()
            raw["source"] = "simulated"
        else:
            raw["source"] = "envirophat"
        return raw

    def _read_hardware(self) -> Optional[dict]:
        try:
            from envirophat import light, motion, weather
            r, g, b = light.rgb()
            return {
                "temperature": weather.temperature(),
                "pressure":    weather.pressure(),
                "light":       light.light(),
                "rgb":         (r, g, b),
                "motion":      any(abs(v) > 0.1 for v in motion.accelerometer()),
            }
        except ImportError:
            return None
        except Exception as e:
            log.warning(f"envirophat read error: {e}")
            return None

    def _read_simulated(self) -> dict:
        t = time.time()
        cycle = math.sin(t / 3600 * math.pi)
        return {
            "temperature": -5.0 + cycle * 8 + random.gauss(0, 0.3),
            "pressure":    1013.0 + random.gauss(0, 0.5),
            "light":       max(0, 500 * (cycle + 1) + random.gauss(0, 20)),
            "rgb":         (random.gauss(120, 5), random.gauss(130, 5), random.gauss(160, 5)),
            "motion":      random.random() < 0.05,
            "humidity":    65 + random.gauss(0, 2),
        }

    # ── interpret ─────────────────────────────────────────────────────────────

    def interpret(self, raw: dict) -> Optional[ObservationMessage]:
        temp = raw["temperature"]
        pressure = raw["pressure"]
        light_val = raw["light"]
        motion = raw.get("motion", False)

        observation_type, summary, confidence = self._classify(
            temp, pressure, light_val, motion, raw
        )

        self._prev_temp = temp

        return ObservationMessage(
            agent_id=self.agent_id,
            observation_type=observation_type,
            semantic_summary=summary,
            confidence=confidence,
            raw_data={k: v for k, v in raw.items() if k != "rgb"},
        )

    def _classify(self, temp, pressure, light_val, motion, raw):
        # Detect notable conditions in priority order
        if self._prev_temp is not None:
            delta = temp - self._prev_temp
            if abs(delta) > 2.0:
                direction = "rising" if delta > 0 else "falling"
                return (
                    "thermal_change",
                    f"Temperature {direction} rapidly: {self._prev_temp:.1f}°C → "
                    f"{temp:.1f}°C ({delta:+.1f}°C since last reading)",
                    0.90,
                )

        if motion:
            return (
                "motion_detected",
                f"Movement detected. Ambient: {temp:.1f}°C, "
                f"light {light_val:.0f} lux, pressure {pressure:.0f} hPa",
                0.85,
            )

        if temp < -10:
            return (
                "extreme_cold",
                f"Extreme cold: {temp:.1f}°C. Pressure {pressure:.0f} hPa.",
                0.88,
            )

        if light_val > 800:
            return (
                "high_light",
                f"High light level {light_val:.0f} lux at {temp:.1f}°C — "
                f"possible direct sunlight or artificial source.",
                0.75,
            )

        # Quiet nominal reading
        return (
            "nominal_conditions",
            f"Stable conditions: {temp:.1f}°C, {pressure:.0f} hPa, "
            f"{light_val:.0f} lux ({raw.get('source', 'unknown')})",
            0.70,
        )


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s"
    )
    agent = EnviroAgent(
        name="envirophat-lab",
        location="Yukon field station",
        sense_interval=15,
    )
    agent.start()
