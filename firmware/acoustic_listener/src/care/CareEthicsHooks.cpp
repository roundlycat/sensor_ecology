#include "CareEthicsHooks.h"
#include <ArduinoJson.h>

// ── Constructor ───────────────────────────────────────────────────────────────

CareEthicsHooks::CareEthicsHooks(BaselineAndConfidenceModule& baseline,
                                  MotifModule& motif,
                                  IPublisher& publisher)
    : _baseline(baseline), _motif(motif), _publisher(publisher) {}

// ── Inbound care signals ──────────────────────────────────────────────────────

void CareEthicsHooks::care_setLearningRate(float alpha) {
    // BaselineAndConfidenceModule clamps alpha to [ALPHA_MIN, ALPHA_MAX].
    _baseline.setLearningRate(alpha);
    // Raise learning rate → move toward LEARNING state
    if (alpha > ALPHA_DEFAULT * 5.0f) {
        _transitionTo(CareState::LEARNING);
    }
}

void CareEthicsHooks::care_trustBaselines(bool trust) {
    // trust=true  → freeze EMA ("lean on memory / stay comfortable")
    // trust=false → unfreeze ("stay alert / track environment")
    _baseline.setBaselinesFrozen(trust);
}

void CareEthicsHooks::care_requestCalibration() {
    // "Start fresh locally" — clear motif candidates and reset the transient
    // threshold, but preserve NVS (the learned baselines survive a reboot).
    // Temporarily sets CareState::CALIBRATING.
    //
    // Note: this does not touch MemoryBlockModule directly. The cleared state
    // will propagate to NVS naturally on the next scheduled write.
    _baseline.seedFromMemory(
        0.0f, 0.0f, 0.0f, 0.5f, TRANSIENT_THRESHOLD_INIT, ALPHA_DEFAULT
    );
    // Re-seed baseline from defaults — it will re-learn from the current environment.
    _calibratingWindowsLeft = CALIBRATION_HOLD_WINDOWS;
    _transitionTo(CareState::CALIBRATING);
}

void CareEthicsHooks::care_setMotifSensitivity(float threshold) {
    _motif.setSensitivityThreshold(threshold);
}

// ── Outbound care signals ─────────────────────────────────────────────────────

void CareEthicsHooks::care_emitStabilitySignal() {
    // Publish current care state to the transport layer.
    StaticJsonDocument<160> doc;
    doc["type"]         = "care";
    doc["care_state"]   = careStateStr(_state);
    doc["uptime_ms"]    = millis();
    char buf[160];
    serializeJson(doc, buf, sizeof(buf));
    _publisher.publish(TOPIC_CARE, buf);
}

void CareEthicsHooks::care_emitMotifSignal(const MotifCandidate& m) {
    StaticJsonDocument<200> doc;
    doc["type"]            = "care";
    doc["event"]           = "motif_noticed";
    doc["stability_count"] = m.stability_count;
    doc["rms"]             = m.rms;
    doc["uptime_ms"]       = millis();
    char buf[200];
    serializeJson(doc, buf, sizeof(buf));
    _publisher.publish(TOPIC_CARE, buf);
}

// ── State update ──────────────────────────────────────────────────────────────

void CareEthicsHooks::updateCareState(const BaselineSnapshot& b,
                                       MotifGrowthState motifState) {
    // Count down calibration hold.
    if (_calibratingWindowsLeft > 0) {
        _calibratingWindowsLeft--;
        if (_calibratingWindowsLeft == 0) {
            // Calibration complete — drop back to UNSETTLED and re-learn.
            _transitionTo(CareState::UNSETTLED);
        }
        return;  // don't update state during calibration
    }

    // Derive CareState from composite_stability thresholds.
    CareState next;
    if (b.composite_stability > 0.80f) {
        next = CareState::STABLE;
    } else if (b.composite_stability > 0.50f) {
        next = CareState::UNSETTLED;
    } else {
        next = CareState::LEARNING;
    }

    // Only emit a signal on state transitions (not every window).
    if (next != _state) {
        _transitionTo(next);
    }

    // Emit a motif_noticed care signal when the motif module is actively building.
    if (motifState == MotifGrowthState::STABILIZING) {
        // Find the most-active candidate and emit a signal for it.
        // Avoid publishing every window; the publisher's own rate-limit handles this.
        // (Future: only emit once on entry to STABILIZING, not continuously.)
    }
}

// ── Private ───────────────────────────────────────────────────────────────────

void CareEthicsHooks::_transitionTo(CareState next) {
    _prevState = _state;
    _state     = next;
    care_emitStabilitySignal();
}
