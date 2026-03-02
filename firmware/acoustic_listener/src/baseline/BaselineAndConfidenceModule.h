#pragma once
#include <Arduino.h>
#include "../config.h"
#include "../acoustic/AcousticSensingModule.h"

// BaselineSnapshot — a point-in-time view of the current acoustic environment.
// Passed by const-ref to all downstream modules each window.
// Carries both the learned baselines AND the derived confidence values so
// no module needs to recompute them.
struct BaselineSnapshot {
    // Learned slow baselines (EMA-smoothed)
    float baseline_rms;
    float baseline_variance;
    float baseline_hum;
    float baseline_texture;       // spectral_flatness baseline
    float transient_threshold;    // adaptive peak/rms threshold

    // Per-dimension confidence [0,1]
    float baseline_confidence;    // how close current rms is to baseline
    float hum_confidence;         // how close current hum is to baseline hum
    float broadband_confidence;   // spectral_flatness itself (flat = confident)
    float transient_clarity;      // clamp(transient_ratio / threshold, 0, 1)

    // Composite
    float composite_stability;    // weighted sum of above four

    // Learning rate in effect (may have been modified by CareEthicsHooks)
    float learning_rate_alpha;
};

// BaselineAndConfidenceModule
// Responsibilities:
//   - Maintain slow exponential moving averages of acoustic features.
//   - Compute per-dimension and composite confidence / stability scores.
//   - Expose a BaselineSnapshot for all downstream modules.
//   - Accept learning rate adjustments from CareEthicsHooks.
//
// The EMA update rule is:
//   baseline = alpha * new_value + (1 - alpha) * baseline
// where alpha is clamped to [ALPHA_MIN, ALPHA_MAX].
// At the default ALPHA_DEFAULT=0.001, the time constant is ~1000 windows ≈ 33 s.
class BaselineAndConfidenceModule {
public:
    BaselineAndConfidenceModule();

    // Seed baselines from a persisted MemoryBlock on first boot after NVS load.
    // Call before the main loop starts if valid memory was restored.
    void seedFromMemory(float rms, float variance, float hum,
                        float texture, float transient_threshold, float alpha);

    // Update baselines and recompute confidence. Call once per window.
    void updateFromFeatures(const AcousticFeatures& f);

    // Return current snapshot (computed during last updateFromFeatures()).
    const BaselineSnapshot& getSnapshot() const { return _snapshot; }

    // CareEthicsHooks interface — adjust learning rate at runtime.
    // Alpha is clamped to [ALPHA_MIN, ALPHA_MAX] before applying.
    void setLearningRate(float alpha);

    // CareEthicsHooks interface — "lean on memory": freeze baselines temporarily.
    // When frozen, EMA updates are suspended; confidence still computed.
    void setBaselinesFrozen(bool frozen) { _frozen = frozen; }

private:
    BaselineSnapshot _snapshot;
    bool _frozen = false;
    bool _seeded = false;   // first window seeds rather than blends

    void _computeConfidence(const AcousticFeatures& f);
    static float _clampAlpha(float a);
};
