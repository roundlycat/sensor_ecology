/*
 * env_node_01 — Sensor Ecology Agent Node
 * =========================================
 * Freenove ESP32-S3 WROOM
 * TCS34725 RGB/light sensor  + MPU-6050 accelerometer/gyroscope
 * I2C: SDA=GPIO8, SCL=GPIO9
 * MQTT broker: 192.168.0.25:1883
 *
 * Publishes interpreted observations only — never raw streams.
 * Buffers up to 10 messages offline and flushes on reconnect.
 *
 * Libraries (Arduino Library Manager):
 *   Adafruit TCS34725
 *   Adafruit MPU6050
 *   Adafruit Unified Sensor  (dependency of both above)
 *   PubSubClient  by Nick O'Leary
 *   ArduinoJson   by Benoit Blanchon
 */

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Adafruit_TCS34725.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <math.h>

// ── Credentials — edit these ──────────────────────────────────────────────────
#define WIFI_SSID  "YOUR_SSID"
#define WIFI_PASS  "YOUR_PASSWORD"

// ── Network ───────────────────────────────────────────────────────────────────
#define MQTT_HOST  "192.168.0.25"
#define MQTT_PORT  1883

// ── Agent identity ────────────────────────────────────────────────────────────
#define AGENT_ID       "env-node-01"
#define AGENT_LOCATION "lab"

// ── I2C pins (Freenove ESP32-S3) ──────────────────────────────────────────────
#define PIN_SDA  8
#define PIN_SCL  9

// ── MQTT topics ───────────────────────────────────────────────────────────────
#define TOPIC_REGISTER  "agents/registration"
#define TOPIC_INTERP    "agents/" AGENT_ID "/interpretation"
#define TOPIC_STATUS    "agents/" AGENT_ID "/status"

// ── Timing (ms) ───────────────────────────────────────────────────────────────
#define INTERVAL_MPU_SAMPLE_MS      20      //  50 Hz
#define INTERVAL_LIGHT_MS        10000      //  10 s
#define INTERVAL_HEARTBEAT_MS    30000      //  30 s
#define INTERVAL_SILENCE_MS      60000      //  re-publish even if unchanged
#define INTERVAL_MQTT_RETRY_MS    5000      //  reconnect backoff
#define INTERVAL_BUFFER_FLUSH_MS   100      //  drain 1 buffered msg per 100 ms

// ── MPU window ────────────────────────────────────────────────────────────────
#define MPU_WINDOW  50   // samples at 50 Hz = 1 second

// ── Motion thresholds (RMS g) ─────────────────────────────────────────────────
#define TH_IDLE_RMS_MAX       0.020f
#define TH_IMPACT_PEAK        0.300f
//                             RMS min    RMS max    ZCR/s min  ZCR/s max
#define TH_TYPING_RMS_MIN     0.020f
#define TH_TYPING_RMS_MAX     0.080f
#define TH_TYPING_ZCR_MIN     10
#define TH_TYPING_ZCR_MAX     30
#define TH_FOOT_RMS_MIN       0.040f
#define TH_FOOT_RMS_MAX       0.200f
#define TH_FOOT_ZCR_MIN       2
#define TH_FOOT_ZCR_MAX       8
#define TH_FOOT_PEAK_MIN      0.100f
#define TH_EQUIP_RMS_MIN      0.050f
#define TH_EQUIP_RMS_MAX      0.150f
#define TH_EQUIP_ZCR_MIN      20

// ── Light thresholds ──────────────────────────────────────────────────────────
#define TH_LUX_DARK           10.0f
#define TH_LUX_DAYLIGHT      200.0f
#define TH_LUX_MEDIUM         50.0f
#define TH_CCT_WARM          4000      // K — incandescent/warm
#define TH_CCT_NEUTRAL       5500      // K — transition to daylight/overcast
#define TH_CCT_SCREEN        6500      // K — blue-dominant / screen
#define TH_RRATIO_SCREEN     0.20f     // r/c ratio below this = screen-dominant

