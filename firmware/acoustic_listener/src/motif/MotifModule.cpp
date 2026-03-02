#include "MotifModule.h"
#include <string.h>
#include <math.h>

// ── Constructor ───────────────────────────────────────────────────────────────

MotifModule::MotifModule() {
    memset(_ring, 0, sizeof(_ring));
    _growthState   = MotifGrowthState::IDLE;
    _promotedMotif = nullptr;
    _promotedIdx   = -1;
}

// ── Public ────────────────────────────────────────────────────────────────────

void MotifModule::seedFromMemory(const MotifCandidate* motifs, uint8_t count) {
    const uint8_t n = (count < MOTIF_RING_SIZE) ? count : MOTIF_RING_SIZE;
    for (uint8_t i = 0; i < n; i++) {
        _ring[i] = motifs[i];
    }
    _updateGrowthState();
}

void MotifModule::updateFromFeatures(const AcousticFeatures& f,
                                     const BaselineSnapshot& b) {
    // Clear the transient PROMOTED state from the previous window.
    // The concept formation flags persist until polled.
    if (_growthState == MotifGrowthState::PROMOTED) {
        _promotedMotif = nullptr;
        _promotedIdx   = -1;
    }

    // Only consider forming motifs when the environment is stable enough.
    // Below MOTIF_STABILITY_THRESHOLD the signal is too noisy to fingerprint.
    if (b.composite_stability < MOTIF_STABILITY_THRESHOLD) {
        // Decay all candidates' age slightly — they grow older even when we
        // can't observe them, preventing stale patterns from persisting forever.
        for (int i = 0; i < MOTIF_RING_SIZE; i++) {
            if (_ring[i].active && _ring[i].age_windows < 255) {
                _ring[i].age_windows++;
            }
        }
        _updateGrowthState();
        return;
    }

    // ── Match incoming features against existing candidates ───────────────────
    int matchIdx = _findMatch(f);

    if (matchIdx >= 0) {
        // An existing candidate matches this window — reinforce it.
        MotifCandidate& c = _ring[matchIdx];
        if (c.stability_count < 65535) c.stability_count++;
        if (c.age_windows < 255)       c.age_windows++;

        // Update the candidate's feature vector toward the current observation
        // using a faster-moving EMA (alpha=0.01, ~100-window time constant).
        // This allows motifs to slowly track gradual environmental drift.
        static constexpr float MOTIF_REFINE_ALPHA = 0.01f;
        c.rms             = MOTIF_REFINE_ALPHA * f.rms             + (1.0f - MOTIF_REFINE_ALPHA) * c.rms;
        c.variance        = MOTIF_REFINE_ALPHA * f.variance        + (1.0f - MOTIF_REFINE_ALPHA) * c.variance;
        c.hum_energy      = MOTIF_REFINE_ALPHA * f.hum_energy      + (1.0f - MOTIF_REFINE_ALPHA) * c.hum_energy;
        c.transient_ratio = MOTIF_REFINE_ALPHA * f.transient_ratio + (1.0f - MOTIF_REFINE_ALPHA) * c.transient_ratio;

        // Check for promotion
        if (c.stability_count >= MOTIF_PROMOTE_COUNT) {
            _promotedMotif            = &_ring[matchIdx];
            _promotedIdx              = matchIdx;
            _growthState              = MotifGrowthState::PROMOTED;
            _conceptForDisplay        = true;
            _conceptForPublisher      = true;
            // Reset stability_count after promotion so the same motif can be
            // promoted again if it sustains long enough (re-promotion signals
            // continued environmental regularity).
            c.stability_count = 0;
            return;
        }
    } else {
        // No match found — create a new candidate.
        // Find a free slot first; if none, evict the weakest candidate.
        int slot = _findFreeSlot();
        if (slot < 0) slot = _findEvictSlot();
        if (slot >= 0) {
            _addCandidate(f, slot);
        }
    }

    _updateGrowthState();
}

// ── Private ───────────────────────────────────────────────────────────────────

float MotifModule::_l1Distance(const MotifCandidate& c,
                                const AcousticFeatures& f) const {
    // L1 (Manhattan) distance on four normalised dimensions.
    // No multiplication for comparison — just absolute differences.
    return fabsf(c.rms             - f.rms)             +
           fabsf(c.variance        - f.variance)        +
           fabsf(c.hum_energy      - f.hum_energy)      +
           fabsf(c.transient_ratio - f.transient_ratio);
}

int MotifModule::_findMatch(const AcousticFeatures& f) const {
    int   bestIdx  = -1;
    float bestDist = _sensitivityThreshold;  // only match if within threshold

    for (int i = 0; i < MOTIF_RING_SIZE; i++) {
        if (!_ring[i].active) continue;
        const float d = _l1Distance(_ring[i], f);
        if (d < bestDist) {
            bestDist = d;
            bestIdx  = i;
        }
    }
    return bestIdx;
}

int MotifModule::_findFreeSlot() const {
    for (int i = 0; i < MOTIF_RING_SIZE; i++) {
        if (!_ring[i].active) return i;
    }
    return -1;
}

int MotifModule::_findEvictSlot() const {
    // Evict the candidate with the lowest stability_count (least established).
    // If two candidates are equally weak, the one seen first (lowest index) wins.
    int   minIdx   = 0;
    uint16_t minCount = _ring[0].stability_count;
    for (int i = 1; i < MOTIF_RING_SIZE; i++) {
        if (_ring[i].stability_count < minCount) {
            minCount = _ring[i].stability_count;
            minIdx   = i;
        }
    }
    return minIdx;
}

void MotifModule::_addCandidate(const AcousticFeatures& f, int slot) {
    MotifCandidate& c  = _ring[slot];
    c.rms             = f.rms;
    c.variance        = f.variance;
    c.hum_energy      = f.hum_energy;
    c.transient_ratio = f.transient_ratio;
    c.stability_count = 1;
    c.age_windows     = 0;
    c.active          = true;
}

void MotifModule::_updateGrowthState() {
    // Derive overall state from the most-mature active candidate.
    uint16_t maxCount = 0;
    bool     hasActive = false;

    for (int i = 0; i < MOTIF_RING_SIZE; i++) {
        if (_ring[i].active) {
            hasActive = true;
            if (_ring[i].stability_count > maxCount) {
                maxCount = _ring[i].stability_count;
            }
        }
    }

    if (!hasActive) {
        _growthState = MotifGrowthState::IDLE;
    } else if (maxCount >= MOTIF_PROMOTE_COUNT / 2) {
        _growthState = MotifGrowthState::STABILIZING;
    } else {
        _growthState = MotifGrowthState::ACCUMULATING;
    }
    // Note: PROMOTED is set directly in updateFromFeatures() — not here.
}

bool MotifModule::_pollEvent(bool& flag) {
    if (!flag) return false;
    flag = false;
    return true;
}
