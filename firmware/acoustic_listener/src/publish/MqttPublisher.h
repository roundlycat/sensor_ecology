#pragma once
// MqttPublisher — SKELETON ONLY, not connected in initial build.
// Implement when WiFi/MQTT is added. The IPublisher interface is already
// wired in StatePublishingModule and CareEthicsHooks, so enabling this
// is a one-line swap in main.cpp.
//
// Suggested lib_deps addition when implementing:
//   knolleary/PubSubClient @ ^2.8

#include "IPublisher.h"

class MqttPublisher : public IPublisher {
public:
    // TODO: inject WiFiClient and broker credentials via constructor
    MqttPublisher() {}

    void publish(const char* /*topic*/, const char* /*json*/) override {
        // TODO: mqtt.publish(topic, json)
    }

    bool isConnected() const override {
        return false;  // TODO: return mqtt.connected()
    }

    void maintain() override {
        // TODO: wifi + mqtt reconnect logic (non-blocking)
    }
};