// ── Observation labels ────────────────────────────────────────────────────────
// Motion
static const char OBS_IDLE[]   PROGMEM = "idle";
static const char OBS_TYPING[] PROGMEM = "typing";
static const char OBS_FOOT[]   PROGMEM = "footsteps";
static const char OBS_IMPACT[] PROGMEM = "impact";
static const char OBS_EQUIP[]  PROGMEM = "equipment_running";
// Light
static const char OBS_DARK[]   PROGMEM = "dark";
static const char OBS_DIM[]    PROGMEM = "dim_warm";
static const char OBS_DAY[]    PROGMEM = "daylight";
static const char OBS_OCAST[]  PROGMEM = "overcast";
static const char OBS_SCREEN[] PROGMEM = "screen_dominant";
static const char OBS_AWARM[]  PROGMEM = "artificial_warm";

// ── Offline ring buffer ───────────────────────────────────────────────────────
#define RING_SIZE  10

struct BufferedMsg {
  char topic[64];
  char payload[400];
};

struct RingBuffer {
  BufferedMsg slots[RING_SIZE];
  int head  = 0;
  int count = 0;

  void push(const char* topic, const char* payload) {
    int idx = (head + count) % RING_SIZE;
    if (count < RING_SIZE) {
      count++;
    } else {
      // Full: drop oldest, advance head
      head = (head + 1) % RING_SIZE;
    }
    strlcpy(slots[idx].topic,   topic,   sizeof(slots[idx].topic));
    strlcpy(slots[idx].payload, payload, sizeof(slots[idx].payload));
  }

  bool pop(BufferedMsg& out) {
    if (count == 0) return false;
    out  = slots[head];
    head = (head + 1) % RING_SIZE;
    count--;
    return true;
  }
};

// ── Globals ───────────────────────────────────────────────────────────────────
WiFiClient        wifiClient;
PubSubClient      mqtt(wifiClient);

Adafruit_TCS34725 tcs(TCS34725_INTEGRATIONTIME_101MS, TCS34725_GAIN_1X);
Adafruit_MPU6050  mpu;

RingBuffer        offlineBuf;

bool tcsOk = false;
bool mpuOk = false;

// MPU 1-second tumbling window
float  mpuBuf[MPU_WINDOW];
int    mpuIdx     = 0;
bool   mpuFull    = false;
bool   impactSent = false;   // latch: suppress re-reporting until back to idle

// Last published state (for change detection)
char lastMotionObs[32] = "";
char lastLightObs[32]  = "";

// Timestamps
unsigned long tLastMpuSample  = 0;
unsigned long tLastLight       = 0;
unsigned long tLastHeartbeat   = 0;
unsigned long tLastMotionPub   = 0;
unsigned long tLastLightPub    = 0;
unsigned long tLastMqttRetry   = 0;
unsigned long tLastFlush       = 0;

bool registered = false;


// ═══════════════════════════════════════════════════════════════════════════════
// MQTT helpers
// ═══════════════════════════════════════════════════════════════════════════════

// Publish or buffer a message.
static void mqttSend(const char* topic, const char* payload) {
  if (mqtt.connected()) {
    mqtt.publish(topic, payload);
    Serial.printf("[MQTT→] %s : %s\n", topic, payload);
  } else {
    offlineBuf.push(topic, payload);
    Serial.printf("[BUF+] %s\n", payload);
  }
}

// Build an interpretation message and send/buffer it.
static void publishInterp(
    const char* sensor,
    const char* observation,
    float       confidence,
    const char* rawKey1, float rawVal1,
    const char* rawKey2 = nullptr, float rawVal2 = 0.0f)
{
  StaticJsonDocument<320> doc;
  doc["agent_id"]    = AGENT_ID;
  doc["sensor"]      = sensor;
  doc["observation"] = observation;
  doc["confidence"]  = (float)((int)(confidence * 100 + 0.5f)) / 100.0f;
  doc["timestamp_ms"] = millis();

  JsonObject raw = doc.createNestedObject("raw");
  // Round to 4 decimal places for readability
  raw[rawKey1] = (float)((int)(rawVal1 * 10000 + 0.5f)) / 10000.0f;
  if (rawKey2) {
    raw[rawKey2] = (float)((int)(rawVal2 * 10000 + 0.5f)) / 10000.0f;
  }

  char buf[320];
  serializeJson(doc, buf, sizeof(buf));
  mqttSend(TOPIC_INTERP, buf);
}


