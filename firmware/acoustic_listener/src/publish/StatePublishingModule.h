#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include "../config.h"
#include "IPublisher.h"
#include "../baseline/BaselineAndConfidenceModule.h"
#include "../motif/MotifModule.h"
#include "../memory/MemoryBlockModule.h"

// StatePublishingModule
// Responsibilities:
//   - Emit a status snapshot JSON at an adaptive cadence based on
//     device maturity (version_minor) and stability.
//   - Emit a motif_event JSON whenever a motif is promoted.
//   - Route all output through the injected IPublisher (Serial by default).
//
// Publish cadence (from config.h):
//   version_minor < 3 OR uptime < 300s  → STATUS_FAST_INTERVAL_MS  (30s)
//   composite_stability > 0.8           → STATUS_SLOW_INTERVAL_MS (300s)
//   otherwise                           → STATUS_MID_INTERVAL_MS   (60s)
//
// JSON schemas are defined in the spec and reproduced in the implementation.
// No Serial.print in hot paths — all output goes through IPublisher::publish().
class StatePublishingModule {
public:
    // publisher must outlive this object.
    explicit StatePublishingModule(IPublisher& publisher);

    // Conditionally send a status snapshot. Call every loop().
    void maybeSendStatus(uint32_t now,
                         const BaselineSnapshot& b,
                         MotifGrowthState state,
                         const MemoryBlock& mem);

    // Conditionally send a motif_event. Call every loop() after motif.update().
    // Pass the promoted motif and its ring index (both from MotifModule).
    // If the publisher flag has already been cleared (no new event), this is a no-op.
    void maybeSendMotifEvent(uint32_t now,
                             const MotifCandidate* promoted,
                             int promoted_idx,
                             bool has_event);

private:
    IPublisher& _pub;
    uint32_t    _lastStatusMs  = 0;
    uint32_t    _lastMotifMs   = 0;

    uint32_t _pickStatusInterval(const BaselineSnapshot& b,
                                 const MemoryBlock& mem,
                                 uint32_t now) const;
};
