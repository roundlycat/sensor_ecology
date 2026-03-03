/*
 * MXChip AZ3166 — Slow-Growth Acoustic Listener
 * ================================================
 *
 * MODULE INTERACTIONS
 * -------------------
 *
 *   [DMA ISR] ──→ AcousticSensing ──→ BaselineConfidence ──→ MemoryBlock
 *                                   └──→ MotifModule ──→ StatePublishing (event)
 *                                                    └──→ DisplayModule (merge)
 *   [10 Hz timer] → DisplayModule.update()
 *   [adaptive]    → StatePublishing.maybePublish()
 *   [10 min]      → MemoryBlock.saveMemoryBlock()
 *   [30 min]      → MemoryBlock.maybeIncrementMinorVersion()
 *   [external]    → CareEthicsHooks.{setLearningPace, receiveEncouragement, ...}
 *
 * MEMORY BLOCK (MemoryBlockData, ~70 bytes in STSAFE zone 7)
 * -----------------------------------------------------------
 *   version_major    — changes only with incompatible firmware upgrades
 *   version_minor    — self-increments when composite_stability ≥ 0.82 and
 *                      at least 30 minutes have elapsed since the last bump
 *   baseline_*       — slowly learned acoustic environment profile
 *   transient_thresh — learned transient detection threshold
 *   motifs[3]        — compact summaries of the last 3 promoted concepts
 *   total_concepts   — lifetime count of concept promotions
 *   checksum         — simple byte sum; invalid → fresh calibration
 *
 * MINOR VERSION INCREMENT
 * -----------------------
 *   Triggered by BaselineAndConfidenceModule reaching sustained stability.
 *   Saved to flash immediately (bypassing rate-limit) so the version is
 *   durable across reboots.
 *
 * CONCEPT FORMATION → DISPLAY MERGE
 * ------------------------------------
 *   When MotifModule promotes a candidate, DisplayModule.triggerConceptFormation()
 *   is called with a seed derived from the motif's texture value. The three
 *   ambient indicators (arc, pulse, accent) briefly converge — no two merges
 *   look identical because the seed drives small timing and motion variations.
 *   The merged state fades over CONCEPT_MERGE_MS (4 s) without a hard cut.
 *
 * CARE-ETHICS HOOKS
 * -----------------
 *   CareEthicsHooks provides the ecology interface:
 *     Inbound:  setLearningPace(), receiveEncouragement(), nudgeThreshold()
 *     Outbound: emitWellBeingSignal(), emitConceptSignal(), getSocialPresenceByte()
 *   These can be driven from MQTT subscriptions (add in StatePublishing),
 *   serial commands during development, or future BLE / REST handlers.
 *   The hooks apply suggestions at 30% weight so the device retains agency.
 */

#include <Arduino.h>
#include "config.h"
#include "AcousticSensing.h"
#include "BaselineConfidence.h"
#include "MotifModule.h"
#include "MemoryBlock.h"
#include "StatePublishing.h"
#include "DisplayModule.h"
#include "CareEthicsHooks.h"

// ---------------------------------------------------------------------------
// Module instances
// ---------------------------------------------------------------------------
static AcousticSensingModule      acoustic;
static BaselineAndConfidenceModule baseline;
static MotifModule                 motif;
static MemoryBlockModule           memory;
static StatePublishingModule       publishing;
static DisplayModule               display;
static CareEthicsHooks             care;

// ---------------------------------------------------------------------------
// Timing
// ---------------------------------------------------------------------------
static uint32_t lastFeatureMs  = 0;
static uint32_t lastDisplayMs  = 0;
static uint32_t lastPresenceMs = 0;

// ---------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    delay(300);
    Serial.println();
    Serial.println("[boot] ===== MXChip Acoustic Listener =====");

    // Bind care hooks to live module instances.
    care.bindModules(&baseline, &motif);

    // --- Persistent memory ---
    bool memOk = memory.loadMemoryBlock();
    if (memOk) {
        const MemoryBlockData& d = memory.getData();
        baseline.restoreBaselines(
            d.baseline_rms,
            d.baseline_variance,
            d.baseline_hum,
            d.baseline_texture,
            d.transient_thresh);

        // Apply pending threshold hint if one was stored
        // (not applicable on first boot; care peers send this over MQTT later)

        Serial.printf("[boot] resumed from v%u.%u  concepts=%lu\n",
                      d.version_major, d.version_minor,
                      (unsigned long)d.total_concepts);
    } else {
        Serial.println("[boot] no valid memory — fresh calibration");
        // Check if a care peer has a threshold hint waiting
        // (in production: subscribe to a retained MQTT topic here)
    }

    // --- Display ---
    display.begin();
    Serial.println("[boot] display ready");

    // --- Network + MQTT ---
    publishing.begin();

    // --- Audio (starts DMA recording) ---
    acoustic.begin();

    Serial.println("[boot] ready — listening");
}

