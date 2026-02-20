/*
 * Sensor Ecology — BME280 Environmental Agent
 *
 * Reads temperature, humidity, and pressure from a BME280 and publishes
 * semantic interpretations to the MQTT bus on the Raspberry Pi coordinator.
 *
 * Agent behaviour:
 *   - Registers itself with the ecology on boot
 *   - Only publishes when something significant changes
 *   - Falls back to a heartbeat every HEARTBEAT_INTERVAL_MS if conditions hold steady
 *   - Reconnects WiFi and MQTT automatically
 *
 * Topic structure:
 *   agents/{agent_id}/registration   (boot, LWT)
 *   agents/{agent_id}/observation    (interpretations)
 */

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_BME280.h>
#include <Adafruit_Sensor.h>
#include "config.h"

// ── Identity ──────────────────────────────────────────────────────────────────
static String agentId;    // e.g. "esp32_bme280_a4cf12bc3d5e"
static String agentName;  // e.g. "bme280-bc3d5e"

// ── Hardware ──────────────────────────────────────────────────────────────────
static Adafruit_BME280 bme;
static bool bmeOk = false;

// ── Networking ────────────────────────────────────────────────────────────────
static WiFiClient   wifiClient;
static PubSubClient mqtt(wifiClient);

// ── Sense state ───────────────────────────────────────────────────────────────
static float prevTemp = NAN;
static float prevHum  = NAN;
static float prevPres = NAN;

static unsigned long lastSenseMs     = 0;
static unsigned long lastPublishMs   = 0;
static uint32_t      msgSeq          = 0;


// ── Identity ──────────────────────────────────────────────────────────────────

