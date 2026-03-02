#pragma once
// IPublisher — pure abstract transport interface
// All publishing goes through this seam. SerialPublisher is the default.
// MqttPublisher is a skeleton for future connectivity.
// CareEthicsHooks outbound signals also route through here.

class IPublisher {
public:
    virtual ~IPublisher() = default;

    // Send a null-terminated JSON string on the given topic/channel.
    // Implementations are responsible for framing (e.g. topic prefix for MQTT,
    // or a plain newline-terminated line for Serial).
    virtual void publish(const char* topic, const char* json) = 0;

    // Return true if the transport is currently able to deliver messages.
    // Serial is always connected; MQTT may not be.
    virtual bool isConnected() const = 0;

    // Called once per loop() — allow the transport to do housekeeping
    // (MQTT reconnect, buffer flush, etc.). Must not block.
    virtual void maintain() = 0;
};