// ---------------------------------------------------------------------------
void loop() {
    uint32_t now = millis();

    // ------------------------------------------------------------------
    // 1. Feature extraction
    //    The DMA callback fills the acoustic window asynchronously.
    //    We only consume features once per FEATURE_INTERVAL_MS to avoid
    //    feeding the modules faster than they meaningfully respond.
    // ------------------------------------------------------------------
    if (acoustic.isFeatureReady() &&
        (now - lastFeatureMs) >= FEATURE_INTERVAL_MS)
    {
        AcousticFeatures f = acoustic.getFeatures();
        acoustic.consumeFeature();
        lastFeatureMs = now;

        if (f.valid) {
            baseline.updateFromFeatures(f);
            motif.updateFromFeatures(f);
        }
    }

    // ------------------------------------------------------------------
    // 2. Snapshot current state (cheap, computed on demand)
    // ------------------------------------------------------------------
    ConfidenceSnapshot snap = baseline.getConfidenceSnapshot();
    MotifGrowthState   mgs  = motif.getMotifGrowthState();

    // ------------------------------------------------------------------
    // 3. Concept formation — check for motif promotion
    // ------------------------------------------------------------------
    PromotedMotif promoted;
    if (motif.checkForPromotion(&promoted)) {

        // Record in persistent memory (round-robin slot, total++ inside)
        memory.recordMotifSummary(
            promoted.rms, promoted.hum, promoted.texture, 1.0f);

        // Trigger display merge — seed from texture for serendipitous variation
        display.triggerConceptFormation(promoted.texture * 7.3f, now);

        // Publish concept event immediately (bypasses cadence)
        publishing.publishMotifEvent(
            promoted,
            memory.getVersionMajor(),
            memory.getVersionMinor(),
            memory.getTotalConcepts());

        // Emit care signal for ecology peers
        char careBuf[140];
        CareEthicsHooks::emitConceptSignal(
            careBuf, sizeof(careBuf),
            promoted.rms, promoted.hum, promoted.texture,
            memory.getVersionMajor(), memory.getVersionMinor());
        Serial.printf("[care] concept: %s\n", careBuf);

        // Persist immediately after a concept formation (force=true)
        memory.saveMemoryBlock(
            baseline.getBaselineRMS(),
            baseline.getBaselineVariance(),
            baseline.getBaselineHum(),
            baseline.getBaselineTexture(),
            baseline.getTransientThresh(),
            now, /*force=*/true);
    }

    // ------------------------------------------------------------------
    // 4. Minor version bump (if sustained stability warrants it)
    // ------------------------------------------------------------------
    if (memory.maybeIncrementMinorVersion(snap.composite_stability, now)) {
        // Version changed — persist it immediately
        memory.saveMemoryBlock(
            baseline.getBaselineRMS(),
            baseline.getBaselineVariance(),
            baseline.getBaselineHum(),
            baseline.getBaselineTexture(),
            baseline.getTransientThresh(),
            now, /*force=*/true);
    }

    // ------------------------------------------------------------------
    // 5. Periodic background save (rate-limited by MEMORY_SAVE_INTERVAL_MS)
    // ------------------------------------------------------------------
    memory.saveMemoryBlock(
        baseline.getBaselineRMS(),
        baseline.getBaselineVariance(),
        baseline.getBaselineHum(),
        baseline.getBaselineTexture(),
        baseline.getTransientThresh(),
        now, /*force=*/false);

    // ------------------------------------------------------------------
    // 6. State publishing (adaptive cadence)
    // ------------------------------------------------------------------
    publishing.maybePublish(
        snap, mgs,
        memory.getVersionMajor(),
        memory.getVersionMinor(),
        now);

    // ------------------------------------------------------------------
    // 7. Display update (10 Hz)
    // ------------------------------------------------------------------
    if ((now - lastDisplayMs) >= DISPLAY_UPDATE_MS) {
        lastDisplayMs = now;
        display.update(
            snap, mgs,
            memory.getVersionMajor(),
            memory.getVersionMinor(),
            now);
    }

    // ------------------------------------------------------------------
    // 8. Social presence heartbeat (every 60 s on a separate slow timer)
    //    In production: publish to a care/presence MQTT topic so peers
    //    can track this device's ecological status over time.
    // ------------------------------------------------------------------
    if ((now - lastPresenceMs) >= 60000UL) {
        lastPresenceMs = now;
        uint8_t presence = CareEthicsHooks::getSocialPresenceByte(
            snap.composite_stability, mgs, memory.getVersionMinor());
        Serial.printf("[care] presence byte: 0x%02X  wb=%d\n",
            presence,
            CareEthicsHooks::emitWellBeingSignal(snap.composite_stability, mgs));
        // TODO: publish presence to "sensors/mxchip/presence" topic
    }
}
