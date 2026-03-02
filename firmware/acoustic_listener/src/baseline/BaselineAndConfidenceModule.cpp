#include "BaselineAndConfidenceModule.h"
#include <math.h>

// ── Helpers ───────────────────────────────────────────────────────────────────

static inline float clamp01(float v) {
    if (v < 0.0f) return 0.0f;
    if (v > 1.0f) return 1.0f;
    return v;
}

// ── Constructor ───────────────────────────────────────────────────────────────

BaselineAndConfidenceModule::BaselineAndConfidenceModule() {
    // Zero-initialise snapshot; defaults will be overwritten on first window
    // or by seedFromMemory().
    _snapshot = {};
    _snapshot.learning_rate_alpha  = ALPHA_DEFAULT;
    _snapshot.transient_threshold  = TRANSIENT_THRESHOLD_INIT;
}

// ── Public ────────────────────────────────────────────────────────────────────

void BaselineAndConfidenceModule::seedFromMemory(
        float rms, float variance, float hum,
        float texture, float transient_threshold, float alpha) {
    _snapshot.baseline_rms         = rms;
    _snapshot.baseline_variance    = variance;
    _snapshot.baseline_hum         = hum;
    _snapshot.baseline_texture     = texture;
    _snapshot.transient_threshold  = transient_threshold;
    _snapshot.learning_rate_alpha  = _clampAlpha(alpha);
    _seeded = true;
}

void BaselineAndConfidenceModule::updateFromFeatures(const AcousticFeatures& f) {
    const float alpha = _snapshot.learning_rate_alpha;

    if (!_seeded) {
        // First window: seed baselines directly from observed features.
        // Avoids the EMA needing thousands of windows to converge from zero.
        _snapshot.baseline_rms        = f.rms;
        _snapshot.baseline_variance   = f.variance;
        _snapshot.baseline_hum        = f.hum_energy;
        _snapshot.baseline_texture    = f.spectral_flatness;
        _snapshot.transient_threshold = TRANSIENT_THRESHOLD_INIT;
        _seeded = true;
    } else if (!_frozen) {
        // Slow EMA update — the core "slow-growth" mechanism.
        // alpha = 0.001 → ~1000-window time constant (~33 seconds at 32ms/window).
        // CareEthicsHooks can raise alpha to speed up re-learning or lower it
        // to become more conservative.
        _snapshot.baseline_rms =
            alpha * f.rms + (1.0f - alpha) * _snapshot.baseline_rms;

        _snapshot.baseline_variance =
            alpha * f.variance + (1.0f - alpha) * _snapshot.baseline_variance;

        _snapshot.baseline_hum =
            alpha * f.hum_energy + (1.0f - alpha) * _snapshot.baseline_hum;

        _snapshot.baseline_texture =
            alpha * f.spectral_flatness + (1.0f - alpha) * _snapshot.baseline_texture;

        // Transient threshold tracks the typical transient_ratio seen over time.
        // This prevents a naturally impulsive environment from being flagged
        // continuously as high-transient.
        _snapshot.transient_threshold =
            alpha * f.transient_ratio + (1.0f - alpha) * _snapshot.transient_threshold;
        if (_snapshot.transient_threshold < 1.0f) _snapshot.transient_threshold = 1.0f;
    }

    _computeConfidence(f);
}

void BaselineAndConfidenceModule::setLearningRate(float alpha) {
    _snapshot.learning_rate_alpha = _clampAlpha(alpha);
}

// ── Private ───────────────────────────────────────────────────────────────────

void BaselineAndConfidenceModule::_computeConfidence(const AcousticFeatures& f) {
    // ── baseline_confidence: how close current RMS is to the learned baseline ──
    // = 1 - clamp( |rms - baseline_rms| / baseline_rms, 0, 1 )
    // An RMS that perfectly matches the baseline gives confidence 1.0.
    // An RMS that has doubled or halved gives confidence 0.0.
    const float rmsBase = _snapshot.baseline_rms + CONF_EPSILON;
    _snapshot.baseline_confidence = clamp01(
        1.0f - fabsf(f.rms - _snapshot.baseline_rms) / rmsBase
    );

    // ── hum_confidence: how close current hum is to baseline hum ──────────────
    const float humBase = _snapshot.baseline_hum + CONF_EPSILON;
    _snapshot.hum_confidence = clamp01(
        1.0f - fabsf(f.hum_energy - _snapshot.baseline_hum) / humBase
    );

    // ── broadband_confidence: spectral flatness IS the confidence here ─────────
    // A flat spectrum (flatness → 1.0) means the acoustic environment is diffuse
    // and consistent. A tonal/structured spectrum (flatness → 0.0) signals change.
    _snapshot.broadband_confidence = f.spectral_flatness;

    // ── transient_clarity: clamp( transient_ratio / threshold, 0, 1 ) ──────────
    // High transient_clarity means the acoustic energy is impulsive relative
    // to the baseline; it informs whether the environment is event-driven.
    _snapshot.transient_clarity = clamp01(
        f.transient_ratio / (_snapshot.transient_threshold + CONF_EPSILON)
    );

    // ── composite_stability: weighted sum of four dimensions ──────────────────
    // Weights are defined in config.h so they can be tuned without touching logic.
    _snapshot.composite_stability =
        W_BASELINE_CONF     * _snapshot.baseline_confidence  +
        W_HUM_CONF          * _snapshot.hum_confidence        +
        W_BROADBAND_CONF    * _snapshot.broadband_confidence  +
        W_TRANSIENT_CLARITY * _snapshot.transient_clarity;

    // Clamp to [0,1] for safety against floating-point drift
    _snapshot.composite_stability = clamp01(_snapshot.composite_stability);
}

float BaselineAndConfidenceModule::_clampAlpha(float a) {
    if (a < ALPHA_MIN) return ALPHA_MIN;
    if (a > ALPHA_MAX) return ALPHA_MAX;
    return a;
}
