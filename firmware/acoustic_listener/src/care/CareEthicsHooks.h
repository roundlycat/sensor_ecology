#pragma once
#include <Arduino.h>
#include "../config.h"
#include "../baseline/BaselineAndConfidenceModule.h"
#include "../motif/MotifModule.h"
#include "../publish/IPublisher.h"

// CareState — the device's self-assessed relational stance.
// Used by DisplayModule to set the drift speed of the motif accent indicator.
// The DisplayModule reads this rather than raw composite_stability to keep
// the concern in the right module.
enum class CareState {
    STABLE,       // composite_stability consistently high — settled, confident
    UNSETTLED,    // moderate stability — still learning, environment changing
    LEARNING,     // low stability, alpha recently raised — actively adapting
    CALIBRATING   // care_requestCalibration() in progress — temporary reset
};

inline const char* careStateStr(CareState s) {
    switch (s) {
        case CareState::STABLE:      return "STABLE";
        case CareState::UNSETTLED:   return "UNSETTLED";
        case CareState::LEARNING:    return "LEARNING";
        case CareState::CALIBRATING: return "CALIBRATING";
    }
    return "UNSETTLED";
}

// CareEthicsHooks
// Responsibilities:
//   - Translate inbound care signals (from Serial commands or future MQTT) into
//     mutations on the baseline and motif modules.
//   - Derive and expose CareState based on composite stability.
//   - Emit outbound care signals via IPublisher.
//
// All inbound care_ methods are designed to be callable from:
//   (a) the serial command parser in main.cpp
//   (b) a future MQTT callback
// They are deliberately thin: they modify module state, not internal state here.
class CareEthicsHooks {
public:
    // Both references must outlive this object.
    CareEthicsHooks(BaselineAndConfidenceModule& baseline,
                    MotifModule& motif,
                    IPublisher& publisher);

    // ── Inbound care signals ──────────────────────────────────────────────────

    // Adjust the EMA learning rate. "slow down / speed up" from a supervising agent.
    // Alpha is clamped internally by BaselineAndConfidenceModule.
    void care_setLearningRate(float alpha);

    // "lean on memory" — freeze baselines so they don't drift from known good values.
    // "stay alert"     — unfreeze so baselines track the current environment.
    void care_trustBaselines(bool trust);

    // "start fresh locally" — clear all motif candidates, reset transient threshold,
    // but do NOT wipe NVS (memory persists across reboots).
    // Sets CareState::CALIBRATING for CALIBRATION_HOLD_WINDOWS windows.
    void care_requestCalibration();

    // Adjust how strictly incoming features must match existing candidates.
    // Lower threshold = more permissive (more motifs found).
    // Higher threshold = more conservative (only very stable patterns promoted).
    void care_setMotifSensitivity(float threshold);

    // ── Outbound care signals ─────────────────────────────────────────────────

    // Emit current stability stance to the publisher.
    // Called by updateCareState() automatically when state transitions.
    void care_emitStabilitySignal();

    // Emit a motif_noticed signal when a candidate is actively accumulating.
    void care_emitMotifSignal(const MotifCandidate& m);

    // ── State update ──────────────────────────────────────────────────────────

    // Call once per window (when windowReady). Derives CareState from the current
    // snapshot and motif state. Emits outbound signals on state transitions.
    void updateCareState(const BaselineSnapshot& b, MotifGrowthState motifState);

    CareState getCurrentState() const { return _state; }

private:
    BaselineAndConfidenceModule& _baseline;
    MotifModule&                 _motif;
    IPublisher&                  _publisher;

    CareState _state     = CareState::UNSETTLED;
    CareState _prevState = CareState::UNSETTLED;

    uint32_t _calibratingWindowsLeft = 0;
    static const uint32_t CALIBRATION_HOLD_WINDOWS = 100;  // ~3 s

    void _transitionTo(CareState next);
};
