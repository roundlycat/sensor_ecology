#pragma once

// ── WiFi ─────────────────────────────────────────────────────────────────────
#define WIFI_SSID "xraycanard"
#define WIFI_PASS "gaqsob-jezgy2-maknuP"

// ── MQTT broker (Raspberry Pi IP) ────────────────────────────────────────────
#define MQTT_HOST "192.168.0.25"
#define MQTT_PORT 1883

// ── Agent metadata ────────────────────────────────────────────────────────────
// Where is this node physically located?
#define AGENT_LOCATION "living room"

// ── BME280 wiring ─────────────────────────────────────────────────────────────
// Default ESP32 I2C pins. Change if you've wired differently.
#define BME_SDA      21
#define BME_SCL      22
// I2C address: 0x76 when SDO is low (default), 0x77 when SDO is high
#define BME_I2C_ADDR 0x76

// ── Sensing behaviour ─────────────────────────────────────────────────────────
// How often to read the sensor
#define SENSE_INTERVAL_MS     30000UL   // 30 seconds

// Publish a heartbeat even if nothing changes
#define HEARTBEAT_INTERVAL_MS 600000UL  // 10 minutes

// Change thresholds — only publish if a value moves by more than this
#define TEMP_THRESHOLD_C  0.5f   // degrees Celsius
#define HUM_THRESHOLD_PCT 2.0f   // percent relative humidity
#define PRES_THRESHOLD_HPA 0.5f  // hPa
