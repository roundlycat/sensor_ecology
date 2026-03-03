#pragma once
#include <Arduino.h>
#include "AcousticSensing.h"

// ---------------------------------------------------------------------------
// ConfidenceSnapshot
// All values in [0, 1]; higher = more confident / more stable.
// ---------------------------------------------------------------------------
struct ConfidenceSnapshot {
    float baseline_confidence;    // How settled the RMS noise floor is
    float hum_confidence;         // How consistent the hum envelope is
    float broadband_confidence;   // How stable the broadband texture is
    float transient_clarity;      // How well-defined transient events are
    float composite_stability;    // Weighted blend of all four dimensions
};

// ---------------------------------------------------------------------------
// BaselineAndConfidenceModule
//
// Maintains slow-moving baselines for each acoustic feature dimension and
// computes confidence scores by measuring short-term deviation relative to
// those baselines. Baselines adapt at a rate controlled by the alpha values
// in config.h (slow-growth character), optionally scaled by a care-ethics
// learning-rate multiplier from external agents.
// ---------------------------------------------------------------------------
class BaselineAndConfidenceModule {
public:
    BaselineAndConfidenceModule();

    // Called each time a fresh AcousticFeatures vector is available.
    // Updates baselines (slow IIR) and short-term variance trackers.
    void updateFromFeatures(const AcousticFeatures& f);

    // Returns current confidence snapshot (computed on demand, lightweight).
    ConfidenceSnapshot getConfidenceSnapshot() const;

    // --- Accessors for memory save/restore ---
    float getBaselineRMS()      const { return _baseRMS; }
    float getBaselineVariance() const { return _baseVar; }
    float getBaselineHum()      const { return _baseHum; }
    float getBaselineTexture()  const { return _baseTex; }
    float getTransientThresh()  const { return _transThresh; }

    // Restore baselines from persistent memory on boot.
    void restoreBaselines(float rms, float var, float hum,
                          float tex, float transThresh);

    // Care-ethics hook: externally adjust the learning rate.
    //   ratio > 1.0 → slower adaptation (more inertia; "you seem stressed")
    //   ratio = 1.0 → normal pace
    //   ratio < 1.0 → faster adaptation (more responsive)
    // Clamped to [0.2, 6.0] so the device retains its own agency.
    void setLearningRateMultiplier(float ratio);
    float getLearningRateMultiplier() const { return _lrMult; }

private:
    // Slow baselines (IIR)
    float _baseRMS;
    float _baseVar;
    float _baseHum;
    float _baseTex;
    float _transThresh;

    // Short-term EWM variance trackers (for stability measurement).
    // Each tracks: EWM of (feature - baseline)² / baseline²  (relative deviation²)
    float _ewmDevRMS;
    float _ewmDevHum;
    float _ewmDevTex;

    // External learning-rate scale factor (care-ethics hook).
    float _lrMult;

    // Count of feature updates received (used to weight early-life uncertainty).
    uint32_t _updateCount;

    // Helper: map a relative deviation² to a confidence in [0,1].
    static float devToConfidence(float relDev2);
};
