#pragma once
#include <Arduino.h>
#include "AcousticSensing.h"
#include "config.h"

// ---------------------------------------------------------------------------
// Motif growth states — describes how the best current candidate is evolving.
// ---------------------------------------------------------------------------
enum MotifGrowthState : uint8_t {
    MGS_IDLE               = 0,  // No active candidates
    MGS_DRIFTING           = 1,  // Candidates exist but none are consolidating
    MGS_STABILIZING        = 2,  // Best candidate has crossed the halfway mark
    MGS_READY_FOR_PROMOTION = 3, // Best candidate has reached the stability target
};

// ---------------------------------------------------------------------------
// MotifCandidate — a single slot in the ring buffer.
// ---------------------------------------------------------------------------
struct MotifCandidate {
    float   rms;
    float   variance;
    float   hum;
    float   texture;
    float   transient;
    uint8_t stability_count;  // number of confirmed observations
    float   strength;         // [0,1] decays when not confirmed
    bool    active;
};

// ---------------------------------------------------------------------------
// PromotedMotif — snapshot passed to StatePublishing and DisplayModule
// when a candidate is promoted to a concept.
// ---------------------------------------------------------------------------
struct PromotedMotif {
    float   rms, variance, hum, texture, transient;
    uint8_t final_stability;
};

// ---------------------------------------------------------------------------
// MotifModule
//
// Maintains MOTIF_SLOTS candidate motifs as a pool (not a strict ring — slots
// are reused by strength). Each incoming feature vector either:
//   (a) confirms an existing candidate close enough in feature space, or
//   (b) starts a new candidate, evicting the weakest existing one.
//
// When a candidate's stability_count reaches MOTIF_STABILITY_TARGET it is
// flagged for promotion. checkForPromotion() returns true once and clears
// the slot, producing a PromotedMotif for the caller.
// ---------------------------------------------------------------------------
class MotifModule {
public:
    MotifModule();

    // Feed a fresh feature vector into the motif tracker.
    void updateFromFeatures(const AcousticFeatures& f);

    // Returns true (once) when a candidate reaches the promotion threshold.
    // Fills *out if provided and clears the promoted slot.
    bool checkForPromotion(PromotedMotif* out = nullptr);

    // Current aggregate growth state of the candidate pool.
    MotifGrowthState getMotifGrowthState() const;

    // Strongest active candidate (may be nullptr if pool is empty).
    const MotifCandidate* getBestCandidate() const;

    // Reset all candidates (e.g., after memory format migration).
    void reset();

private:
    MotifCandidate _slots[MOTIF_SLOTS];
    int            _promotionSlot;  // index of ready-to-promote slot, or -1

    // Normalised Euclidean distance in feature space.
    float distance(const AcousticFeatures& f, const MotifCandidate& m) const;

    // Index of the slot with the lowest strength (eviction target).
    int findWeakestSlot() const;

    // Normalisation scales — chosen to make each dimension ~unit-variance
    // for a typical quiet indoor environment.
    static constexpr float SCALE_RMS       = 1500.f;
    static constexpr float SCALE_VAR       = 2.25e6f;  // (1500)²
    static constexpr float SCALE_HUM       = 400.f;
    static constexpr float SCALE_TEX       = 0.8f;
    static constexpr float SCALE_TRANSIENT = 8.f;
};
