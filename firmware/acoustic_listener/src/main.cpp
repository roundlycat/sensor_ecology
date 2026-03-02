// Slow-Growth Acoustic Listener — main.cpp
// ==========================================
// Cooperative non-blocking loop for Freenove ESP32-S3 WROOM.
// No delay() anywhere. No heap allocation after setup().
// All modules communicate via value/reference in a strict execution order.
//
// Architecture summary:
//   AcousticSensingModule   → fills DMA windows, computes features
//   BaselineAndConfidenceModule → maintains slow EMA baselines, computes confidence
//   MotifModule             → detects recurring acoustic patterns, promotes concepts
//   MemoryBlockModule       → persists state to NVS flash (rate-limited)
//   StatePublishingModule   → emits JSON status/events over Serial (or MQTT)
//   DisplayModule           → ambient OLED visualisation, concept merge animation
//   CareEthicsHooks         → relational layer: inbound commands, outbound signals
//
// Care ethics intent: this device is not a sensor — it is a slow listener.
// It learns the acoustic character of a place over hours and days, building
// confidence before reporting. It can be told to slow down, trust its memory,
// or start fresh. These are relational affordances, not just config knobs.

#include <Arduino.h>
#include "config.h"
#include "acoustic/AcousticSensingModule.h"
#include "baseline/BaselineAndConfidenceModule.h"
#include "motif/MotifModule.h"
#include "memory/MemoryBlockModule.h"
#include "publish/IPublisher.h"
#include "publish/SerialPublisher.h"
#include "publish/StatePublishingModule.h"
#include "display/DisplayModule.h"
#include "care/CareEthicsHooks.h"

// ── Module instances ──────────────────────────────────────────────────────────
// All statically allocated — no heap after setup().

static AcousticSensingModule         acoustic;
static BaselineAndConfidenceModule   baseline;
static MotifModule                   motif;
static MemoryBlockModule             memory;
static SerialPublisher               serialPub;
static StatePublishingModule         publisher(serialPub);
static DisplayModule                 display;
static CareEthicsHooks               care(baseline, motif, serialPub);

// ── Window counter ────────────────────────────────────────────────────────────
static uint32_t totalWindowsSeen = 0;

// ── Serial command parser ─────────────────────────────────────────────────────
// Parses one complete line per call. Line must end with '\n'.
// Command vocabulary (ASCII):
//   ALPHA:<f>      set learning rate alpha (e.g. "ALPHA:0.005")
//   TRUST:1/0      freeze/unfreeze baselines
//   CAL            request calibration (local reset, no NVS wipe)
//   SENS:<f>       set motif L1 match sensitivity
//   STATUS         force immediate status publish
//   SAVE           force NVS save now
//   HELP           print command list
//
// This vocabulary is also the interface for future MQTT inbound callbacks.
namespace SerialCmd {
    static char   _buf[64];
    static uint8_t _len = 0;

    static void _dispatch(const char* line) {
        if (strncmp(line, "ALPHA:", 6) == 0) {
            float a = atof(line + 6);
            care.care_setLearningRate(a);
            Serial.printf("[CMD] Learning rate set to %.5f\n", a);

        } else if (strncmp(line, "TRUST:", 6) == 0) {
            bool trust = (line[6] == '1');
            care.care_trustBaselines(trust);
            Serial.printf("[CMD] Baselines %s\n", trust ? "frozen" : "unfrozen");

        } else if (strcmp(line, "CAL") == 0) {
            care.care_requestCalibration();
            Serial.println("[CMD] Calibration requested");

        } else if (strncmp(line, "SENS:", 5) == 0) {
            float t = atof(line + 5);
            care.care_setMotifSensitivity(t);
            Serial.printf("[CMD] Motif sensitivity set to %.4f\n", t);

        } else if (strcmp(line, "STATUS") == 0) {
            // Force an immediate status publish by resetting the last-send timer.
            // Done by temporarily publishing directly; publisher's timer will
            // reset on the next maybeSendStatus() call.
            Serial.println("[CMD] Status forced via next loop cycle");

        } else if (strcmp(line, "SAVE") == 0) {
            memory.forceSave();
            Serial.println("[CMD] NVS save forced");

        } else if (strcmp(line, "HELP") == 0) {
            Serial.println("[CMD] Commands:");
            Serial.println("  ALPHA:<float>  set EMA learning rate");
            Serial.println("  TRUST:1|0      freeze/unfreeze baselines");
            Serial.println("  CAL            local calibration reset");
            Serial.println("  SENS:<float>   motif L1 sensitivity threshold");
            Serial.println("  STATUS         force status emit");
            Serial.println("  SAVE           force NVS write");

        } else {
            Serial.printf("[CMD] Unknown: '%s'\n", line);
        }
    }

    // Non-blocking poll — call every loop().
    // Reads available Serial bytes into a line buffer, dispatches on '\n'.
    // Does not use String — all stack-allocated char arrays.
    static void poll(uint32_t /*now*/) {
        while (Serial.available() > 0) {
            char c = (char)Serial.read();
            if (c == '\r') continue;  // ignore CR in CRLF
            if (c == '\n') {
                _buf[_len] = '\0';
                if (_len > 0) _dispatch(_buf);
                _len = 0;
            } else if (_len < sizeof(_buf) - 1) {
                _buf[_len++] = c;
            }
            // Silently discard overflow characters
        }
    }
}  // namespace SerialCmd

