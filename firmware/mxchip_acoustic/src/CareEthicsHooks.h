#pragma once
#include <Arduino.h>
#include "BaselineConfidence.h"
#include "MotifModule.h"

// ---------------------------------------------------------------------------
// CareEthicsHooks — bidirectional ecological influence interface.
//
// This device is a member of an ecology of agents (Pi nodes, Unity scenes,
// other embedded devices). In a care-ethics framing, agents can be both
// caregiver and cared-for, and influence flows both ways — not as commands
// but as gentle suggestions that each agent may accept or attenuate.
//
// ── INBOUND (other agents caring for this device) ───────────────────────────
//
//   setLearningPace(ratio)
//     Another agent notices this device is reacting too quickly or too slowly
//     to environmental changes and suggests adjusting the learning rate.
//     ratio > 1.0 → more inertia ("you seem stressed — settle down")
//     ratio < 1.0 → more responsive ("you seem stuck — open up a little")
//     The suggestion is blended into the device's own multiplier at 30%
//     weight — the device retains 70% of its own previous position.
//
//   receiveEncouragement(trustLevel 0..1)
//     A peer signals that this device's baselines are trustworthy and should
//     be preserved. Higher trust → higher inertia → slower drift.
//     This is a form of "I see you, your readings are valuable."
//
//   nudgeThreshold(transientHint)
//     A peer hints that the environment is unusually loud or quiet.
//     Logged and held as a prior; the device's own IIR will naturally
//     converge, but the hint speeds up that convergence in the first
//     few hundred samples after a reset.
//
// ── OUTBOUND (this device contributing to the ecology) ──────────────────────
//
//   emitWellBeingSignal(stability, mgs) → int 0/1/2
//     0 = unsettled (other agents might give this device space)
//     1 = learning  (active growth — other agents might listen more)
//     2 = stable    (mature — this device's readings are most reliable)
//
//   emitConceptSignal(buf, len, ...) → int (bytes written)
//     Formats a compact JSON fragment describing a newly promoted concept.
//     The caller is responsible for publishing this (via StatePublishing
//     or a separate care channel).
//
//   getSocialPresenceByte(stability, mgs, vMinor) → uint8_t
//     A single byte encoding the device's current ecological identity:
//       bits 1-0: well-being (0/1/2)
//       bits 3-2: motif state (MGS enum, 0-3)
//       bits 7-4: version_minor low nibble
//     Peers can store and compare this over time to track social change.
//
// ── FUTURE HOOKS (stubs) ────────────────────────────────────────────────────
//   onPeerDiscovered(peerId)  — another device comes online
//   onPeerFarewell(peerId)    — a peer goes offline; device may slow learning
//   onEcologyStress(level)    — system-wide signal (e.g., from a Pi coordinator)
//
// ---------------------------------------------------------------------------

class CareEthicsHooks {
public:
    CareEthicsHooks();

    // Bind to the live module instances (call before setup() completes).
    void bindModules(BaselineAndConfidenceModule* baseline,
                     MotifModule*                 motif);

    // ── Inbound ─────────────────────────────────────────────────────────────

    // Soft learning-rate adjustment from a peer.
    // Blended at 30% external / 70% current to preserve device agency.
    void setLearningPace(float ratio);

    // Peer expresses trust; slows drift to preserve established baselines.
    void receiveEncouragement(float trustLevel);

    // Peer hints at an unusual acoustic environment.
    // Stores the hint for use during the next fresh calibration window.
    void nudgeThreshold(float transientHint);

    // ── Outbound ────────────────────────────────────────────────────────────

    // Returns 0 (unsettled) / 1 (learning) / 2 (stable).
    static int emitWellBeingSignal(float compositeStability,
                                   MotifGrowthState mgs);

    // Formats a concept-formation JSON fragment into buf.
    // Returns the number of bytes written (excluding null terminator).
    static int emitConceptSignal(char* buf, size_t len,
                                 float rms, float hum, float texture,
                                 uint8_t vMajor, uint8_t vMinor);

    // Compact social-presence byte for peer consumption.
    static uint8_t getSocialPresenceByte(float stability,
                                         MotifGrowthState mgs,
                                         uint8_t vMinor);

    // ── Future stubs ────────────────────────────────────────────────────────

    // Call when a peer device publishes its presence.
    // This device might adjust its learning pace or publish more frequently.
    void onPeerDiscovered(uint8_t peerId, uint8_t peerPresenceByte);

    // Call when a peer goes silent for longer than expected.
    // This device may slow its own learning ("someone I relied on is gone").
    void onPeerFarewell(uint8_t peerId);

    // System-wide ecology stress signal from a coordinator (e.g., Pi node).
    // level 0.0 = calm, 1.0 = high stress — all devices should slow learning.
    void onEcologyStress(float level);

    // Current threshold hint stored by nudgeThreshold() (for use by caller).
    float getPendingThresholdHint() const { return _thresholdHint; }
    bool  hasThresholdHint()        const { return _hasThresholdHint; }
    void  clearThresholdHint()            { _hasThresholdHint = false; }

private:
    BaselineAndConfidenceModule* _baseline;
    MotifModule*                 _motif;

    float _trustAccum;       // smoothed trust level from receiveEncouragement()
    float _thresholdHint;    // stored from nudgeThreshold()
    bool  _hasThresholdHint;
};
