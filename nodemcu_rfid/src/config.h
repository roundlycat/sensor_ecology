#pragma once

// ── WiFi ──────────────────────────────────────────────────────────────────────
#define WIFI_SSID "xraycanard"
#define WIFI_PASS "gaqsob-jezgy2-maknuP"

// ── MQTT broker (Raspberry Pi) ────────────────────────────────────────────────
#define MQTT_HOST "192.168.0.25"
#define MQTT_PORT 1883

// ── Agent metadata ────────────────────────────────────────────────────────────
#define AGENT_LOCATION "living room"

// ── RC522 wiring (NodeMCU hardware SPI) ──────────────────────────────────────
// SCK  → D5 (GPIO14)   MOSI → D7 (GPIO13)
// MISO → D6 (GPIO12)   SS   → D8 (GPIO15)
// RST  → D3 (GPIO0)    VCC  → 3.3V   GND → GND
#define RFID_SS_PIN  15   // D8
#define RFID_RST_PIN  0   // D3

// ── Sensing behaviour ─────────────────────────────────────────────────────────
#define SCAN_INTERVAL_MS      100UL    // poll RFID at 10 Hz
#define DEBOUNCE_MS          2000UL    // ignore same UID within this window
#define HEARTBEAT_INTERVAL_MS 300000UL // 5-minute keepalive if nothing scanned