void buildIdentity() {
    // Use MAC address for a stable, unique agent ID across reboots
    uint8_t mac[6];
    WiFi.macAddress(mac);
    char macHex[13];
    snprintf(macHex, sizeof(macHex), "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    agentId   = String("esp32_bme280_") + macHex;
    agentName = String("bme280-") + String(macHex + 6);  // last 3 bytes
}


// ── WiFi ──────────────────────────────────────────────────────────────────────

void connectWifi() {
    if (WiFi.isConnected()) return;
    Serial.printf("WiFi → %s ", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    for (int i = 0; i < 40 && !WiFi.isConnected(); i++) {
        delay(500);
        Serial.print(".");
    }
    if (WiFi.isConnected()) {
        Serial.printf(" connected (%s)\n", WiFi.localIP().toString().c_str());
    } else {
        Serial.println(" FAILED, will retry");
    }
}


// ── MQTT helpers ──────────────────────────────────────────────────────────────

bool mqttPublish(const String& topic, JsonDocument& doc, bool retained = false) {
    char buf[512];
    size_t len = serializeJson(doc, buf, sizeof(buf));
    bool ok = mqtt.publish(topic.c_str(), (const uint8_t*)buf, len, retained);
    if (!ok) Serial.printf("MQTT publish failed on %s\n", topic.c_str());
    return ok;
}

void publishRegistration(bool isOnline) {
    String topic = "agents/" + agentId + "/registration";
    StaticJsonDocument<256> doc;
    doc["agent_id"]   = agentId;
    doc["agent_name"] = agentName;
    doc["agent_type"] = "bme280_node";
    doc["status"]     = isOnline ? "online" : "offline";
    doc["location"]   = AGENT_LOCATION;
    JsonArray caps = doc.createNestedArray("capabilities");
    caps.add("temperature");
    caps.add("humidity");
    caps.add("pressure");
    mqttPublish(topic, doc, /*retained=*/true);
    Serial.printf("Registered: %s\n", agentId.c_str());
}

void publishObservation(const char*   obsType,
                         const String& summary,
                         float         confidence,
                         float temp, float hum, float pres) {
    String topic = "agents/" + agentId + "/observation";

    char msgId[56];
    snprintf(msgId, sizeof(msgId), "%s-%lu", agentId.c_str(), (unsigned long)++msgSeq);

    StaticJsonDocument<512> doc;
    doc["agent_id"]         = agentId;
    doc["observation_type"] = obsType;
    doc["semantic_summary"] = summary;
    doc["confidence"]       = (float)round(confidence * 100) / 100;
    doc["message_id"]       = msgId;
    // ts is omitted — Pi bridge timestamps on receipt

    JsonObject raw = doc.createNestedObject("raw_data");
    raw["temperature_c"] = round(temp * 10) / 10.0;
    raw["humidity_pct"]  = round(hum  * 10) / 10.0;
    raw["pressure_hpa"]  = round(pres * 10) / 10.0;
    raw["source"]        = "bme280";

    if (mqttPublish(topic, doc)) {
        Serial.printf("[obs] %s — %s\n", obsType, summary.c_str());
        lastPublishMs = millis();
    }
}


// ── Interpretation logic ──────────────────────────────────────────────────────

const char* comfortLabel(float temp, float hum) {
    bool tempOk = temp >= 18.0f && temp <= 26.0f;
    bool humOk  = hum  >= 30.0f && hum  <= 65.0f;
    if (tempOk && humOk) return "comfortable";
    if (temp > 26.0f)    return "warm";
    if (temp < 18.0f)    return "cool";
    if (hum  > 65.0f)    return "humid";
    return "dry";
}

void interpretAndPublish(float temp, float hum, float pres) {
    unsigned long now = millis();

    bool firstReading  = isnan(prevTemp);
    bool heartbeatDue  = (now - lastPublishMs) >= HEARTBEAT_INTERVAL_MS;

    bool tempChanged = !firstReading && fabsf(temp - prevTemp) >= TEMP_THRESHOLD_C;
    bool humChanged  = !firstReading && fabsf(hum  - prevHum)  >= HUM_THRESHOLD_PCT;
    bool presChanged = !firstReading && fabsf(pres - prevPres) >= PRES_THRESHOLD_HPA;

    if (tempChanged) {
        float delta  = temp - prevTemp;
        String dir   = delta > 0 ? "rising" : "falling";
        String msg   = "Temperature " + dir + ": "
                     + String(prevTemp, 1) + "°C → " + String(temp, 1) + "°C"
                     + " (" + (delta > 0 ? "+" : "") + String(delta, 1) + "°C). "
                     + "Humidity " + String(hum, 0) + "%, "
                     + "pressure " + String(pres, 0) + " hPa";
        publishObservation("thermal_change", msg, 0.88f, temp, hum, pres);

    } else if (presChanged) {
        float delta  = pres - prevPres;
        String dir   = delta > 0 ? "rising" : "falling";
        const char* outlook = (delta < 0)
            ? "possibly an incoming weather system"
            : "conditions may be improving";
        String msg = "Pressure " + dir + ": "
                   + String(prevPres, 0) + " → " + String(pres, 0) + " hPa"
                   + " (" + (delta > 0 ? "+" : "") + String(delta, 1) + " hPa) — "
                   + outlook;
        publishObservation("pressure_change", msg, 0.80f, temp, hum, pres);

    } else if (humChanged) {
        float delta = hum - prevHum;
        String dir  = delta > 0 ? "rising" : "falling";
        String msg  = "Humidity " + dir + ": "
                    + String(prevHum, 0) + "% → " + String(hum, 0) + "%. "
                    + "Temperature " + String(temp, 1) + "°C";
        publishObservation("humidity_change", msg, 0.75f, temp, hum, pres);

    } else if (firstReading || heartbeatDue) {
        String msg = "Stable conditions: "
                   + String(temp, 1) + "°C, "
                   + String(hum, 0) + "% RH, "
                   + String(pres, 0) + " hPa. "
                   + "Environment is " + comfortLabel(temp, hum);
        publishObservation("nominal_conditions", msg, 0.70f, temp, hum, pres);
    }

    prevTemp = temp;
    prevHum  = hum;
    prevPres = pres;
}


// ── MQTT connection ───────────────────────────────────────────────────────────

bool connectMqtt() {
    String clientId = "ecology-" + agentId;

    // Last Will: mark agent offline if connection drops unexpectedly
    String lwt = "agents/" + agentId + "/registration";
    String offlinePayload = "{\"agent_id\":\"" + agentId + "\",\"status\":\"offline\"}";

    bool ok = mqtt.connect(
        clientId.c_str(),
        nullptr, nullptr,           // no username/password
        lwt.c_str(), 1, true,       // LWT: QoS 1, retained
        offlinePayload.c_str()
    );

    if (ok) {
        Serial.println("MQTT connected");
        publishRegistration(true);
    } else {
        Serial.printf("MQTT failed (rc=%d), will retry\n", mqtt.state());
    }
    return ok;
}


// ── Arduino lifecycle ─────────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println("\n\n=== Sensor Ecology — BME280 Agent ===");

    // WiFi needed before identity (MAC address)
    connectWifi();
    buildIdentity();
    Serial.printf("Agent ID : %s\n", agentId.c_str());
    Serial.printf("MQTT     : %s:%d\n", MQTT_HOST, MQTT_PORT);
    Serial.printf("Location : %s\n", AGENT_LOCATION);

    // BME280
    Wire.begin(BME_SDA, BME_SCL);
    if (bme.begin(BME_I2C_ADDR, &Wire)) {
        bmeOk = true;
        // Forced mode: sensor sleeps between readings, lowest power draw
        bme.setSampling(
            Adafruit_BME280::MODE_FORCED,
            Adafruit_BME280::SAMPLING_X2,   // temperature oversampling
            Adafruit_BME280::SAMPLING_X2,   // pressure oversampling
            Adafruit_BME280::SAMPLING_X2,   // humidity oversampling
            Adafruit_BME280::FILTER_X4      // IIR filter coefficient
        );
        Serial.printf("BME280 ready at 0x%02X\n", BME_I2C_ADDR);
    } else {
        Serial.printf("BME280 not found at 0x%02X — check wiring or try 0x77\n",
                      BME_I2C_ADDR);
    }

    mqtt.setServer(MQTT_HOST, MQTT_PORT);
    mqtt.setBufferSize(512);
    connectMqtt();

    // Force first reading immediately
    lastSenseMs = millis() - SENSE_INTERVAL_MS;
}

void loop() {
    // Maintain WiFi
    if (!WiFi.isConnected()) {
        connectWifi();
        return;
    }

    // Maintain MQTT
    if (!mqtt.connected()) {
        static unsigned long lastMqttRetryMs = 0;
        if (millis() - lastMqttRetryMs >= 5000) {
            lastMqttRetryMs = millis();
            connectMqtt();
        }
    }
    mqtt.loop();

    // Sense on interval
    if (bmeOk && millis() - lastSenseMs >= SENSE_INTERVAL_MS) {
        lastSenseMs = millis();

        bme.takeForcedMeasurement();  // required in MODE_FORCED
        float temp = bme.readTemperature();
        float hum  = bme.readHumidity();
        float pres = bme.readPressure() / 100.0f;  // Pa → hPa

        Serial.printf("[sense] %.1f°C  %.0f%%  %.0f hPa\n", temp, hum, pres);

        if (mqtt.connected()) {
            interpretAndPublish(temp, hum, pres);
        }
    }

    delay(10);
}
