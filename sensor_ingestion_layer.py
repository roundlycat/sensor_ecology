"""
Sensor Ingestion Layer
======================
Reads from physical hardware, detects perceptual events, and feeds the
embedding pipeline. Designed for Raspberry Pi 5 with the Canonical Sensor
Spine v1.0 component set.

Architecture:
    Hardware  ->  SensorPoller (per domain)  ->  EventDetector
                                                        |
                                              PerceptualEmbeddingPipeline

Each sensor domain runs its own async polling loop at a rate appropriate
to its perceptual character:
    - Environmental field: slow (5-30s), continuous field
    - Embodied state:      medium (1-5s), agent vitals
    - Relational contact:  fast (50-100ms), event-driven
    - High-bandwidth:      on-demand / interrupt-driven

Event detection uses a change threshold model: readings are buffered and
compared against a rolling baseline. When deviation exceeds threshold,
a fused event is emitted to the pipeline. This prevents flooding the
database with unremarkable sensor data while capturing meaningful
perceptual events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import functools
from typing import Callable, Optional
from uuid import UUID


import paho.mqtt.client as mqtt

from perceptual_embedding_pipeline import (
    AgentState,
    FusionConfidence,
    PerceptualEmbeddingPipeline,
    SensorDomain,
    SensorReading,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event detection primitives
# ---------------------------------------------------------------------------

@dataclass
class ChangeThreshold:
    """
    Defines what constitutes a noteworthy change for a given channel.
    absolute: minimum absolute change from baseline to trigger
    relative: minimum fractional change from baseline to trigger
    Either condition triggers; both are checked.
    """
    channel:    str
    absolute:   float
    relative:   float = 0.05     # 5% change default
    direction:  str   = "either" # "up", "down", "either"


@dataclass
class DomainThresholds:
    """Threshold set for one sensor domain."""
    channels:       list[ChangeThreshold]
    # How many channels must exceed threshold to fire an event
    quorum:         int   = 1
    # Minimum seconds between events from this domain (cooldown)
    cooldown_s:     float = 10.0


class RollingBaseline:
    """
    Maintains a rolling baseline (median of last N readings) per channel.
    Median rather than mean — more resistant to spike contamination.
    """

    def __init__(self, window: int = 20):
        self.window   = window
        self._buffers: dict[str, deque[float]] = {}

    def update(self, channel: str, value: float) -> None:
        if channel not in self._buffers:
            self._buffers[channel] = deque(maxlen=self.window)
        self._buffers[channel].append(value)

    def baseline(self, channel: str) -> Optional[float]:
        buf = self._buffers.get(channel)
        if not buf or len(buf) < 3:
            return None
        return statistics.median(buf)

    def deviation(self, channel: str, value: float) -> tuple[float, float]:
        """Returns (absolute_deviation, relative_deviation)."""
        base = self.baseline(channel)
        if base is None:
            return 0.0, 0.0
        abs_dev = abs(value - base)
        rel_dev = abs_dev / abs(base) if base != 0 else 0.0
        return abs_dev, rel_dev

    def is_stable(self, channel: str) -> bool:
        buf = self._buffers.get(channel)
        return buf is not None and len(buf) >= self.window // 2


# ---------------------------------------------------------------------------
# Base sensor poller
# ---------------------------------------------------------------------------

class SensorPoller(ABC):
    """
    Abstract base for one sensor domain's polling loop.
    Subclasses implement _read_hardware() and define thresholds.
    """

    def __init__(
        self,
        domain:            SensorDomain,
        thresholds:        DomainThresholds,
        poll_interval_s:   float,
        pipeline:          PerceptualEmbeddingPipeline,
        agent_state_fn:    Callable[[], AgentState],
        db_writer,                                      # SensorReadingWriter
        baseline_window:   int = 20,
    ):
        self.domain          = domain
        self.thresholds      = thresholds
        self.poll_interval   = poll_interval_s
        self.pipeline        = pipeline
        self.agent_state_fn  = agent_state_fn
        self.db_writer       = db_writer
        self.baseline        = RollingBaseline(baseline_window)
        self._last_event_t   = 0.0
        self._running        = False

    @abstractmethod
    async def _read_hardware(self) -> list[SensorReading]:
        """Read from hardware. Return empty list on transient failure."""
        ...

    @abstractmethod
    def _label_event(self, readings: list[SensorReading]) -> str:
        """Generate a human-readable label for the detected event."""
        ...

    async def run(self) -> None:
        self._running = True
        logger.info("Poller started: %s (interval=%.1fs)", self.domain.value, self.poll_interval)
        while self._running:
            try:
                await self._poll_cycle()
            except Exception as exc:
                logger.exception("Poller error (%s): %s", self.domain.value, exc)
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False

    async def _poll_cycle(self) -> None:
        readings = await self._read_hardware()
        if not readings:
            return

        # Write raw readings to DB regardless of event detection
        persisted = await self.db_writer.write_readings(readings)

        # Update baselines
        for r in readings:
            if r.quality_flag == 0:
                self.baseline.update(r.channel, r.raw_value)

        # Check for event-worthy deviation
        triggered, confidence = self._check_thresholds(readings)
        if not triggered:
            return

        # Respect cooldown
        now = time.monotonic()
        if now - self._last_event_t < self.thresholds.cooldown_s:
            return
        self._last_event_t = now

        label = self._label_event(readings)
        agent_state = self.agent_state_fn()

        logger.info("Event detected: %s / %s", self.domain.value, label)
        await self.pipeline.process(
            readings=persisted,
            domain=self.domain,
            agent_state=agent_state,
            event_label=label,
            confidence=confidence,
        )

    def _check_thresholds(
        self, readings: list[SensorReading]
    ) -> tuple[bool, FusionConfidence]:
        by_channel = {r.channel: r.raw_value for r in readings if r.quality_flag == 0}
        triggered_count = 0

        for ct in self.thresholds.channels:
            value = by_channel.get(ct.channel)
            if value is None:
                continue
            abs_dev, rel_dev = self.baseline.deviation(ct.channel, value)
            exceeded = (
                abs_dev >= ct.absolute or rel_dev >= ct.relative
            )
            if ct.direction == "up":
                exceeded = exceeded and (value > (self.baseline.baseline(ct.channel) or 0))
            elif ct.direction == "down":
                exceeded = exceeded and (value < (self.baseline.baseline(ct.channel) or 0))

            if exceeded:
                triggered_count += 1

        if triggered_count == 0:
            return False, FusionConfidence.LOW

        quorum_met = triggered_count >= self.thresholds.quorum
        if not quorum_met:
            return False, FusionConfidence.LOW

        # Confidence scales with how many channels agreed
        if triggered_count >= len(self.thresholds.channels):
            confidence = FusionConfidence.HIGH
        elif triggered_count >= self.thresholds.quorum + 1:
            confidence = FusionConfidence.MODERATE
        else:
            confidence = FusionConfidence.LOW

        return True, confidence


# ---------------------------------------------------------------------------
# Environmental field poller  (BME688 + SHT35)
# ---------------------------------------------------------------------------

class EnvironmentalFieldPoller(SensorPoller):
    """
    Reads BME688 (primary) and SHT35 (calibration reference).
    Computes sensor agreement and uses discrepancy as a quality signal.
    """

    # Typical Yukon winter range: -40 to +5C exterior, 18-22C interior.
    # Thresholds are conservative — we want genuine field changes, not noise.
    DEFAULT_THRESHOLDS = DomainThresholds(
        channels=[
            ChangeThreshold("temperature",  absolute=0.5,  relative=0.02),
            ChangeThreshold("humidity",      absolute=3.0,  relative=0.05),
            ChangeThreshold("pressure",      absolute=0.5,  relative=0.001),
            ChangeThreshold("voc_index",     absolute=10.0, relative=0.10),
        ],
        quorum=2,
        cooldown_s=15.0,
    )

    def __init__(self, pipeline, agent_state_fn, db_writer, **kwargs):
        super().__init__(
            domain=SensorDomain.ENVIRONMENTAL_FIELD,
            thresholds=self.DEFAULT_THRESHOLDS,
            poll_interval_s=10.0,
            pipeline=pipeline,
            agent_state_fn=agent_state_fn,
            db_writer=db_writer,
            **kwargs,
        )
        self._bme = None
        self._sht = None

    async def _init_hardware(self) -> None:
        """Call once at startup, not in the constructor (avoids import-time I2C errors)."""
        try:
            import board
            import busio
            import adafruit_bme680
            import adafruit_sht31d

            i2c = busio.I2C(board.SCL, board.SDA)
            self._bme = adafruit_bme680.Adafruit_BME680_I2C(i2c, address=0x77)
            self._bme.sea_level_pressure = 1013.25
            self._sht = adafruit_sht31d.SHT31D(i2c, address=0x44)
            logger.info("BME688 + SHT35 initialised on I2C")
        except Exception as exc:
            logger.warning("Hardware init failed, using simulation mode: %s", exc)

    async def _read_hardware(self) -> list[SensorReading]:
        readings = []
        now = datetime.now(timezone.utc)

        if self._bme is None:
            # Simulation mode for development without hardware
            import random
            readings = [
                SensorReading("BME688", "temperature", -15.0 + random.gauss(0, 0.1),
                              "degC", SensorDomain.ENVIRONMENTAL_FIELD, recorded_at=now),
                SensorReading("BME688", "humidity",    68.0 + random.gauss(0, 0.5),
                              "%", SensorDomain.ENVIRONMENTAL_FIELD, recorded_at=now),
                SensorReading("BME688", "pressure",    970.0 + random.gauss(0, 0.2),
                              "hPa", SensorDomain.ENVIRONMENTAL_FIELD, recorded_at=now),
                SensorReading("BME688", "voc_index",   80.0 + random.gauss(0, 2),
                              "index", SensorDomain.ENVIRONMENTAL_FIELD, recorded_at=now),
            ]
            return readings

        try:
            readings.extend([
                SensorReading("BME688", "temperature", self._bme.temperature,
                              "degC", SensorDomain.ENVIRONMENTAL_FIELD, recorded_at=now),
                SensorReading("BME688", "humidity",    self._bme.humidity,
                              "%", SensorDomain.ENVIRONMENTAL_FIELD, recorded_at=now),
                SensorReading("BME688", "pressure",    self._bme.pressure,
                              "hPa", SensorDomain.ENVIRONMENTAL_FIELD, recorded_at=now),
                SensorReading("BME688", "voc_index",   self._bme.gas,
                              "ohm", SensorDomain.ENVIRONMENTAL_FIELD, recorded_at=now),
            ])
        except Exception as exc:
            logger.warning("BME688 read error: %s", exc)

        if self._sht:
            try:
                sht_temp = self._sht.temperature
                sht_hum  = self._sht.relative_humidity

                # Compute calibration discrepancy and flag if large
                bme_temps = [r.raw_value for r in readings if r.channel == "temperature"]
                if bme_temps:
                    drift = abs(bme_temps[0] - sht_temp)
                    quality = 1 if drift > 1.0 else 0   # flag if >1°C disagreement
                    readings.append(SensorReading(
                        "SHT35", "temperature", sht_temp,
                        "degC", SensorDomain.ENVIRONMENTAL_FIELD,
                        quality_flag=quality, recorded_at=now,
                    ))
                readings.append(SensorReading(
                    "SHT35", "humidity", sht_hum,
                    "%", SensorDomain.ENVIRONMENTAL_FIELD, recorded_at=now,
                ))
            except Exception as exc:
                logger.warning("SHT35 read error: %s", exc)

        return readings

    def _label_event(self, readings: list[SensorReading]) -> str:
        by_channel = {r.channel: r.raw_value for r in readings if r.quality_flag == 0}
        temp  = by_channel.get("temperature", 0)
        btemp = self.baseline.baseline("temperature") or temp
        pres  = by_channel.get("pressure", 1013)
        bpres = self.baseline.baseline("pressure") or pres
        voc   = by_channel.get("voc_index", 0)
        bvoc  = self.baseline.baseline("voc_index") or voc

        parts = []
        if abs(temp - btemp) >= 0.5:
            parts.append("temperature_drop" if temp < btemp else "temperature_rise")
        if abs(pres - bpres) >= 0.5:
            parts.append("pressure_fall" if pres < bpres else "pressure_rise")
        if abs(voc - bvoc) >= 10:
            parts.append("voc_spike" if voc > bvoc else "voc_drop")

        return "_".join(parts) if parts else "environmental_shift"


# ---------------------------------------------------------------------------
# Embodied state poller  (ICM-42688-P + INA219 + thermistor)
# ---------------------------------------------------------------------------

class EmbodiedStatePoller(SensorPoller):
    """
    Reads IMU, power monitor, and thermal sensors.
    Also reads CPU load and board temperature from the OS — these are
    first-class embodied signals, not just diagnostics.
    """

    DEFAULT_THRESHOLDS = DomainThresholds(
        channels=[
            ChangeThreshold("acceleration_magnitude", absolute=0.15, relative=0.10),
            ChangeThreshold("current_mA",             absolute=150,  relative=0.15),
            ChangeThreshold("cpu_temp_c",             absolute=3.0,  relative=0.05),
        ],
        quorum=1,
        cooldown_s=5.0,
    )

    def __init__(self, pipeline, agent_state_fn, db_writer, state_updater_fn=None, **kwargs):
        super().__init__(
            domain=SensorDomain.EMBODIED_STATE,
            thresholds=self.DEFAULT_THRESHOLDS,
            poll_interval_s=2.0,
            pipeline=pipeline,
            agent_state_fn=agent_state_fn,
            db_writer=db_writer,
            **kwargs,
        )
        self._ina = None
        self._imu = None
        self._state_updater = state_updater_fn

    async def _init_hardware(self) -> None:
        try:
            import board
            import busio
            from adafruit_ina219 import INA219

            i2c = busio.I2C(board.SCL, board.SDA)
            self._ina = INA219(i2c, addr=0x40)
            logger.info("INA219 initialised")
        except Exception as exc:
            logger.warning("INA219 init failed: %s", exc)

        try:
            import spidev
            # ICM-42688-P via SPI — adapt to your wiring
            # Using icm42688 library if available, else raw SPI
            import icm42688
            self._imu = icm42688.ICM42688(spidev.SpiDev())
            logger.info("ICM-42688-P initialised")
        except Exception as exc:
            logger.warning("IMU init failed: %s", exc)

    async def _read_hardware(self) -> list[SensorReading]:
        readings = []
        now = datetime.now(timezone.utc)

        # OS-level readings — always available, always meaningful
        cpu_temp = self._read_cpu_temp()
        cpu_load = self._read_cpu_load()
        if cpu_temp is not None:
            readings.append(SensorReading(
                "Pi5_CPU", "cpu_temp_c", cpu_temp,
                "degC", SensorDomain.EMBODIED_STATE, recorded_at=now,
            ))
        if cpu_load is not None:
            readings.append(SensorReading(
                "Pi5_CPU", "cpu_load_pct", cpu_load,
                "%", SensorDomain.EMBODIED_STATE, recorded_at=now,
            ))

        # INA219 power monitor
        if self._ina:
            try:
                readings.extend([
                    SensorReading("INA219", "bus_voltage_V",  self._ina.bus_voltage,
                                  "V", SensorDomain.EMBODIED_STATE, recorded_at=now),
                    SensorReading("INA219", "current_mA",     self._ina.current,
                                  "mA", SensorDomain.EMBODIED_STATE, recorded_at=now),
                    SensorReading("INA219", "power_mW",       self._ina.power * 1000,
                                  "mW", SensorDomain.EMBODIED_STATE, recorded_at=now),
                ])
            except Exception as exc:
                logger.warning("INA219 read error: %s", exc)

        # IMU
        if self._imu:
            try:
                ax, ay, az = self._imu.acceleration
                gx, gy, gz = self._imu.gyro
                mag = (ax**2 + ay**2 + az**2) ** 0.5
                readings.extend([
                    SensorReading("ICM42688", "acceleration_magnitude", mag,
                                  "g", SensorDomain.EMBODIED_STATE, recorded_at=now),
                    SensorReading("ICM42688", "accel_x", ax,
                                  "g", SensorDomain.EMBODIED_STATE, recorded_at=now),
                    SensorReading("ICM42688", "accel_y", ay,
                                  "g", SensorDomain.EMBODIED_STATE, recorded_at=now),
                    SensorReading("ICM42688", "accel_z", az,
                                  "g", SensorDomain.EMBODIED_STATE, recorded_at=now),
                    SensorReading("ICM42688", "gyro_magnitude",
                                  (gx**2 + gy**2 + gz**2) ** 0.5,
                                  "dps", SensorDomain.EMBODIED_STATE, recorded_at=now),
                ])
            except Exception as exc:
                logger.warning("IMU read error: %s", exc)
        else:
            # Simulation
            import random
            readings.append(SensorReading(
                "ICM42688_sim", "acceleration_magnitude", 1.0 + random.gauss(0, 0.01),
                "g", SensorDomain.EMBODIED_STATE, recorded_at=now,
            ))

        # Keep AgentStateProvider current so other pollers see up-to-date vitals
        if self._state_updater is not None:
            by_ch = {r.channel: r.raw_value for r in readings}
            await self._state_updater(
                power_mW=by_ch.get("power_mW"),
                temp_c=by_ch.get("cpu_temp_c"),
                cpu_load_pct=int(by_ch["cpu_load_pct"]) if "cpu_load_pct" in by_ch else None,
            )

        return readings

    def _read_cpu_temp(self) -> Optional[float]:
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                return int(f.read().strip()) / 1000.0
        except Exception:
            return None

    def _read_cpu_load(self) -> Optional[float]:
        try:
            import psutil
            return psutil.cpu_percent(interval=None)
        except Exception:
            return None

    def _label_event(self, readings: list[SensorReading]) -> str:
        by_channel = {r.channel: r.raw_value for r in readings}
        parts = []

        accel = by_channel.get("acceleration_magnitude")
        if accel and accel > 1.5:
            parts.append("impact_detected")
        elif accel and accel < 0.3:
            parts.append("near_stillness")

        current = by_channel.get("current_mA")
        bcurrent = self.baseline.baseline("current_mA")
        if current and bcurrent:
            if current > bcurrent * 1.3:
                parts.append("power_surge")
            elif current < bcurrent * 0.7:
                parts.append("power_drop")

        cpu_temp = by_channel.get("cpu_temp_c")
        bcpu = self.baseline.baseline("cpu_temp_c")
        if cpu_temp and bcpu:
            if cpu_temp > bcpu + 5:
                parts.append("thermal_stress")
            elif cpu_temp < bcpu - 3:
                parts.append("thermal_recovery")

        return "_".join(parts) if parts else "embodied_state_shift"


# ---------------------------------------------------------------------------
# Relational contact poller  (piezo + VCNL4040)
# ---------------------------------------------------------------------------

class RelationalContactPoller(SensorPoller):
    """
    Event-driven contact detection. Polls fast but only emits on genuine contact.
    Piezo: ADC reading via Pi 5 GPIO with a comparator circuit.
    VCNL4040: proximity + ambient light via I2C.
    """

    DEFAULT_THRESHOLDS = DomainThresholds(
        channels=[
            ChangeThreshold("proximity_raw",  absolute=100, relative=0.20),
            ChangeThreshold("piezo_amplitude", absolute=200, relative=0.30),
        ],
        quorum=1,
        cooldown_s=1.0,  # fast cooldown — contact events can be rapid
    )

    # Tap sequence detector state
    TAP_WINDOW_S = 1.5
    TAP_THRESHOLD = 400

    def __init__(self, pipeline, agent_state_fn, db_writer, **kwargs):
        super().__init__(
            domain=SensorDomain.RELATIONAL_CONTACT,
            thresholds=self.DEFAULT_THRESHOLDS,
            poll_interval_s=0.05,   # 20Hz
            pipeline=pipeline,
            agent_state_fn=agent_state_fn,
            db_writer=db_writer,
            **kwargs,
        )
        self._vcnl = None
        self._tap_times: deque[float] = deque(maxlen=10)

    async def _init_hardware(self) -> None:
        try:
            import board
            import busio
            import adafruit_vcnl4040

            i2c = busio.I2C(board.SCL, board.SDA)
            self._vcnl = adafruit_vcnl4040.VCNL4040(i2c)
            logger.info("VCNL4040 initialised")
        except Exception as exc:
            logger.warning("VCNL4040 init failed: %s", exc)

    async def _read_hardware(self) -> list[SensorReading]:
        readings = []
        now = datetime.now(timezone.utc)

        if self._vcnl:
            try:
                readings.extend([
                    SensorReading("VCNL4040", "proximity_raw", self._vcnl.proximity,
                                  "raw", SensorDomain.RELATIONAL_CONTACT, recorded_at=now),
                    SensorReading("VCNL4040", "ambient_light", self._vcnl.light,
                                  "lux", SensorDomain.RELATIONAL_CONTACT, recorded_at=now),
                ])
            except Exception as exc:
                logger.warning("VCNL4040 read error: %s", exc)

        # Piezo via ADC — Pi 5 doesn't have onboard ADC, assume MCP3008 or ADS1115
        piezo_val = self._read_piezo_adc()
        if piezo_val is not None:
            readings.append(SensorReading(
                "Piezo", "piezo_amplitude", piezo_val,
                "raw_adc", SensorDomain.RELATIONAL_CONTACT, recorded_at=now,
            ))
            # Track tap times for pattern detection
            if piezo_val > self.TAP_THRESHOLD:
                self._tap_times.append(time.monotonic())

        return readings

    def _read_piezo_adc(self) -> Optional[float]:
        """
        Read piezo from ADS1115 via I2C. Adapt channel to your wiring.
        Returns None if hardware unavailable (simulation returns 0).
        """
        try:
            import board
            import busio
            import adafruit_ads1x15.ads1115 as ADS
            from adafruit_ads1x15.analog_in import AnalogIn

            i2c = busio.I2C(board.SCL, board.SDA)
            ads = ADS.ADS1115(i2c)
            chan = AnalogIn(ads, ADS.P0)
            return abs(chan.value)
        except Exception:
            return 0.0   # simulation: no contact

    def _detect_tap_pattern(self) -> str:
        now_t = time.monotonic()
        recent = [t for t in self._tap_times if now_t - t < self.TAP_WINDOW_S]
        n = len(recent)
        if n == 1:
            return "single_tap"
        elif n == 2:
            return "double_tap"
        elif n == 3:
            return "triple_tap"
        elif n >= 4:
            return "rapid_sequence"
        return "contact"

    def _label_event(self, readings: list[SensorReading]) -> str:
        by_channel = {r.channel: r.raw_value for r in readings}
        parts = []

        proximity = by_channel.get("proximity_raw", 0)
        if proximity > 200:
            parts.append("close_approach")
        elif proximity > 50:
            parts.append("presence_detected")

        piezo = by_channel.get("piezo_amplitude", 0)
        if piezo > self.TAP_THRESHOLD:
            pattern = self._detect_tap_pattern()
            parts.append(pattern)

        return "_".join(parts) if parts else "contact_event"


# ---------------------------------------------------------------------------
# High-bandwidth poller  (MLX90640 32×24 thermal camera via MQTT)
# ---------------------------------------------------------------------------

class HighBandwidthPoller(SensorPoller):
    """
    Receives MLX90640 32×24 thermal frames published by thermal_publisher.py
    running on the thermal Pi, via MQTT topic ``thermal/{node}/frame``.

    Because frames arrive over MQTT rather than a local I2C bus, the poller
    bridges paho-mqtt's synchronous callback thread to asyncio via an
    asyncio.Queue.  _read_hardware() drains one frame per call; the run()
    loop polls at 100 ms so the queue is emptied quickly without busy-waiting.

    Feature channels extracted per frame:
        max_temp_c          hottest pixel (°C)
        min_temp_c          coldest pixel (°C)
        mean_temp_c         frame mean (°C) — proxy for ambient
        presence_score      fraction of pixels > mean + BODY_DELTA  (0–1)
        thermal_centroid_x  x position of warm region centroid  (0=left, 1=right)
        thermal_centroid_y  y position of warm region centroid  (0=top, 1=bottom)
        frame_delta_rms     RMS °C change from previous frame   (motion proxy)
    """

    FRAME_SIZE  = 768    # 32 × 24
    BODY_DELTA  = 3.5    # °C above mean to qualify as "warm body" pixel

    DEFAULT_THRESHOLDS = DomainThresholds(
        channels=[
            # Presence: a warm body appearing / disappearing / moving
            ChangeThreshold("presence_score",   absolute=0.05, relative=0.15),
            # Max temperature spike (e.g. hand reaching in, radiator cycling)
            ChangeThreshold("max_temp_c",        absolute=1.5,  relative=0.04),
            # Frame-to-frame motion
            ChangeThreshold("frame_delta_rms",   absolute=0.5,  relative=0.20),
        ],
        quorum=1,
        cooldown_s=30.0,
    )

    def __init__(
        self,
        pipeline,
        agent_state_fn,
        db_writer,
        broker_host: str = "localhost",
        broker_port: int = 1883,
        thermal_node_name: str = "+",
        **kwargs,
    ):
        super().__init__(
            domain=SensorDomain.HIGH_BANDWIDTH,
            thresholds=self.DEFAULT_THRESHOLDS,
            poll_interval_s=0.1,       # drain queue fast; threshold gate controls events
            pipeline=pipeline,
            agent_state_fn=agent_state_fn,
            db_writer=db_writer,
            **kwargs,
        )
        self._broker_host       = broker_host
        self._broker_port       = broker_port
        self._thermal_node      = thermal_node_name
        self._topic             = f"thermal/{thermal_node_name}/frame"
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._mqtt_client: Optional[mqtt.Client] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._prev_frame: Optional[list[float]] = None

    # -----------------------------------------------------------------------
    # Hardware init  (subscribe to MQTT on startup)
    # -----------------------------------------------------------------------

    async def _init_hardware(self) -> None:
        self._loop = asyncio.get_running_loop()
        import os
        client = mqtt.Client(
            client_id=f"hbw-poller-{self._thermal_node}-{os.getpid()}",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        client.on_connect    = self._on_mqtt_connect
        client.on_message    = self._on_mqtt_message
        client.on_disconnect = self._on_mqtt_disconnect
        self._mqtt_client = client
        try:
            client.connect(self._broker_host, self._broker_port, keepalive=60)
            client.loop_start()
            logger.info(
                "HighBandwidthPoller: MQTT subscriber connected to %s:%d, topic %s",
                self._broker_host, self._broker_port, self._topic,
            )
        except Exception as exc:
            logger.warning(
                "HighBandwidthPoller: MQTT connect failed (%s) — "
                "thermal frames will not be received until broker is available",
                exc,
            )

    # -----------------------------------------------------------------------
    # paho callbacks  (run in paho's thread — must not touch asyncio directly)
    # -----------------------------------------------------------------------

    def _on_mqtt_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe(self._topic, qos=0)
            logger.info("HighBandwidthPoller: subscribed to %s", self._topic)
        else:
            logger.error("HighBandwidthPoller: broker refused connection: %s", reason_code)

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception as exc:
            logger.warning("HighBandwidthPoller: message parse error: %s", exc)
            return

        if self._loop is None or self._loop.is_closed():
            return

        def _safe_put():
            try:
                self._queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.debug("HighBandwidthPoller: frame queue full, dropping oldest frame")
                try:
                    self._queue.get_nowait()   # discard oldest
                    self._queue.put_nowait(payload)
                except Exception:
                    pass

        self._loop.call_soon_threadsafe(_safe_put)

    def _on_mqtt_disconnect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            logger.warning(
                "HighBandwidthPoller: MQTT disconnected unexpectedly (rc=%s)", reason_code
            )

    # -----------------------------------------------------------------------
    # SensorPoller interface
    # -----------------------------------------------------------------------

    async def _read_hardware(self) -> list[SensorReading]:
        """Drain one frame from the MQTT queue and convert to SensorReadings."""
        try:
            payload = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return []

        frame: list[float] = payload.get("frame", [])
        if len(frame) != self.FRAME_SIZE:
            logger.warning(
                "HighBandwidthPoller: unexpected frame size %d (expected %d)",
                len(frame), self.FRAME_SIZE,
            )
            return []

        ts_str = payload.get("ts")
        try:
            ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now(timezone.utc)
        except Exception:
            ts = datetime.now(timezone.utc)

        return self._frame_to_readings(frame, ts)

    def _frame_to_readings(
        self, frame: list[float], ts: datetime
    ) -> list[SensorReading]:
        mean_temp = statistics.mean(frame)
        max_temp  = max(frame)
        min_temp  = min(frame)

        # Presence score: fraction of pixels that qualify as a warm body.
        warm_pixels = [
            (i, v) for i, v in enumerate(frame)
            if v > mean_temp + self.BODY_DELTA
        ]
        presence_score = len(warm_pixels) / self.FRAME_SIZE

        # Centre of mass of warm region (row = y, col = x, both normalised 0–1).
        cx = cy = 0.0
        if warm_pixels:
            total_w = sum(v - mean_temp for _, v in warm_pixels)
            if total_w > 0:
                for idx, v in warm_pixels:
                    w   = v - mean_temp
                    row = idx // 32   # 0–23
                    col = idx  % 32   # 0–31
                    cx += w * (col / 31.0)
                    cy += w * (row / 23.0)
                cx /= total_w
                cy /= total_w

        # RMS frame delta — measures thermal motion between consecutive frames.
        delta_rms = 0.0
        if self._prev_frame is not None and len(self._prev_frame) == self.FRAME_SIZE:
            delta_rms = math.sqrt(
                sum((a - b) ** 2 for a, b in zip(frame, self._prev_frame))
                / self.FRAME_SIZE
            )
        self._prev_frame = frame[:]

        domain = SensorDomain.HIGH_BANDWIDTH
        return [
            SensorReading("MLX90640", "max_temp_c",          max_temp,       "degC",    domain, recorded_at=ts),
            SensorReading("MLX90640", "min_temp_c",          min_temp,       "degC",    domain, recorded_at=ts),
            SensorReading("MLX90640", "mean_temp_c",         mean_temp,      "degC",    domain, recorded_at=ts),
            SensorReading("MLX90640", "presence_score",      presence_score, "ratio",   domain, recorded_at=ts),
            SensorReading("MLX90640", "thermal_centroid_x",  cx,             "norm",    domain, recorded_at=ts),
            SensorReading("MLX90640", "thermal_centroid_y",  cy,             "norm",    domain, recorded_at=ts),
            SensorReading("MLX90640", "frame_delta_rms",     delta_rms,      "degC_rms",domain, recorded_at=ts),
        ]

    def _label_event(self, readings: list[SensorReading]) -> str:
        by_ch   = {r.channel: r.raw_value for r in readings if r.quality_flag == 0}
        parts   = []

        presence = by_ch.get("presence_score", 0.0)
        b_pres   = self.baseline.baseline("presence_score") or 0.0
        if presence > 0.15 and presence > b_pres + 0.05:
            parts.append("presence_detected")
        elif presence < 0.02 and b_pres > 0.10:
            parts.append("presence_departed")

        max_t = by_ch.get("max_temp_c", 0.0)
        b_max = self.baseline.baseline("max_temp_c") or max_t
        if max_t > b_max + 2.0:
            parts.append("thermal_approach")
        elif max_t < b_max - 2.0:
            parts.append("thermal_retreat")

        delta = by_ch.get("frame_delta_rms", 0.0)
        if delta > 1.5:
            parts.append("thermal_motion")

        return "_".join(parts) if parts else "thermal_shift"

    def stop(self) -> None:
        super().stop()
        if self._mqtt_client:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()


# ---------------------------------------------------------------------------
# MXChip AZ3166 acoustic listener poller
# Bridges sensors/mxchip/status and sensors/mxchip/motif into the pipeline.
# The device runs its own on-board classifier and publishes interpreted state,
# not raw PCM — so readings are confidence scores, not sample amplitudes.
# ---------------------------------------------------------------------------

class MXChipAcousticPoller(SensorPoller):
    """
    Subscribes to the MXChip acoustic listener's two MQTT streams:

        sensors/mxchip/status  — periodic confidence snapshot (30 s–5 min,
                                 adaptive by stability).  Processed through
                                 the normal threshold gate.
        sensors/mxchip/motif   — concept formation event, published immediately
                                 when the on-device classifier promotes a new
                                 acoustic concept.  Bypasses the gate and is
                                 forwarded to the pipeline with HIGH confidence.

    The MXChip is registered as its own agent_node so its readings are
    attributed to the physical device rather than the Pi.
    """

    NODE_NAME = "mxchip-acoustic"
    NODE_TYPE = "mxchip_az3166"

    DEFAULT_THRESHOLDS = DomainThresholds(
        channels=[
            ChangeThreshold("stability",    absolute=0.08, relative=0.10),
            ChangeThreshold("broadband_c",  absolute=0.10, relative=0.15),
            ChangeThreshold("transient_c",  absolute=0.10, relative=0.15),
        ],
        quorum=1,
        cooldown_s=30.0,
    )

    def __init__(
        self,
        pipeline,
        agent_state_fn,
        db_writer,
        broker_host: str = "localhost",
        broker_port: int = 1883,
        pool=None,          # asyncpg pool — used to register the MXChip node
        **kwargs,
    ):
        super().__init__(
            domain=SensorDomain.HIGH_BANDWIDTH,
            thresholds=self.DEFAULT_THRESHOLDS,
            poll_interval_s=0.5,    # drain queue at 2 Hz
            pipeline=pipeline,
            agent_state_fn=agent_state_fn,
            db_writer=db_writer,
            **kwargs,
        )
        self._broker_host    = broker_host
        self._broker_port    = broker_port
        self._pool           = pool
        self._status_queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._concept_queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._mqtt_client: Optional[mqtt.Client] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def _init_hardware(self) -> None:
        self._loop = asyncio.get_running_loop()

        # Register the MXChip as its own agent_node and switch the writer to it.
        if self._pool is not None:
            node_id = await self._ensure_node_registered()
            self.db_writer = SensorReadingWriter(self._pool, node_id)
            logger.info(
                "MXChipAcousticPoller: node '%s' → %s", self.NODE_NAME, node_id
            )

        import os
        client = mqtt.Client(
            client_id=f"mxchip-poller-{os.getpid()}",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        client.on_connect    = self._on_mqtt_connect
        client.on_message    = self._on_mqtt_message
        client.on_disconnect = self._on_mqtt_disconnect
        self._mqtt_client = client
        try:
            client.connect(self._broker_host, self._broker_port, keepalive=60)
            client.loop_start()
            logger.info(
                "MXChipAcousticPoller: connected to broker %s:%d",
                self._broker_host, self._broker_port,
            )
        except Exception as exc:
            logger.warning("MXChipAcousticPoller: MQTT connect failed: %s", exc)

    async def _ensure_node_registered(self) -> UUID:
        """Upsert the MXChip as an agent_node and return its UUID."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_nodes (node_name, node_type, location_label)
                VALUES ($1, $2, $3)
                ON CONFLICT (node_name) DO UPDATE
                    SET last_heartbeat_at = NOW()
                RETURNING id
                """,
                self.NODE_NAME, self.NODE_TYPE, "lab",
            )
        return row["id"]

    # ── paho callbacks (run in paho's thread) ─────────────────────────────────

    def _on_mqtt_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe("sensors/mxchip/#", qos=0)
            logger.info("MXChipAcousticPoller: subscribed to sensors/mxchip/#")
        else:
            logger.error(
                "MXChipAcousticPoller: broker refused connection: %s", reason_code
            )

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception as exc:
            logger.warning("MXChipAcousticPoller: parse error: %s", exc)
            return

        if self._loop is None or self._loop.is_closed():
            return

        def _safe_put(queue, item):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(item)
                except Exception:
                    pass

        if msg.topic.endswith("/status"):
            self._loop.call_soon_threadsafe(_safe_put, self._status_queue, payload)
        elif msg.topic.endswith("/motif"):
            self._loop.call_soon_threadsafe(_safe_put, self._concept_queue, payload)

    def _on_mqtt_disconnect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            logger.warning(
                "MXChipAcousticPoller: disconnected unexpectedly (rc=%s)", reason_code
            )

    # ── SensorPoller interface ────────────────────────────────────────────────

    async def _read_hardware(self) -> list[SensorReading]:
        """Drain one status message and convert to SensorReadings."""
        try:
            payload = self._status_queue.get_nowait()
        except asyncio.QueueEmpty:
            return []

        now    = datetime.now(timezone.utc)
        domain = SensorDomain.HIGH_BANDWIDTH
        return [
            SensorReading("MXChip_mic", "baseline_c",  float(payload.get("baseline_c",  0.0)), "confidence", domain, recorded_at=now),
            SensorReading("MXChip_mic", "hum_c",       float(payload.get("hum_c",       0.0)), "confidence", domain, recorded_at=now),
            SensorReading("MXChip_mic", "broadband_c", float(payload.get("broadband_c", 0.0)), "confidence", domain, recorded_at=now),
            SensorReading("MXChip_mic", "transient_c", float(payload.get("transient_c", 0.0)), "confidence", domain, recorded_at=now),
            SensorReading("MXChip_mic", "stability",   float(payload.get("stability",   0.0)), "ratio",      domain, recorded_at=now),
        ]

    def _label_event(self, readings: list[SensorReading]) -> str:
        by_ch     = {r.channel: r.raw_value for r in readings}
        stability = by_ch.get("stability",   0.0)
        transient = by_ch.get("transient_c", 0.0)
        broadband = by_ch.get("broadband_c", 0.0)
        if transient > 0.7:
            return "acoustic_transient_detected"
        if broadband > 0.7:
            return "acoustic_broadband_activity"
        if stability > 0.8:
            return "acoustic_field_settled"
        if stability < 0.3:
            return "acoustic_field_unsettled"
        return "acoustic_state_shift"

    # ── Extended run loop — drains concept events before each poll cycle ───────

    async def run(self) -> None:
        self._running = True
        logger.info("MXChipAcousticPoller started (poll=%.1fs)", self.poll_interval)
        while self._running:
            try:
                await self._drain_concept_events()
                await self._poll_cycle()
            except Exception as exc:
                logger.exception("MXChipAcousticPoller error: %s", exc)
            await asyncio.sleep(self.poll_interval)

    async def _drain_concept_events(self) -> None:
        """Forward concept formation events to the pipeline with HIGH confidence."""
        while True:
            try:
                payload = self._concept_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            now    = datetime.now(timezone.utc)
            domain = SensorDomain.HIGH_BANDWIDTH
            readings = [
                SensorReading("MXChip_mic", "rms",       float(payload.get("rms",       0.0)), "",      domain, recorded_at=now),
                SensorReading("MXChip_mic", "hum",       float(payload.get("hum",       0.0)), "",      domain, recorded_at=now),
                SensorReading("MXChip_mic", "texture",   float(payload.get("texture",   0.0)), "",      domain, recorded_at=now),
                SensorReading("MXChip_mic", "transient", float(payload.get("transient", 0.0)), "",      domain, recorded_at=now),
                SensorReading("MXChip_mic", "stability", float(payload.get("stability", 0.0)), "count", domain, recorded_at=now),
            ]
            persisted   = await self.db_writer.write_readings(readings)
            agent_state = self.agent_state_fn()
            logger.info(
                "MXChipAcousticPoller: concept formed — total=%s texture=%.3f",
                payload.get("total_concepts", "?"), payload.get("texture", 0.0),
            )
            await self.pipeline.process(
                readings=persisted,
                domain=domain,
                agent_state=agent_state,
                event_label="acoustic_concept_formed",
                confidence=FusionConfidence.HIGH,
            )

    def stop(self) -> None:
        super().stop()
        if self._mqtt_client:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()


# ---------------------------------------------------------------------------
# Agent state provider
# Central, mutable store for the agent's current metabolic state.
# Updated by EmbodiedStatePoller; read by all other pollers when emitting events.
# ---------------------------------------------------------------------------

class AgentStateProvider:
    """
    Shared mutable state for agent vitals. Thread-safe via asyncio.Lock.
    All pollers call this to get the metabolic context at event time.
    """

    def __init__(self):
        self._state = AgentState()
        self._lock  = asyncio.Lock()

    async def update(self, power_mW=None, temp_c=None, cpu_load_pct=None) -> None:
        async with self._lock:
            if power_mW    is not None: self._state.power_mW      = power_mW
            if temp_c      is not None: self._state.temp_c        = temp_c
            if cpu_load_pct is not None: self._state.cpu_load_pct = cpu_load_pct

    def get(self) -> AgentState:
        # Snapshot — no lock needed for read of simple Python object
        return AgentState(
            power_mW=self._state.power_mW,
            temp_c=self._state.temp_c,
            cpu_load_pct=self._state.cpu_load_pct,
        )


# ---------------------------------------------------------------------------
# Sensor reading DB writer
# Persists raw readings and returns them with db_id populated for the pipeline
# ---------------------------------------------------------------------------

class SensorReadingWriter:

    def __init__(self, pool, agent_node_id: UUID):
        self.pool = pool
        self.agent_node_id = agent_node_id

    async def write_readings(
        self, readings: list[SensorReading]
    ) -> list[SensorReading]:
        """
        Insert sensor readings and return copies with db_id populated.
        """
        if not readings:
            return []

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                INSERT INTO sensor_readings
                    (agent_node_id, domain, sensor_label, channel,
                     raw_value, unit, quality_flag, recorded_at)
                SELECT
                    r.agent_node_id, r.domain::sensor_domain, r.sensor_label, r.channel,
                    r.raw_value, r.unit, r.quality_flag, r.recorded_at
                FROM jsonb_to_recordset($1::jsonb) AS r(
                    agent_node_id uuid,
                    domain        text,
                    sensor_label  text,
                    channel       text,
                    raw_value     float8,
                    unit          text,
                    quality_flag  smallint,
                    recorded_at   timestamptz
                )
                RETURNING id
                """,
                # asyncpg needs a JSON string here
                __import__("json").dumps([
                    {
                        "agent_node_id": str(self.agent_node_id),
                        "domain":       r.domain.value,
                        "sensor_label": r.sensor_label,
                        "channel":      r.channel,
                        "raw_value":    r.raw_value,
                        "unit":         r.unit,
                        "quality_flag": r.quality_flag,
                        "recorded_at":  r.recorded_at.isoformat(),
                    }
                    for r in readings
                ]),
            )

        result = []
        for reading, row in zip(readings, rows):
            r2 = SensorReading(
                sensor_label=reading.sensor_label,
                channel=reading.channel,
                raw_value=reading.raw_value,
                unit=reading.unit,
                domain=reading.domain,
                quality_flag=reading.quality_flag,
                recorded_at=reading.recorded_at,
                db_id=row["id"],
            )
            result.append(r2)
        return result


