#pragma once
// config.h — all hardware pins, tuning knobs, and shared constants
// Adjust pin assignments here if wiring differs from the reference layout.
//
// ESP32-S3 note: GPIO 26-32 are reserved for internal SPI flash on the
// ESP32-S3-WROOM-1 module. The original spec's MIC_I2S_SD=32 is changed
// to GPIO 4 which is freely available on the Freenove ESP32-S3 board.
// I2C uses GPIO 8/9, the board's standard I2C bus.

// ── Hardware: INMP441 I2S MEMS microphone ────────────────────────────────────
#define MIC_I2S_WS          15      // Word Select (LRCLK)
#define MIC_I2S_SCK         14      // Bit Clock (BCLK)
#define MIC_I2S_SD           4      // Serial Data (DOUT from mic)
                                    // Original spec said 32, changed for ESP32-S3

// ── Hardware: SSD1306 128×64 OLED via I2C ────────────────────────────────────
#define DISPLAY_SDA          8      // Board-standard I2C SDA (Freenove ESP32-S3)
#define DISPLAY_SCL          9      // Board-standard I2C SCL
#define DISPLAY_ADDR      0x3C

// ── Audio sampling ────────────────────────────────────────────────────────────
#define SAMPLE_RATE         16000   // Hz
#define WINDOW_SIZE           512   // samples per analysis frame (~32 ms)
#define DMA_CHUNK_SAMPLES      64   // samples read per sampleI2S() call

// ── Firmware versioning ───────────────────────────────────────────────────────
#define FIRMWARE_MAJOR          1   // Major version — firmware-level only
#define FIRMWARE_VERSION_STR "1.0"

// ── Baseline EMA learning ─────────────────────────────────────────────────────
#define ALPHA_DEFAULT       0.001f  // ~1000-window time constant (~33 s)
#define ALPHA_MIN          0.0001f  // Never allow abrupt shifts
#define ALPHA_MAX           0.050f

// ── Motif ring ────────────────────────────────────────────────────────────────
#define MOTIF_RING_SIZE          4
#define MOTIF_PROMOTE_COUNT    300   // windows ≈ 10 s
#define MOTIF_STABILITY_THRESHOLD  0.60f
#define MOTIF_L1_MATCH_EPSILON     0.05f

// ── Persistent memory ─────────────────────────────────────────────────────────
#define MEMORY_WRITE_MIN_INTERVAL_MS  600000UL   // 10 min, millis()-gated
#define STABILITY_FOR_MINOR_VERSION    0.85f
#define WINDOWS_FOR_MINOR_BUMP         3000       // ~100 s

// ── State publishing cadence ──────────────────────────────────────────────────
#define STATUS_FAST_INTERVAL_MS    30000UL   // early-life / low minor version
#define STATUS_MID_INTERVAL_MS     60000UL   // default
#define STATUS_SLOW_INTERVAL_MS   300000UL   // stable and mature

// ── Display merge animation ───────────────────────────────────────────────────
#define MERGE_DURATION_MS    2000UL
#define MERGE_HOLD_MS        1500UL
#define MERGE_FADE_MS        1500UL

// ── Confidence weights (must sum to 1.0) ──────────────────────────────────────
#define W_BASELINE_CONF     0.40f
#define W_HUM_CONF          0.30f
#define W_BROADBAND_CONF    0.20f
#define W_TRANSIENT_CLARITY 0.10f

// ── Epsilon guard for division ────────────────────────────────────────────────
#define CONF_EPSILON        1e-6f

// ── Hum IIR lowpass cutoff ────────────────────────────────────────────────────
// Tracks energy in the 50-200 Hz band (HVAC, mains hum, low machinery).
#define HUM_LOWPASS_FC      200.0f  // Hz

// ── Sub-band spectral flatness ────────────────────────────────────────────────
// 8 biquad bandpass filters, centre frequencies in Hz.
// Log-spaced from 100 Hz to 7500 Hz; computed in AcousticSensingModule::begin().
#define SUBBANDS            8
static const float SUBBAND_FC[SUBBANDS] = {
    100.0f, 250.0f, 600.0f, 1200.0f, 2000.0f, 3500.0f, 5500.0f, 7500.0f
};
static const float SUBBAND_Q = 0.7f;  // Broad bandpass — 8 bands cover the spectrum

// ── Transient detection ───────────────────────────────────────────────────────
// Initial threshold; updated via BaselineAndConfidenceModule.
#define TRANSIENT_THRESHOLD_INIT   4.0f   // peak_abs / rms ratio

// ── NVS storage namespace ─────────────────────────────────────────────────────
#define NVS_NAMESPACE    "acousticv1"
#define NVS_KEY_BLOCK    "block"

// ── MQTT topics (used when MqttPublisher is wired up) ────────────────────────
#define TOPIC_STATUS     "acoustic/status"
#define TOPIC_MOTIF      "acoustic/motif"
#define TOPIC_CARE       "acoustic/care"
