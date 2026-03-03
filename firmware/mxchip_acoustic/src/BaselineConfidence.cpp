#include "BaselineConfidence.h"
#include "config.h"
#include <math.h>

BaselineAndConfidenceModule::BaselineAndConfidenceModule()
    : _baseRMS(800.f),      // Reasonable initial guess for a quiet room
      _baseVar(640000.f),   // ~800²
      _baseHum(200.f),
      _baseTex(0.35f),
      _transThresh(3.5f),
      _ewmDevRMS(1.f),      // Start at maximum uncertainty
      _ewmDevHum(1.f),
      _ewmDevTex(1.f),
      _lrMult(1.0f),
      _updateCount(0)
{}

void BaselineAndConfidenceModule::restoreBaselines(
    float rms, float var, float hum, float tex, float transThresh)
{
    _baseRMS     = rms;
    _baseVar     = var;
    _baseHum     = hum;
    _baseTex     = tex;
    _transThresh = transThresh;
    // Restored state: we have some prior knowledge, so reduce uncertainty.
    _ewmDevRMS = 0.15f;
    _ewmDevHum = 0.15f;
    _ewmDevTex = 0.15f;
    // Pretend we've already seen some data so early-life discounting is muted.
    _updateCount = 120;
}

void BaselineAndConfidenceModule::setLearningRateMultiplier(float ratio) {
    if (ratio < 0.2f) ratio = 0.2f;
    if (ratio > 6.0f) ratio = 6.0f;
    _lrMult = ratio;
}

void BaselineAndConfidenceModule::updateFromFeatures(const AcousticFeatures& f) {
    if (!f.valid) return;
    _updateCount++;

    // Effective alpha values, scaled by the care-ethics multiplier.
    // Dividing by _lrMult slows adaptation when multiplier > 1.
    float aBase = BASELINE_ALPHA / _lrMult;
    float aHum  = HUM_ALPHA      / _lrMult;
    float aTex  = TEXTURE_ALPHA  / _lrMult;

    // --- Slow baseline updates (IIR low-pass) ---
    _baseRMS += (f.rms       - _baseRMS) * aBase;
    _baseVar += (f.variance  - _baseVar) * aBase;
    _baseHum += (f.hum_level - _baseHum) * aHum;
    _baseTex += (f.texture   - _baseTex) * aTex;

    // Transient threshold tracks peak ratio with asymmetric update:
    // adapt up quickly (catch real transients), decay down slowly.
    if (f.transient_ratio > _transThresh) {
        _transThresh += (f.transient_ratio - _transThresh) * aBase * 3.0f;
    } else {
        _transThresh += (f.transient_ratio - _transThresh) * aBase * 0.2f;
    }

    // --- Short-term EWM deviation trackers ---
    // Relative deviation² = ((observed - baseline) / baseline)²
    // Alpha for the deviation tracker is faster than baseline (to track
    // current stability over a shorter horizon, ~50 updates ≈ 25 seconds).
    const float aDev = 0.02f;

    float relDevRMS = (_baseRMS > 1.f)
        ? ((f.rms - _baseRMS) / _baseRMS) : 0.f;
    _ewmDevRMS += (relDevRMS * relDevRMS - _ewmDevRMS) * aDev;

    float relDevHum = (_baseHum > 1.f)
        ? ((f.hum_level - _baseHum) / _baseHum) : 0.f;
    _ewmDevHum += (relDevHum * relDevHum - _ewmDevHum) * aDev;

    float relDevTex = (_baseTex > 0.001f)
        ? ((f.texture - _baseTex) / _baseTex) : 0.f;
    _ewmDevTex += (relDevTex * relDevTex - _ewmDevTex) * aDev;
}

// Map relative deviation² to confidence in [0,1].
// dev² = 0     → confidence = 1.0  (perfectly settled)
// dev² = 0.04  → confidence = 0.5  (20% relative deviation = half-confident)
// dev² → ∞    → confidence → 0
float BaselineAndConfidenceModule::devToConfidence(float relDev2) {
    // confidence = 1 / (1 + k * relDev²),  k chosen so 0.04 → 0.5
    const float k = 25.0f;   // 1/(1 + 25*0.04) = 1/2 ✓
    return 1.0f / (1.0f + k * relDev2);
}

ConfidenceSnapshot BaselineAndConfidenceModule::getConfidenceSnapshot() const {
    ConfidenceSnapshot snap;

    // Early-life discount: before we've seen 60 updates (~30 s) the device
    // has not had time to form reliable baselines, so cap confidence.
    float earlyScale = (_updateCount < 60)
        ? (float)_updateCount / 60.f
        : 1.0f;

    snap.baseline_confidence  = devToConfidence(_ewmDevRMS) * earlyScale;
    snap.hum_confidence       = devToConfidence(_ewmDevHum) * earlyScale;
    snap.broadband_confidence = devToConfidence(_ewmDevTex) * earlyScale;

    // Transient clarity: how much headroom the learned threshold has above
    // a pure tone (transient_ratio ≈ 1.41). A threshold of 1.5 = no clarity;
    // threshold of 10+ = clear transient definition.
    float clarity = (_transThresh > 1.5f)
        ? (1.0f - expf(-(_transThresh - 1.5f) / 4.0f))
        : 0.0f;
    snap.transient_clarity = clarity * earlyScale;

    // Composite: weighted blend.
    snap.composite_stability =
          0.35f * snap.baseline_confidence
        + 0.25f * snap.hum_confidence
        + 0.25f * snap.broadband_confidence
        + 0.15f * snap.transient_clarity;

    return snap;
}
