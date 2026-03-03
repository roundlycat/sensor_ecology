#include "MotifModule.h"
#include "config.h"
#include <math.h>
#include <string.h>

MotifModule::MotifModule() : _promotionSlot(-1) {
    reset();
}

void MotifModule::reset() {
    memset(_slots, 0, sizeof(_slots));
    _promotionSlot = -1;
}

float MotifModule::distance(const AcousticFeatures& f,
                            const MotifCandidate&   m) const
{
    float dr = (f.rms             - m.rms)       / SCALE_RMS;
    float dv = (f.variance        - m.variance)  / SCALE_VAR;
    float dh = (f.hum_level       - m.hum)       / SCALE_HUM;
    float dt = (f.texture         - m.texture)   / SCALE_TEX;
    float dp = (f.transient_ratio - m.transient) / SCALE_TRANSIENT;
    return sqrtf(dr*dr + dv*dv + dh*dh + dt*dt + dp*dp);
}

int MotifModule::findWeakestSlot() const {
    int   weakest     = 0;
    float minStrength = _slots[0].active ? _slots[0].strength : -1.f;
    for (int i = 1; i < MOTIF_SLOTS; i++) {
        float s = _slots[i].active ? _slots[i].strength : -1.f;
        if (s < minStrength) { minStrength = s; weakest = i; }
    }
    return weakest;
}

void MotifModule::updateFromFeatures(const AcousticFeatures& f) {
    if (!f.valid) return;

    // --- Decay all active candidates ---
    for (int i = 0; i < MOTIF_SLOTS; i++) {
        if (!_slots[i].active) continue;
        _slots[i].strength *= MOTIF_DRIFT_DECAY;
        if (_slots[i].strength < 0.008f) {
            _slots[i].active = false;
            if (_promotionSlot == i) _promotionSlot = -1;
        }
    }

    // --- Find closest matching candidate ---
    int   bestSlot = -1;
    float bestDist = MOTIF_SIMILARITY_THRESH;
    for (int i = 0; i < MOTIF_SLOTS; i++) {
        if (!_slots[i].active) continue;
        float d = distance(f, _slots[i]);
        if (d < bestDist) { bestDist = d; bestSlot = i; }
    }

    if (bestSlot >= 0) {
        // Confirm and refine the matched candidate.
        MotifCandidate& m = _slots[bestSlot];

        // Online mean update (slowly pull centre toward confirmed observations).
        const float alpha = 0.08f;
        m.rms       += (f.rms             - m.rms)       * alpha;
        m.variance  += (f.variance        - m.variance)  * alpha;
        m.hum       += (f.hum_level       - m.hum)       * alpha;
        m.texture   += (f.texture         - m.texture)   * alpha;
        m.transient += (f.transient_ratio - m.transient) * alpha;

        // Boost strength; cap at 1.
        m.strength = fminf(1.0f, m.strength + 0.06f);
        if (m.stability_count < 255) m.stability_count++;

        if (m.stability_count >= MOTIF_STABILITY_TARGET) {
            _promotionSlot = bestSlot;
        }
    } else {
        // No matching candidate — plant a new one in an empty or weak slot.
        int newSlot = -1;
        for (int i = 0; i < MOTIF_SLOTS; i++) {
            if (!_slots[i].active) { newSlot = i; break; }
        }
        if (newSlot < 0) {
            // All slots occupied — evict weakest (but not one being promoted).
            newSlot = findWeakestSlot();
        }

        MotifCandidate& m = _slots[newSlot];
        m.rms             = f.rms;
        m.variance        = f.variance;
        m.hum             = f.hum_level;
        m.texture         = f.texture;
        m.transient       = f.transient_ratio;
        m.stability_count = 1;
        m.strength        = 0.08f;
        m.active          = true;

        // If this slot was previously awaiting promotion, reset that.
        if (_promotionSlot == newSlot) _promotionSlot = -1;
    }
}

bool MotifModule::checkForPromotion(PromotedMotif* out) {
    if (_promotionSlot < 0) return false;
    MotifCandidate& m = _slots[_promotionSlot];
    if (!m.active || m.stability_count < MOTIF_STABILITY_TARGET) {
        _promotionSlot = -1;
        return false;
    }

    if (out) {
        out->rms              = m.rms;
        out->variance         = m.variance;
        out->hum              = m.hum;
        out->texture          = m.texture;
        out->transient        = m.transient;
        out->final_stability  = m.stability_count;
    }

    // Clear the slot — the concept has been absorbed into higher-level memory.
    m = {};
    _promotionSlot = -1;
    return true;
}

MotifGrowthState MotifModule::getMotifGrowthState() const {
    if (_promotionSlot >= 0) return MGS_READY_FOR_PROMOTION;

    bool anyActive = false;
    for (int i = 0; i < MOTIF_SLOTS; i++) {
        if (!_slots[i].active) continue;
        anyActive = true;
        if (_slots[i].stability_count >= MOTIF_STABILITY_TARGET / 2) {
            return MGS_STABILIZING;
        }
    }
    return anyActive ? MGS_DRIFTING : MGS_IDLE;
}

const MotifCandidate* MotifModule::getBestCandidate() const {
    const MotifCandidate* best = nullptr;
    float bestStr = 0.f;
    for (int i = 0; i < MOTIF_SLOTS; i++) {
        if (_slots[i].active && _slots[i].strength > bestStr) {
            bestStr = _slots[i].strength;
            best    = &_slots[i];
        }
    }
    return best;
}