# ---------------------------------------------------------------------------
# Ingestion coordinator
# Starts and supervises all poller loops
# ---------------------------------------------------------------------------

class IngestionCoordinator:
    """
    Owns all sensor pollers and runs them as concurrent asyncio tasks.
    Handles graceful shutdown and poller restart on failure.
    """

    def __init__(
        self,
        pipeline:           PerceptualEmbeddingPipeline,
        db_pool,
        agent_node_id:      UUID,
        thermal_node_name:  Optional[str] = None,
        mqtt_broker_host:   str = "localhost",
        mqtt_broker_port:   int = 1883,
        enable_mxchip:      bool = False,
    ):
        self.pipeline          = pipeline
        self.pool              = db_pool
        self.agent_node_id     = agent_node_id
        self.thermal_node_name = thermal_node_name
        self.mqtt_broker_host  = mqtt_broker_host
        self.mqtt_broker_port  = mqtt_broker_port
        self.enable_mxchip     = enable_mxchip
        self.state_provider    = AgentStateProvider()
        self._writer           = SensorReadingWriter(db_pool, agent_node_id)
        self._pollers: list[SensorPoller] = []
        self._tasks:   list[asyncio.Task] = []

    def _build_pollers(self) -> list[SensorPoller]:
        state_fn = self.state_provider.get
        common = dict(
            pipeline=self.pipeline,
            agent_state_fn=state_fn,
            db_writer=self._writer,
        )
        pollers: list[SensorPoller] = [
            EnvironmentalFieldPoller(**common),
            EmbodiedStatePoller(**common, state_updater_fn=self.state_provider.update),
            RelationalContactPoller(**common),
        ]
        if self.thermal_node_name:
            pollers.append(HighBandwidthPoller(
                **common,
                broker_host=self.mqtt_broker_host,
                broker_port=self.mqtt_broker_port,
                thermal_node_name=self.thermal_node_name,
            ))
            logger.info(
                "HighBandwidthPoller enabled for thermal node '%s'",
                self.thermal_node_name,
            )
        if self.enable_mxchip:
            pollers.append(MXChipAcousticPoller(
                **common,
                broker_host=self.mqtt_broker_host,
                broker_port=self.mqtt_broker_port,
                pool=self.pool,
            ))
            logger.info("MXChipAcousticPoller enabled")
        return pollers

    async def _heartbeat_loop(self) -> None:
        """Periodically stamp last_heartbeat_at so the monitor shows this node as online."""
        while True:
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE agent_nodes SET last_heartbeat_at = NOW() WHERE id = $1",
                        self.agent_node_id,
                    )
            except Exception as exc:
                logger.warning("Heartbeat update failed: %s", exc)
            await asyncio.sleep(30)

    async def start(self) -> None:
        self._pollers = self._build_pollers()

        # Hardware init for each poller
        for poller in self._pollers:
            if hasattr(poller, "_init_hardware"):
                await poller._init_hardware()

        # Launch polling loops
        for poller in self._pollers:
            task = asyncio.create_task(
                self._supervised(poller),
                name=f"poller_{poller.domain.value}",
            )
            self._tasks.append(task)

        # Heartbeat task — keeps last_heartbeat_at current for the monitor
        self._tasks.append(asyncio.create_task(
            self._heartbeat_loop(), name="heartbeat",
        ))

        logger.info(
            "IngestionCoordinator started: %d pollers active", len(self._pollers)
        )

    async def _supervised(self, poller: SensorPoller) -> None:
        """Restart a poller if it crashes unexpectedly."""
        while True:
            try:
                await poller.run()
            except asyncio.CancelledError:
                logger.info("Poller cancelled: %s", poller.domain.value)
                return
            except Exception as exc:
                logger.exception(
                    "Poller %s crashed, restarting in 5s: %s",
                    poller.domain.value, exc,
                )
                await asyncio.sleep(5)

    async def stop(self) -> None:
        for poller in self._pollers:
            poller.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("IngestionCoordinator stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    import os
    import asyncpg
    from uuid import UUID

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    DB_URL             = os.environ["DATABASE_URL"]
    NODE_ID            = UUID(os.environ["AGENT_NODE_ID"])
    USE_LOCAL          = os.environ.get("USE_LOCAL_EMBEDDER", "true").lower() == "true"
    THERMAL_NODE       = os.environ.get("THERMAL_NODE_NAME")          # e.g. "pi5-thermal"
    MQTT_BROKER_HOST   = os.environ.get("MQTT_BROKER_HOST", "localhost")
    MQTT_BROKER_PORT   = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
    KANBAN_BOARD_ID    = os.environ.get("KANBAN_BOARD_ID")
    ENABLE_MXCHIP      = os.environ.get("ENABLE_MXCHIP", "false").lower() == "true"

    pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10)

    hook = None
    if KANBAN_BOARD_ID:
        from perceptual_embedding_pipeline import kanban_hook
        hook = functools.partial(kanban_hook, pool, KANBAN_BOARD_ID)

    pipeline = await PerceptualEmbeddingPipeline.build(
        pool=pool,
        agent_node_id=NODE_ID,
        use_local_embedder=USE_LOCAL,
        kanban_hook=hook 
   )

    coordinator = IngestionCoordinator(
        pipeline=pipeline,
        db_pool=pool,
        agent_node_id=NODE_ID,
        thermal_node_name=THERMAL_NODE,
        mqtt_broker_host=MQTT_BROKER_HOST,
        mqtt_broker_port=MQTT_BROKER_PORT,
        enable_mxchip=ENABLE_MXCHIP,
    )

    await coordinator.start()

    try:
        # Run until interrupted
        await asyncio.get_event_loop().create_future()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutdown requested")
    finally:
        await coordinator.stop()
        await pipeline.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
