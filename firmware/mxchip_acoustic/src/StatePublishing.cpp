#include "StatePublishing.h"
#include "config.h"

// ---------------------------------------------------------------------------
// MQTT + WiFi includes
// The MXChip framework bundles Eclipse Paho Embedded MQTT over WiFiClient.
// If compilation fails here, check that framework-arduinostm32mxchip is
// installed (it should be if the ststm32 platform is installed via PIO).
// ---------------------------------------------------------------------------
// MQTTNetwork wraps a mbed TCPSocket; it has the exact int write(buf, len, timeout)
// and int read(buf, len, timeout) signatures that MQTTClient's template expects.
// WiFiClient (Arduino-style) does NOT have those signatures.
#include "MQTTNetwork.h"   // MQTTNetwork class (uses mbed TCPSocket + WiFiInterface())
#include "MQTTmbed.h"      // Countdown (mbed Timer wrapper)
#include "MQTTClient.h"    // Paho embedded MQTT client template
#include "SystemWiFi.h"    // InitSystemWiFi(), SystemWiFiConnect()
#include "rtos.h"          // mbed RTOS Thread + Semaphore for WiFi timeout

// ---------------------------------------------------------------------------
// WiFi connection runs in a background thread so the main loop is not blocked
// if the AP is absent or slow.  A Semaphore signals completion; the caller
// waits at most WIFI_CONNECT_TIMEOUT_MS before giving up and proceeding in
// serial-only mode.
// ---------------------------------------------------------------------------
static rtos::Semaphore g_wifiSem(0);
static bool            g_wifiOk = false;

static void wifiConnectTask() {
    g_wifiOk = SystemWiFiConnect();
    g_wifiSem.release();
}

static MQTTNetwork g_net;
static MQTT::Client<MQTTNetwork, Countdown, 256, 2> g_mqtt(g_net);

// ---------------------------------------------------------------------------
// ensureWifi() — attempt WiFi connect with a hard timeout.
// Runs SystemWiFiConnect() in a background thread; the calling thread waits
// at most WIFI_CONNECT_TIMEOUT_MS.  Returns true if connected.
// On timeout the driver thread is terminated; the EMW10xx module will be in
// an unknown state, but since we run serial-only after this the radio is
// not used until the next retry interval.
// ---------------------------------------------------------------------------
static bool ensureWifi() {
    // Drain any leftover token from a prior release before this wait.
    while (g_wifiSem.wait(0) > 0) {}
    g_wifiOk = false;

    rtos::Thread wifiThread(osPriorityNormal, 8192);
    wifiThread.start(wifiConnectTask);

    int32_t got = g_wifiSem.wait(WIFI_CONNECT_TIMEOUT_MS);
    if (got <= 0) {
        Serial.println("[net] WiFi timeout — serial-only mode");
        wifiThread.terminate();
        return false;
    }
    wifiThread.join();
    return g_wifiOk;
}

// ---------------------------------------------------------------------------
static const char* mgsLabel(MotifGrowthState s) {
    switch (s) {
        case MGS_IDLE:                return "idle";
        case MGS_DRIFTING:            return "drifting";
        case MGS_STABILIZING:         return "stabilizing";
        case MGS_READY_FOR_PROMOTION: return "ready";
        default:                      return "unknown";
    }
}

// ---------------------------------------------------------------------------
StatePublishingModule::StatePublishingModule()
    : _mqttReady(false),
      _lastPublishMs(0),
      _publishInterval(PUBLISH_FAST_MS),
      _wifiLastAttemptMs(0)
{}

// ---------------------------------------------------------------------------
bool StatePublishingModule::ensureMqtt() {
    // --- WiFi ---
    // SystemWiFiConnect() uses SSID/password from STSAFE (provisioned by
    // the IoT DevKit companion app or programmatically via EEPROMInterface).
    // We run it in a background thread so a missing/slow AP never hangs setup().
    if (!ensureWifi()) {
        return false;
    }
    Serial.printf("[net] WiFi connected: %s\n", SystemWiFiSSID());

    // --- MQTT ---
    if (g_mqtt.isConnected()) return true;

    // MQTTNetwork::connect() opens a TCPSocket via the mbed NSAPI.
    int tcpRc = g_net.connect(MQTT_BROKER_IP, MQTT_BROKER_PORT);
    if (tcpRc != 0) {
        Serial.printf("[net] TCP connect failed rc=%d\n", tcpRc);
        return false;
    }

    MQTTPacket_connectData opts = MQTTPacket_connectData_initializer;
    opts.clientID.cstring  = (char*)"mxchip-acoustic";
    opts.keepAliveInterval = 60;
    opts.cleansession      = 1;

    int rc = g_mqtt.connect(opts);
    if (rc != 0) {
        Serial.printf("[net] MQTT connect failed rc=%d\n", rc);
        return false;
    }
    Serial.println("[net] MQTT connected");
    return true;
}

