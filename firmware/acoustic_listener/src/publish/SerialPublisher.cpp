#include "SerialPublisher.h"
#include <Arduino.h>

void SerialPublisher::publish(const char* topic, const char* json) {
    // Format: [topic] {json}\n
    // One JSON object per line for easy parsing by downstream consumers
    // (e.g. the dashboard's ingestion layer).
    Serial.print('[');
    Serial.print(topic);
    Serial.print("] ");
    Serial.println(json);
}
