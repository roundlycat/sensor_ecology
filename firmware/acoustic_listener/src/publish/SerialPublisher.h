#pragma once
#include "IPublisher.h"

// SerialPublisher — default transport over USB CDC Serial.
// Output format: one JSON object per line, prefixed with the topic in brackets.
// Example:  [acoustic/status] {"type":"status", ...}
//
// Serial is always "connected" — isConnected() returns true unconditionally.
// maintain() is a no-op (Serial has no reconnect concept).
class SerialPublisher : public IPublisher {
public:
    SerialPublisher() = default;

    void publish(const char* topic, const char* json) override;
    bool isConnected() const override { return true; }
    void maintain() override {}  // Serial needs no maintenance
};