// ═══════════════════════════════════════════════════════════════════════════════
// WiFi & MQTT connection management (non-blocking)
// ═══════════════════════════════════════════════════════════════════════════════

static void wifiMaintain() {
  if (WiFi.status() == WL_CONNECTED) return;
  // WiFi.begin() is non-blocking; the stack handles the rest
  Serial.printf("[WiFi] Reconnecting to %s…\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

static void publishRegistration() {
  StaticJsonDocument<200> doc;
  doc["agent_id"]   = AGENT_ID;
  doc["agent_type"] = "environmental";
  doc["location"]   = AGENT_LOCATION;
  JsonArray caps = doc.createNestedArray("capabilities");
  caps.add("vibration");
  caps.add("light_quality");
  caps.add("motion");

  char buf[200];
  serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(TOPIC_REGISTER, buf, /*retain=*/true);
  Serial.println("[MQTT] Registration published");
}

static void mqttMaintain() {
  if (mqtt.connected()) {
    mqtt.loop();
    return;
  }
  if (WiFi.status() != WL_CONNECTED) return;

  unsigned long now = millis();
  if (now - tLastMqttRetry < INTERVAL_MQTT_RETRY_MS) return;
  tLastMqttRetry = now;

  Serial.printf("[MQTT] Connecting to %s:%d…\n", MQTT_HOST, MQTT_PORT);
  if (mqtt.connect(AGENT_ID)) {
    Serial.println("[MQTT] Connected");
    if (!registered) {
      publishRegistration();
      registered = true;
    }
  } else {
    Serial.printf("[MQTT] Failed rc=%d, retry in %ds\n",
                  mqtt.state(), INTERVAL_MQTT_RETRY_MS / 1000);
  }
}

// Drain one buffered message per call (rate-limited to INTERVAL_BUFFER_FLUSH_MS).
static void flushBuffer() {
  if (!mqtt.connected() || offlineBuf.count == 0) return;
  unsigned long now = millis();
  if (now - tLastFlush < INTERVAL_BUFFER_FLUSH_MS) return;
  tLastFlush = now;

  BufferedMsg msg;
  if (offlineBuf.pop(msg)) {
    mqtt.publish(msg.topic, msg.payload);
    Serial.printf("[FLUSH] %s\n", msg.payload);
  }
}


// ═══════════════════════════════════════════════════════════════════════════════
// MPU-6050 — motion classification
// ═══════════════════════════════════════════════════════════════════════════════

// Count zero-crossings of the AC component (buf[i] - mean) per second.
// Each crossing ≈ half a cycle, so ZCR/2 ≈ Hz.
static int zeroCrossings(const float* buf, int n, float mean) {
  int count = 0;
  int prevSign = 0;
  for (int i = 0; i < n; i++) {
    int s = (buf[i] >= mean) ? 1 : -1;
    if (prevSign != 0 && s != prevSign) count++;
    prevSign = s;
  }
  return count;
}

// Confidence helper: 1.0 at centre of [lo,hi], decaying to 0 at edges.
static float rangeFit(float val, float lo, float hi) {
  float centre = (lo + hi) * 0.5f;
  float half   = (hi - lo) * 0.5f;
  if (half <= 0.0f) return 0.0f;
  return fmaxf(0.0f, 1.0f - fabsf(val - centre) / half);
}

static void classifyMotion() {
  // Compute mean (= gravity + DC bias component)
  float sum = 0.0f;
  float peak = 0.0f;
  for (int i = 0; i < MPU_WINDOW; i++) {
    sum += mpuBuf[i];
    if (mpuBuf[i] > peak) peak = mpuBuf[i];
  }
  float mean = sum / MPU_WINDOW;

  // RMS of AC component (vibration only, gravity cancelled)
  float rmsAcc = 0.0f;
  for (int i = 0; i < MPU_WINDOW; i++) {
    float v = mpuBuf[i] - mean;
    rmsAcc += v * v;
  }
  float rms = sqrtf(rmsAcc / MPU_WINDOW);

  // Zero-crossing rate (proxy for dominant frequency)
  int zcr = zeroCrossings(mpuBuf, MPU_WINDOW, mean);

  Serial.printf("[MPU] rms=%.4fg peak=%.4fg zcr=%d/s\n", rms, peak, zcr);

  // ── Classify ──────────────────────────────────────────────────────────────
  const char* obs  = OBS_IDLE;
  float       conf = 0.5f;

  if (rms < TH_IDLE_RMS_MAX) {
    obs  = OBS_IDLE;
    conf = 1.0f - (rms / TH_IDLE_RMS_MAX);   // 1.0 at dead-still
    impactSent = false;                        // clear latch on return to idle

  } else if (rms >= TH_EQUIP_RMS_MIN && rms <= TH_EQUIP_RMS_MAX
             && zcr >= TH_EQUIP_ZCR_MIN) {
    // High-frequency sustained vibration → motor / equipment
    obs  = OBS_EQUIP;
    float rmsScore = rangeFit(rms, TH_EQUIP_RMS_MIN, TH_EQUIP_RMS_MAX);
    float zcrScore = fminf(1.0f, (float)(zcr - TH_EQUIP_ZCR_MIN) / 40.0f);
    conf = fmaxf(0.45f, (rmsScore + zcrScore) * 0.5f);

  } else if (rms >= TH_TYPING_RMS_MIN && rms <= TH_TYPING_RMS_MAX
             && zcr >= TH_TYPING_ZCR_MIN && zcr <= TH_TYPING_ZCR_MAX) {
    // Low sustained vibration in keyboard-frequency band
    obs  = OBS_TYPING;
    conf = fmaxf(0.4f, (rangeFit(rms, TH_TYPING_RMS_MIN, TH_TYPING_RMS_MAX)
                        + rangeFit((float)zcr, TH_TYPING_ZCR_MIN, TH_TYPING_ZCR_MAX)) * 0.5f);

  } else if (rms >= TH_FOOT_RMS_MIN && rms <= TH_FOOT_RMS_MAX
             && zcr >= TH_FOOT_ZCR_MIN && zcr <= TH_FOOT_ZCR_MAX
             && (peak - mean) >= TH_FOOT_PEAK_MIN) {
    // Periodic low-frequency spikes → footsteps
    obs  = OBS_FOOTSTEPS;
    conf = fmaxf(0.5f, rangeFit(rms, TH_FOOT_RMS_MIN, TH_FOOT_RMS_MAX));

  } else {
    // Doesn't fit any known pattern cleanly
    obs  = OBS_IDLE;
    conf = 0.35f;
  }

  // ── Publish on state change or silence timeout ────────────────────────────
  bool changed = (strcmp(obs, lastMotionObs) != 0);
  bool stale   = (millis() - tLastMotionPub) > INTERVAL_SILENCE_MS;

  if (changed || stale) {
    publishInterp("mpu6050", obs, conf, "rms_g", rms, "peak_g", peak - mean);
    strlcpy(lastMotionObs, obs, sizeof(lastMotionObs));
    tLastMotionPub = millis();
  }
}

static void sampleMpu() {
  if (!mpuOk) return;
  unsigned long now = millis();
  if (now - tLastMpuSample < INTERVAL_MPU_SAMPLE_MS) return;
  tLastMpuSample = now;

  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);

  const float kG = 9.80665f;
  float ax = a.acceleration.x / kG;
  float ay = a.acceleration.y / kG;
  float az = a.acceleration.z / kG;
  float mag = sqrtf(ax*ax + ay*ay + az*az);   // total magnitude in g

  // ── Per-sample impact detection ───────────────────────────────────────────
  // Dynamic component: deviation from 1 g (total static gravity magnitude).
  // Works regardless of board orientation since |gravity| = 1 g always.
  float acDyn = fabsf(mag - 1.0f);
  if (acDyn >= TH_IMPACT_PEAK && !impactSent) {
    float conf = fminf(1.0f, 0.65f + (acDyn - TH_IMPACT_PEAK) / 0.4f);
    publishInterp("mpu6050", OBS_IMPACT, conf, "peak_g", acDyn);
    strlcpy(lastMotionObs, OBS_IMPACT, sizeof(lastMotionObs));
    tLastMotionPub = millis();
    impactSent     = true;
    // Reset window so post-impact classification starts clean
    mpuIdx  = 0;
    mpuFull = false;
    return;
  }

  // ── Accumulate into window ────────────────────────────────────────────────
  mpuBuf[mpuIdx++] = mag;
  if (mpuIdx >= MPU_WINDOW) {
    mpuIdx  = 0;
    mpuFull = true;
    classifyMotion();   // evaluate every full 1-second window
  }
}


// ═══════════════════════════════════════════════════════════════════════════════
// TCS34725 — light classification
// ═══════════════════════════════════════════════════════════════════════════════

static void classifyLight() {
  if (!tcsOk) return;

  uint16_t r, g, b, c;
  tcs.getRawData(&r, &g, &b, &c);

  if (c == 0) {
    Serial.println("[TCS] clear=0, skip");
    return;
  }

  // calculateColorTemperature_dn40 is the accurate DN40 illuminant method.
  // The simpler calculateColorTemperature() can drift badly with tinted sources.
  uint16_t cct = tcs.calculateColorTemperature_dn40(r, g, b, c);
  uint16_t lux = tcs.calculateLux(r, g, b);
  float    luxF = (float)lux;

  // Red ratio: how much of the clear channel is red.
  // Screen glow has suppressed red (monitors skew green/blue).
  float rRatio = (float)r / (float)c;

  Serial.printf("[TCS] cct=%uK lux=%u rRatio=%.3f\n", cct, lux, rRatio);

  // ── Classify ──────────────────────────────────────────────────────────────
  const char* obs  = OBS_DARK;
  float       conf = 0.75f;

  if (luxF < TH_LUX_DARK) {
    obs  = OBS_DARK;
    conf = 1.0f - (luxF / TH_LUX_DARK) * 0.2f;

  } else if (luxF >= TH_LUX_DAYLIGHT && cct >= TH_CCT_NEUTRAL) {
    // Bright and cool → direct or window daylight
    obs  = OBS_DAY;
    conf = fminf(1.0f, 0.6f + (luxF - TH_LUX_DAYLIGHT) / 800.0f);

  } else if (luxF >= TH_LUX_MEDIUM && cct >= TH_CCT_NEUTRAL && cct < TH_CCT_SCREEN) {
    // Moderate lux, neutral CCT → overcast sky through glass, or cool-white LED
    obs  = OBS_OCAST;
    conf = 0.70f;

  } else if (rRatio < TH_RRATIO_SCREEN && luxF >= TH_LUX_MEDIUM) {
    // Blue-shifted, suppressed red → monitor or screen glow
    obs  = OBS_SCREEN;
    conf = fmaxf(0.55f, (TH_RRATIO_SCREEN - rRatio) / TH_RRATIO_SCREEN);

  } else if (cct <= TH_CCT_WARM && luxF >= TH_LUX_DAYLIGHT) {
    // Warm and bright → incandescent / warm LED overhead
    obs  = OBS_AWARM;
    conf = fmaxf(0.6f, (float)(TH_CCT_WARM - cct) / (float)TH_CCT_WARM);

  } else if (cct <= TH_CCT_WARM) {
    // Warm but dim → lamp, candle, dawn/dusk
    obs  = OBS_DIM;
    conf = 0.70f;

  } else {
    // Medium lux, intermediate CCT — call it artificial warm by default
    obs  = OBS_AWARM;
    conf = 0.50f;
  }

  // ── Publish on state change or silence timeout ────────────────────────────
  bool changed = (strcmp(obs, lastLightObs) != 0);
  bool stale   = (millis() - tLastLightPub) > INTERVAL_SILENCE_MS;

  if (changed || stale) {
    publishInterp("tcs34725", obs, conf, "lux", luxF, "cct_k", (float)cct);
    strlcpy(lastLightObs, obs, sizeof(lastLightObs));
    tLastLightPub = millis();
  }
}

static void sampleLight() {
  if (!tcsOk) return;
  unsigned long now = millis();
  if (now - tLastLight < INTERVAL_LIGHT_MS) return;
  tLastLight = now;
  classifyLight();
}


// ═══════════════════════════════════════════════════════════════════════════════
// Heartbeat
// ═══════════════════════════════════════════════════════════════════════════════

static void heartbeat() {
  unsigned long now = millis();
  if (now - tLastHeartbeat < INTERVAL_HEARTBEAT_MS) return;
  tLastHeartbeat = now;

  if (!mqtt.connected()) return;

  StaticJsonDocument<160> doc;
  doc["agent_id"]       = AGENT_ID;
  doc["uptime_ms"]      = now;
  doc["rssi_dbm"]       = WiFi.RSSI();
  doc["buf_pending"]    = offlineBuf.count;
  doc["sensors_ok"]     = (tcsOk ? "tcs " : "") + String(mpuOk ? "mpu" : "");

  char buf[160];
  serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(TOPIC_STATUS, buf);
  Serial.printf("[HB] uptime=%lus rssi=%ddBm buf=%d\n",
                now / 1000, WiFi.RSSI(), offlineBuf.count);
}


// ═══════════════════════════════════════════════════════════════════════════════
// setup / loop
// ═══════════════════════════════════════════════════════════════════════════════

void setup() {
  Serial.begin(115200);
  delay(200);   // let USB serial settle before first print
  Serial.println("\n[BOOT] env-node-01 starting");

  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);   // 400 kHz fast-mode

  // TCS34725 ──────────────────────────────────────────────────────────────────
  if (tcs.begin(TCS34725_ADDRESS, &Wire)) {
    tcsOk = true;
    Serial.println("[BOOT] TCS34725 OK");
  } else {
    Serial.println("[BOOT] TCS34725 NOT FOUND — light disabled");
  }

  // MPU-6050 ──────────────────────────────────────────────────────────────────
  if (mpu.begin(MPU6050_I2CADDR_DEFAULT, &Wire)) {
    mpuOk = true;
    mpu.setAccelerometerRange(MPU6050_RANGE_2_G);   // ±2g, best resolution
    mpu.setGyroRange(MPU6050_RANGE_250_DEG);
    mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);     // low-pass, suits 50 Hz sampling
    Serial.println("[BOOT] MPU-6050 OK");
  } else {
    Serial.println("[BOOT] MPU-6050 NOT FOUND — motion disabled");
  }

  // MQTT / WiFi ───────────────────────────────────────────────────────────────
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(20);
  mqtt.setBufferSize(512);   // default 256 is too small for our payloads

  WiFi.setAutoReconnect(true);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("[BOOT] WiFi connecting to %s…\n", WIFI_SSID);

  Serial.println("[BOOT] Setup complete — entering loop");
}

void loop() {
  // Connection maintenance (both non-blocking)
  wifiMaintain();
  mqttMaintain();

  // Drain one buffered message per flush interval
  flushBuffer();

  // Sensor sampling
  sampleMpu();
  sampleLight();

  // 30-second heartbeat
  heartbeat();
}
