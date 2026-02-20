#pragma once

// ── WiFi ──────────────────────────────────────────────────────────────────────
#define WIFI_SSID "xraycanard"
#define WIFI_PASS "gaqsob-jezgy2-maknuP"

// ── MQTT broker (Raspberry Pi) ────────────────────────────────────────────────
#define MQTT_HOST "192.168.0.25"
#define MQTT_PORT 1883

// ── Agent metadata ────────────────────────────────────────────────────────────
#define AGENT_LOCATION "living room"

// ── BME280 wiring (NodeMCU) ───────────────────────────────────────────────────
// D2 = GPIO4 = SDA,  D1 = GPIO5 = SCL
#define BME_SDA      4
#define BME_SCL      5
// I2C address: 0x76 when SDO is low (default), 0x77 when SDO is high
#define BME_I2C_ADDR 0x77

// ── Sensing behaviour ─────────────────────────────────────────────────────────
#define SENSE_INTERVAL_MS     30000UL   // 30 seconds
#define HEARTBEAT_INTERVAL_MS 600000UL  // 10 minutes

// Change thresholds — only publish if a value moves by more than this
#define TEMP_THRESHOLD_C   0.5f
#define HUM_THRESHOLD_PCT  2.0f
#define PRES_THRESHOLD_HPA 0.5f
