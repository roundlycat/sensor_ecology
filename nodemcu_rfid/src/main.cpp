/*
 * Sensor Ecology — RC522 RFID Contact Agent (NodeMCU / ESP8266)
 *
 * Reads RFID/NFC card UIDs via an MFRC522 module and publishes
 * semantic contact events to the MQTT bus on the Raspberry Pi coordinator.
 *
 * Agent behaviour:
 *   - Registers itself with the ecology on boot
 *   - Publishes a contact event each time a new card is presented
 *   - Debounces: same UID suppressed for DEBOUNCE_MS after a scan
 *   - Falls back to a heartbeat every HEARTBEAT_INTERVAL_MS if nothing scanned
 *   - Reconnects WiFi and MQTT automatically
 *
 * Topic structure:
 *   agents/{agent_id}/registration   (boot, LWT)
 *   agents/{agent_id}/observation    (contact events)
 *
 * NodeMCU pin notes (hardware SPI):
 *   RC522 SCK  → D5 (GPIO14)
 *   RC522 MOSI → D7 (GPIO13)
 *   RC522 MISO → D6 (GPIO12)
 *   RC522 SS   → D8 (GPIO15)
 *   RC522 RST  → D3 (GPIO0)
 *   RC522 VCC  → 3.3V,  GND → GND
 */

#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <MFRC522.h>
#include "config.h"

// ── Identity ──────────────────────────────────────────────────────────────────
static String agentId;    // e.g. "nodemcu_rfid_a4cf12bc3d5e"
static String agentName;  // e.g. "rfid-bc3d5e"

// ── Hardware ──────────────────────────────────────────────────────────────────
static MFRC522 rfid(RFID_SS_PIN, RFID_RST_PIN);

// ── Networking ────────────────────────────────────────────────────────────────
static WiFiClient   wifiClient;
static PubSubClient mqtt(wifiClient);

// ── Scan state ────────────────────────────────────────────────────────────────
static String        lastUid;
static unsigned long lastUidMs    = 0;
static unsigned long lastPublishMs = 0;
static uint32_t      totalScans   = 0;
static uint32_t      msgSeq       = 0;


// ── Identity ──────────────────────────────────────────────────────────────────

void buildIdentity() {
    uint8_t mac[6];
    WiFi.macAddress(mac);
    char macHex[13];
    snprintf(macHex, sizeof(macHex), "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    agentId   = String("nodemcu_rfid_") + macHex;
    agentName = String("rfid-") + String(macHex + 6);  // last 3 bytes
}


// ── WiFi ──────────────────────────────────────────────────────────────────────

void connectWifi() {
    if (WiFi.status() == WL_CONNECTED) return;
    Serial.printf("WiFi → %s ", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
        delay(500);
        Serial.print(".");
    }
    if (WiFi.status() == WL_CONNECTED) {
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
    doc["agent_type"] = "rfid_node";
    doc["status"]     = isOnline ? "online" : "offline";
    doc["location"]   = AGENT_LOCATION;
    JsonArray caps = doc.createNestedArray("capabilities");
    caps.add("rfid_contact");
    mqttPublish(topic, doc, /*retained=*/true);
    Serial.printf("Registered: %s\n", agentId.c_str());
}

void publishObservation(const char*   obsType,
                         const String& summary,
                         float         confidence,
                         const String& uid,
                         uint32_t      scans) {
    String topic = "agents/" + agentId + "/observation";

    char msgId[64];
    snprintf(msgId, sizeof(msgId), "%s-%lu", agentId.c_str(), (unsigned long)++msgSeq);

    StaticJsonDocument<512> doc;
    doc["agent_id"]         = agentId;
    doc["observation_type"] = obsType;
    doc["semantic_summary"] = summary;
    doc["confidence"]       = (float)round(confidence * 100) / 100;
    doc["message_id"]       = msgId;

    JsonObject raw = doc.createNestedObject("raw_data");
    raw["uid_hex"]      = uid;
    raw["scan_count"]   = scans;
    raw["source"]       = "mfrc522";

    if (mqttPublish(topic, doc)) {
        Serial.printf("[obs] %s — %s\n", obsType, summary.c_str());
        lastPublishMs = millis();
    }
}


// ── UID helpers ───────────────────────────────────────────────────────────────

String uidToHex(MFRC522::Uid& uid) {
    String s = "";
    for (byte i = 0; i < uid.size; i++) {
        if (uid.uidByte[i] < 0x10) s += "0";
        s += String(uid.uidByte[i], HEX);
    }
    s.toUpperCase();
    return s;
}


// ── Interpretation logic ──────────────────────────────────────────────────────

void interpretAndPublish(const String& uid) {
    totalScans++;

    String summary = "RFID contact: tag UID " + uid
                   + " presented at " + AGENT_LOCATION
                   + ". Contact event #" + String(totalScans) + ".";

    publishObservation("rfid_contact", summary, 0.99f, uid, totalScans);
}


// ── MQTT connection ───────────────────────────────────────────────────────────

bool connectMqtt() {
    String clientId = "ecology-" + agentId;

    String lwt            = "agents/" + agentId + "/registration";
    String offlinePayload = "{\"agent_id\":\"" + agentId + "\",\"status\":\"offline\"}";

    bool ok = mqtt.connect(
        clientId.c_str(),
        nullptr, nullptr,
        lwt.c_str(), 1, true,
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
    Serial.println("\n\n=== Sensor Ecology — RFID Agent (NodeMCU) ===");

    connectWifi();
    buildIdentity();
    Serial.printf("Agent ID : %s\n", agentId.c_str());
    Serial.printf("MQTT     : %s:%d\n", MQTT_HOST, MQTT_PORT);
    Serial.printf("Location : %s\n", AGENT_LOCATION);

    SPI.begin();
    rfid.PCD_Init();
    rfid.PCD_DumpVersionToSerial();
    Serial.println("RC522 ready");

    mqtt.setServer(MQTT_HOST, MQTT_PORT);
    mqtt.setBufferSize(512);
    connectMqtt();

    lastPublishMs = millis();
}

void loop() {
    if (WiFi.status() != WL_CONNECTED) {
        connectWifi();
        return;
    }

    if (!mqtt.connected()) {
        static unsigned long lastMqttRetryMs = 0;
        if (millis() - lastMqttRetryMs >= 5000) {
            lastMqttRetryMs = millis();
            connectMqtt();
        }
    }
    mqtt.loop();

    // Heartbeat if nothing has been scanned for a while
    if (millis() - lastPublishMs >= HEARTBEAT_INTERVAL_MS) {
        String summary = "RFID agent at " + String(AGENT_LOCATION)
                       + " standing by. Total contacts: " + String(totalScans);
        publishObservation("nominal_conditions", summary, 0.70f, "", totalScans);
    }

    // Poll for a card
    if (millis() - lastUidMs < SCAN_INTERVAL_MS) return;
    lastUidMs = millis();

    if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) return;

    String uid = uidToHex(rfid.uid);
    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();

    // Debounce — skip if same card seen very recently
    if (uid == lastUid && (millis() - lastUidMs) < DEBOUNCE_MS) return;

    lastUid   = uid;
    lastUidMs = millis();

    Serial.printf("[scan] UID: %s\n", uid.c_str());

    if (mqtt.connected()) {
        interpretAndPublish(uid);
    }
}
