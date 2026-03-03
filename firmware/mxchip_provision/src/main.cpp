/*
 * MXChip AZ3166 — WiFi credential provisioning
 * =============================================
 * Calls WiFi.begin(ssid, pass) which writes credentials into the EMW3166
 * module's persistent flash.  After this sketch runs successfully, flash
 * the main mxchip_acoustic firmware — SystemWiFiConnect() will reconnect
 * using the stored credentials without needing them hardcoded.
 *
 * Flash:   pio run -t upload   (from firmware/mxchip_provision/)
 * Monitor: pio device monitor
 * Done when Serial prints "Credentials stored."
 */

#include <Arduino.h>
#include "AZ3166WiFi.h"

#define WIFI_SSID  "xraycanard"
#define WIFI_PASS  "gaqsob-jezgy2-maknuP"

static bool done = false;

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println();
    Serial.println("[provision] ===== WiFi Provisioning =====");
    Serial.printf ("[provision] SSID: %s\n", WIFI_SSID);
    Serial.println("[provision] Connecting...");

    int status = WiFi.begin(WIFI_SSID, WIFI_PASS);

    if (status == WL_CONNECTED) {
        Serial.println("[provision] Connected!");
        Serial.print ("[provision] IP address: ");
        Serial.println(WiFi.localIP());
        Serial.println("[provision] Credentials stored.");
        Serial.println("[provision] You can now flash mxchip_acoustic.");
        done = true;
    } else {
        Serial.printf("[provision] FAILED (status=%d)\n", status);
        Serial.println("[provision] Check SSID / password and retry.");
    }
}

void loop() {
    if (done) {
        // Blink the user LED to show success.
        digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
        delay(500);
    } else {
        // Retry every 10 s if first attempt failed.
        delay(10000);
        Serial.println("[provision] Retrying...");
        int status = WiFi.begin(WIFI_SSID, WIFI_PASS);
        if (status == WL_CONNECTED) {
            Serial.println("[provision] Connected — credentials stored.");
            Serial.println("[provision] You can now flash mxchip_acoustic.");
            done = true;
        } else {
            Serial.printf("[provision] Still failed (status=%d)\n", status);
        }
    }
}
