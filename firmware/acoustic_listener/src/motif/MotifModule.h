#pragma once
#include <Arduino.h>
#include "../config.h"
#include "../acoustic/AcousticSensingModule.h"
#include "../baseline/BaselineAndConfidenceModule.h"

// MotifCandidate — a compact acoustic fingerprint being tracked.
// Stored in both the live ring buffer and persisted in MemoryBlock.
struct MotifCandidate {
    float    rms;
    float    variance;
    float    hum_energy;
    float    transient_ratio;
    uint16_t stability_count;   // windows where this motif matched (increments)
    uint8_t  age_windows;       // windows since first seen (saturates at 255)
    bool     active;            // slot is occupied
};

// The slow-growth state of the motif ring as a whole.
// Reflects the most-mature candidate currently in the ring.
//   IDLE         — no active candidates
//   ACCUMULATING — at least one candidate, max stability_count < PROMOTE_COUNT/2
//   STABILIZING  — a candidate is past half-way to promotion
//   PROMOTED     — (transient) a candidate just crossed MOTIF_PROMOTE_COUNT
//                  Cleared on the next updateFromFeatures() call.
enum class MotifGrowthState { IDLE, ACCUMULATING, STABILIZING, PROMOTED };

// Helper — returns a short string for JSON serialisation.
inline const char* motifStateStr(MotifGrowthState s) {
    switch (s) {
        case MotifGrowthState::IDLE:         return "IDLE";
        case MotifGrowthState::ACCUMULATING: return "ACCUMULATING";
        case MotifGrowthState::STABILIZING:  return "STABILIZING";
        case MotifGrowthState::PROMOTED:     return "PROMOTED";
    }
    return "IDLE";
}

// MotifModule
// Responsibilities:
//   - Maintain a ring buffer of up to MOTIF_RING_SIZE candidate acoustic motifs.
//   - Match incoming feature windows against existing candidates via L1 distance
//     on [rms, variance, hum_energy, transient_ratio] (no multiplication needed).
//   - Increment stability_count when a match is found.
//   - Promote a candidate to a concept when it reaches MOTIF_PROMOTE_COUNT.
//   - Fire a concept_formation_event flag consumed by Display and Publisher.
//
// Eviction policy: when the ring is full and no matching slot exists, overwrite
// the candidate with the lowest stability_count (least established).
class MotifModule {
public:
    MotifModule();

    // Seed ring from persisted MemoryBlock on startup.
    void seedFromMemory(const MotifCandidate* motifs, uint8_t count);

    // Main update — call once per window when features are available.
    void updateFromFeatures(const AcousticFeatures& f,
                            const BaselineSnapshot& b);

    // Current growth state of the most-mature candidate.
    MotifGrowthState getGrowthState() const { return _growthState; }

    // Pointer to the most recently promoted candidate, or nullptr if none.
    // The pointer is valid until the next updateFromFeatures() call.
    const MotifCandidate* getPromotedMotif() const { return _promotedMotif; }
    int                   getPromotedIndex()  const { return _promotedIdx; }

    // Concept formation event flags — one per consumer.
    // Calling poll*() returns true once, then false until the next event.
    bool pollConceptFormationForDisplay()   { return _pollEvent(_conceptForDisplay); }
    bool pollConceptFormationForPublisher() { return _pollEvent(_conceptForPublisher); }

    // Read-only access to the full ring (for MemoryBlockModule to persist).
    const MotifCandidate* getRing() const { return _ring; }

    // Allow CareEthicsHooks to tune how strict matching is.
    void setSensitivityThreshold(float t) { _sensitivityThreshold = t; }

private:
    MotifCandidate   _ring[MOTIF_RING_SIZE];
    MotifGrowthState _growthState = MotifGrowthState::IDLE;

    const MotifCandidate* _promotedMotif = nullptr;
    int                   _promotedIdx   = -1;

    bool _conceptForDisplay   = false;
    bool _conceptForPublisher = false;

    float _sensitivityThreshold = MOTIF_L1_MATCH_EPSILON;

    // ── Internal helpers ──────────────────────────────────────────────────────
    int  _findMatch(const AcousticFeatures& f) const;
    int  _findFreeSlot() const;
    int  _findEvictSlot() const;   // lowest stability_count
    void _addCandidate(const AcousticFeatures& f, int slot);
    void _updateGrowthState();
    bool _pollEvent(bool& flag);   // returns flag, then clears it

    // L1 distance on normalised [rms, variance, hum_energy, transient_ratio].
    float _l1Distance(const MotifCandidate& c, const AcousticFeatures& f) const;
};