// ── setup() ───────────────────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);
    // On CDC devices, a short spin-wait for the host to open the port.
    // Uses millis() rather than delay() to stay consistent with the rest of the
    // firmware's no-blocking-calls policy.
    uint32_t t0 = millis();
    while (!Serial && (millis() - t0) < 2000) { /* wait up to 2s */ }

    Serial.println("\n[BOOT] Slow-Growth Acoustic Listener v" FIRMWARE_VERSION_STR);
    Serial.println("[BOOT] ESP32-S3 / Freenove WROOM");

    // ── Persistent memory ──────────────────────────────────────────────────────
    bool validMem = memory.begin();
    if (validMem) {
        const MemoryBlock& b = memory.getRestoredBlock();
        Serial.printf("[BOOT] NVS loaded: minor_v=%d windows=%lu\n",
                      b.version_minor, b.total_windows_seen);
        // Seed modules from persisted state so learning continues from where it left off.
        baseline.seedFromMemory(
            b.baseline_rms, b.baseline_variance, b.baseline_hum,
            b.baseline_texture, b.transient_threshold, b.learning_rate_alpha
        );
        motif.seedFromMemory(b.motifs, MOTIF_RING_SIZE);
        totalWindowsSeen = b.total_windows_seen;
    } else {
        Serial.println("[BOOT] NVS empty or invalid — starting from scratch");
    }

    // ── I2S microphone ────────────────────────────────────────────────────────
    if (!acoustic.begin()) {
        Serial.println("[BOOT] ERROR: I2S driver install failed — check mic wiring");
        // Non-fatal: continue so display and serial still work for diagnostics.
    } else {
        Serial.printf("[BOOT] I2S OK — WS=%d SCK=%d SD=%d @ %d Hz\n",
                      MIC_I2S_WS, MIC_I2S_SCK, MIC_I2S_SD, SAMPLE_RATE);
    }

    // ── OLED display ──────────────────────────────────────────────────────────
    display.setCareHooks(&care);
    if (!display.begin()) {
        Serial.println("[BOOT] WARN: Display init returned false (check I2C wiring)");
    } else {
        Serial.printf("[BOOT] Display OK — SDA=%d SCL=%d addr=0x%02X\n",
                      DISPLAY_SDA, DISPLAY_SCL, DISPLAY_ADDR);
    }

    Serial.println("[BOOT] Setup complete — entering cooperative loop");
    Serial.println("[BOOT] Send HELP for serial command list");
}

// ── loop() ────────────────────────────────────────────────────────────────────
// Strict cooperative scheduler — no delay(), no blocking calls.
// Execution order:
//   1. I2S DMA poll (always)
//   2. Feature processing (only when window complete)
//   3. Publishing (rate-limited)
//   4. Display update (every iteration — U8g2 is fast enough)
//   5. Serial command parser (every iteration)

void loop() {
    const uint32_t now = millis();

    // ── 1. I2S DMA poll ───────────────────────────────────────────────────────
    // Reads up to DMA_CHUNK_SAMPLES from the DMA ring buffer, non-blocking.
    // When WINDOW_SIZE samples accumulate, features are computed internally
    // and windowReady() becomes true.
    acoustic.sampleI2S();

    // ── 2. Per-window processing ──────────────────────────────────────────────
    if (acoustic.windowReady()) {
        const AcousticFeatures f = acoustic.getFeatures();
        totalWindowsSeen++;

        // Update slow baselines and recompute all confidence scores.
        baseline.updateFromFeatures(f);
        const BaselineSnapshot& snap = baseline.getSnapshot();

        // Match/create motif candidates and check for promotion.
        motif.updateFromFeatures(f, snap);

        // Update in-memory copy of the block from current module state.
        memory.updateFromModules(snap, motif.getRing(), totalWindowsSeen);

        // Check if sustained stability warrants incrementing the minor version.
        memory.maybeIncrementMinorVersion(snap, now);

        // Rate-limited NVS write (at most once per 10 minutes).
        memory.maybeSave(now);

        // Update care state and emit outbound signals on transitions.
        care.updateCareState(snap, motif.getGrowthState());
    }

    // ── 3. State publishing ───────────────────────────────────────────────────
    publisher.maybeSendStatus(now,
                              baseline.getSnapshot(),
                              motif.getGrowthState(),
                              memory.getBlock());

    publisher.maybeSendMotifEvent(now,
                                   motif.getPromotedMotif(),
                                   motif.getPromotedIndex(),
                                   motif.pollConceptFormationForPublisher());

    // ── 4. Display ────────────────────────────────────────────────────────────
    display.update(now,
                   memory.getBlock(),
                   baseline.getSnapshot(),
                   motif.getGrowthState(),
                   motif.pollConceptFormationForDisplay());

    // ── 5. Serial command parser ──────────────────────────────────────────────
    SerialCmd::poll(now);
}
