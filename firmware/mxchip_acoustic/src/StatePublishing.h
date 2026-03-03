#pragma once
#include <Arduino.h>
#include "BaselineConfidence.h"
#include "MotifModule.h"

// ---------------------------------------------------------------------------
// StatePublishingModule
//
// Publishes device state over MQTT (WiFi) with a Serial fallback when the
// network is unavailable. The publish cadence is adaptive:
//
//   - PUBLISH_FAST_MS  (30 s): during early life or when composite_stability
//     is below STABILITY_PUBLISH_THRESH — the device is still settling.
//   - PUBLISH_SLOW_MS  (5 min): once stable — quieter ecological footprint.
//
// Two topic streams:
//   TOPIC_STATUS : periodic snapshot (version, all confidence scores, state)
//   TOPIC_MOTIF  : event-driven, fired immediately on concept promotion
//
// Architecture note: MQTT is encapsulated behind publishJson(); to switch
// to a different transport (BLE, LoRa, serial-only) only this module changes.
// ---------------------------------------------------------------------------
class StatePublishingModule {
public:
    StatePublishingModule();

    // Call once in setup(). Attempts WiFi + MQTT; falls back gracefully.
    void begin();

    // Call every loop iteration. Publishes at the adaptive cadence.
    void maybePublish(const ConfidenceSnapshot& snap,
                      MotifGrowthState          mgs,
                      uint8_t vMajor, uint8_t vMinor,
                      uint32_t nowMs);

    // Publish a motif-promoted event immediately (bypasses cadence).
    void publishMotifEvent(const PromotedMotif& motif,
                           uint8_t vMajor, uint8_t vMinor,
                           uint32_t totalConcepts);

    // Care-ethics outbound: encode the device's ecological status into a
    // compact well-being signal for peer agents.
    // Returns: 0=unsettled, 1=learning, 2=stable
    static int wellBeingLevel(float compositeStability, MotifGrowthState mgs);

private:
    bool     _mqttReady;
    uint32_t _lastPublishMs;
    uint32_t _publishInterval;

    // Reconnect logic (non-blocking — returns false without blocking if down).
    bool ensureMqtt();

    // Low-level: publish a JSON string to a topic.
    // Falls back to Serial if MQTT is unavailable.
    void publishJson(const char* topic, const char* json);

    // Build the periodic status JSON into a caller-supplied buffer.
    void buildStatusJson(char* buf, size_t len,
                         const ConfidenceSnapshot& snap,
                         MotifGrowthState mgs,
                         uint8_t vMajor, uint8_t vMinor) const;
};
