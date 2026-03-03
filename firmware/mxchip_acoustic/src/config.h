#pragma once

// ============================================================
// Firmware identity
// ============================================================
// version_major: increment only when MemoryBlockData layout changes.
//   A mismatch triggers the migration path on boot.
// version_minor: self-increments at runtime as the device gains
//   acoustic experience. Persisted in the memory block.
#define FW_VERSION_MAJOR  1
#define FW_VERSION_MINOR  0   // runtime start value; overridden by memory load

// ============================================================
// Audio
// ============================================================
#define AUDIO_SAMPLE_RATE    8000
#define AUDIO_BITS           16
#define AUDIO_CHUNK_BYTES    512                        // DMA transfer size
#define AUDIO_CHUNK_SAMPLES  (AUDIO_CHUNK_BYTES / 2)   // 256 int16 samples

// Feature window: accumulate this many chunks before computing features.
// 16 chunks × 256 samples / 8000 Hz ≈ 512 ms of audio per feature vector.
#define FEATURE_WINDOW_CHUNKS   16
#define FEATURE_WINDOW_SAMPLES  (FEATURE_WINDOW_CHUNKS * AUDIO_CHUNK_SAMPLES)
#define FEATURE_INTERVAL_MS     500   // minimum ms between feature publications

// ============================================================
// Slow-growth learning rates (IIR alpha values, applied per feature update)
// Smaller → slower adaptation → more inertia → more "slow-growth" character.
// ============================================================
#define BASELINE_ALPHA   0.004f   // RMS and variance baseline
#define HUM_ALPHA        0.008f   // hum envelope baseline
#define TEXTURE_ALPHA    0.006f   // broadband texture baseline

// ============================================================
// Motif detection
// ============================================================
#define MOTIF_SLOTS              5
// Distance in normalised feature space below which a new observation
// is credited to an existing motif candidate rather than starting a new one.
#define MOTIF_SIMILARITY_THRESH  0.28f
// Number of confirmed hits required to promote a candidate to a concept.
#define MOTIF_STABILITY_TARGET   14
// Per-update decay applied to unconfirmed motif strength.
#define MOTIF_DRIFT_DECAY        0.975f

// ============================================================
// Memory / persistence
// ============================================================
// STSAFE zone 7 is 784 bytes; our block is ~70 bytes — plenty of headroom.
#define MEMORY_ZONE   7
// Minimum interval between flash writes (protect write endurance).
#define MEMORY_SAVE_INTERVAL_MS   (10UL * 60UL * 1000UL)   // 10 minutes
// Minimum interval between minor version bumps.
#define MINOR_BUMP_INTERVAL_MS    (30UL * 60UL * 1000UL)   // 30 minutes
// Composite stability required before a minor version bump is earned.
#define MINOR_VERSION_STABILITY_THRESH  0.82f

// ============================================================
// Network / MQTT
// ============================================================
// WiFi connection timeout in begin(); prevents hanging at boot.
#define WIFI_CONNECT_TIMEOUT_MS   15000UL
// Minimum interval between WiFi reconnect attempts in publishJson().
#define WIFI_RETRY_INTERVAL_MS    (5UL * 60UL * 1000UL)  // 5 minutes
#ifndef MQTT_BROKER_IP
#define MQTT_BROKER_IP    "192.168.0.25"
#endif
#ifndef MQTT_BROKER_PORT
#define MQTT_BROKER_PORT  1883
#endif
#define TOPIC_STATUS  "sensors/mxchip/status"
#define TOPIC_MOTIF   "sensors/mxchip/motif"

// ============================================================
// Publishing cadence
// ============================================================
// Fast cadence: used when the device is unsettled or freshly booted.
#define PUBLISH_FAST_MS   (30UL  * 1000UL)    // 30 seconds
// Slow cadence: used once composite stability is high.
#define PUBLISH_SLOW_MS   (5UL * 60UL * 1000UL) // 5 minutes
#define STABILITY_PUBLISH_THRESH  0.78f          // switch to slow above this

// ============================================================
// Display
// ============================================================
// MXChip AZ3166 has a 128×64 SSD1306-compatible OLED.
// The draw() API works in 8-pixel page rows; FB_PAGES = 8.
#define DISPLAY_FB_W      128
#define DISPLAY_FB_PAGES  8     // → 64 px tall
#define DISPLAY_FB_BYTES  (DISPLAY_FB_W * DISPLAY_FB_PAGES)  // 1024

#define DISPLAY_UPDATE_MS       100    // 10 Hz
// Duration of the concept-formation merge animation.
#define CONCEPT_MERGE_MS        4000