void StatePublishingModule::begin() {
    _wifiLastAttemptMs = millis();
    _mqttReady = ensureMqtt();
    if (!_mqttReady) {
        Serial.println("[net] running in serial-only mode");
    }
}

// ---------------------------------------------------------------------------
void StatePublishingModule::publishJson(const char* topic, const char* json) {
    // Always echo to Serial for debugging / local monitoring.
    Serial.printf("[%s] %s\n", topic, json);

    if (!_mqttReady) {
        // Rate-limit reconnect attempts so the WiFi thread is not spawned on
        // every publish tick.  _wifiLastAttemptMs is set by begin() and here.
        uint32_t nowMs = millis();
        if (_wifiLastAttemptMs != 0 &&
            (nowMs - _wifiLastAttemptMs) < WIFI_RETRY_INTERVAL_MS) {
            return;  // too soon — stay in serial-only mode
        }
        _wifiLastAttemptMs = nowMs;
        _mqttReady = ensureMqtt();
        if (!_mqttReady) return;
    }

    MQTT::Message msg;
    msg.qos       = MQTT::QOS0;
    msg.retained  = false;
    msg.dup       = false;
    msg.payload   = (void*)json;
    msg.payloadlen = strlen(json);

    int rc = g_mqtt.publish(topic, msg);
    if (rc != 0) {
        Serial.printf("[net] publish failed rc=%d\n", rc);
        _mqttReady = false;   // Will reconnect on next attempt
    }
}

// ---------------------------------------------------------------------------
void StatePublishingModule::buildStatusJson(
    char* buf, size_t len,
    const ConfidenceSnapshot& snap,
    MotifGrowthState mgs,
    uint8_t vMajor, uint8_t vMinor) const
{
    snprintf(buf, len,
        "{\"v\":\"%u.%u\","
        "\"baseline_c\":%.3f,"
        "\"hum_c\":%.3f,"
        "\"broadband_c\":%.3f,"
        "\"transient_c\":%.3f,"
        "\"stability\":%.3f,"
        "\"motif_state\":\"%s\","
        "\"wb\":%d}",
        vMajor, vMinor,
        snap.baseline_confidence,
        snap.hum_confidence,
        snap.broadband_confidence,
        snap.transient_clarity,
        snap.composite_stability,
        mgsLabel(mgs),
        wellBeingLevel(snap.composite_stability, mgs));
}

// ---------------------------------------------------------------------------
void StatePublishingModule::maybePublish(
    const ConfidenceSnapshot& snap,
    MotifGrowthState          mgs,
    uint8_t vMajor, uint8_t vMinor,
    uint32_t nowMs)
{
    // Adapt cadence to stability.
    _publishInterval = (snap.composite_stability >= STABILITY_PUBLISH_THRESH)
                     ? PUBLISH_SLOW_MS
                     : PUBLISH_FAST_MS;

    if (_lastPublishMs != 0 &&
        (nowMs - _lastPublishMs) < _publishInterval) return;
    _lastPublishMs = nowMs;

    // Yield MQTT keepalive heartbeat before publishing.
    if (_mqttReady) g_mqtt.yield(5);

    char json[256];
    buildStatusJson(json, sizeof(json), snap, mgs, vMajor, vMinor);
    publishJson(TOPIC_STATUS, json);
}

// ---------------------------------------------------------------------------
void StatePublishingModule::publishMotifEvent(
    const PromotedMotif& motif,
    uint8_t vMajor, uint8_t vMinor,
    uint32_t totalConcepts)
{
    char json[220];
    snprintf(json, sizeof(json),
        "{\"event\":\"concept\","
        "\"v\":\"%u.%u\","
        "\"rms\":%.1f,"
        "\"hum\":%.1f,"
        "\"texture\":%.3f,"
        "\"transient\":%.2f,"
        "\"stability\":%u,"
        "\"total_concepts\":%lu}",
        vMajor, vMinor,
        motif.rms, motif.hum, motif.texture,
        motif.transient, motif.final_stability,
        (unsigned long)totalConcepts);
    publishJson(TOPIC_MOTIF, json);
}

// ---------------------------------------------------------------------------
int StatePublishingModule::wellBeingLevel(
    float compositeStability, MotifGrowthState mgs)
{
    if (compositeStability < 0.45f) return 0;   // unsettled
    if (mgs == MGS_DRIFTING || mgs == MGS_STABILIZING) return 1;  // learning
    return 2;  // stable
}
