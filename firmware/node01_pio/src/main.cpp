/*
 * env_node_01 — Sensor Ecology Agent Node
 * =========================================
 * Freenove ESP32-S3 WROOM
 * TCS34725 RGB/light sensor  + MPU-6050 accelerometer/gyroscope
 * I2C: SDA=GPIO8, SCL=GPIO9
 * MQTT broker: 192.168.0.25:1883
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

// ── Credentials ───────────────────────────────────────────────────────────────
#define WIFI_SSID  "xraycanard"
#define WIFI_PASS  "gaqsob-jezgy2-maknuP"

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
#define INTERVAL_MPU_SAMPLE_MS      20
#define INTERVAL_LIGHT_MS        10000
#define INTERVAL_HEARTBEAT_MS    30000
#define INTERVAL_SILENCE_MS      60000
#define INTERVAL_MQTT_RETRY_MS    5000
#define INTERVAL_BUFFER_FLUSH_MS   100

// ── MPU window ────────────────────────────────────────────────────────────────
#define MPU_WINDOW  50   // samples at 50 Hz = 1 second

// ── Motion thresholds (RMS g) ─────────────────────────────────────────────────
#define TH_IDLE_RMS_MAX       0.020f
#define TH_IMPACT_PEAK        0.300f
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
#define TH_CCT_WARM          4000
#define TH_CCT_NEUTRAL       5500
#define TH_CCT_SCREEN        6500
#define TH_RRATIO_SCREEN     0.20f

// ── Observation labels ────────────────────────────────────────────────────────
static const char OBS_IDLE[]   PROGMEM = "idle";
static const char OBS_TYPING[] PROGMEM = "typing";
static const char OBS_FOOT[]   PROGMEM = "footsteps";
static const char OBS_IMPACT[] PROGMEM = "impact";
static const char OBS_EQUIP[]  PROGMEM = "equipment_running";
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

float  mpuBuf[MPU_WINDOW];
int    mpuIdx     = 0;
bool   mpuFull    = false;
bool   impactSent = false;

char lastMotionObs[32] = "";
char lastLightObs[32]  = "";

unsigned long tLastMpuSample  = 0;
unsigned long tLastLight       = 0;
unsigned long tLastHeartbeat   = 0;
unsigned long tLastMotionPub   = 0;
unsigned long tLastLightPub    = 0;
unsigned long tLastMqttRetry   = 0;
unsigned long tLastFlush       = 0;

bool registered = false;


// ── MQTT helpers ──────────────────────────────────────────────────────────────

static void mqttSend(const char* topic, const char* payload) {
  if (mqtt.connected()) {
    mqtt.publish(topic, payload);
    Serial.printf("[MQTT→] %s : %s\n", topic, payload);
  } else {
    offlineBuf.push(topic, payload);
    Serial.printf("[BUF+] %s\n", payload);
  }
}

static const char* sensorDomain(const char* sensor) {
  if (strcmp(sensor, "mpu6050")  == 0) return "embodied_state";
  if (strcmp(sensor, "tcs34725") == 0) return "environmental_field";
  return "unknown";
}

static void publishInterp(
    const char* sensor,
    const char* observation,
    float       confidence,
    const char* rawKey1, float rawVal1,
    const char* rawKey2 = nullptr, float rawVal2 = 0.0f)
{
  JsonDocument doc;
  doc["agent_id"]     = AGENT_ID;
  doc["sensor"]       = sensor;
  doc["domain"]       = sensorDomain(sensor);
  doc["observation"]  = observation;
  doc["confidence"]   = (float)((int)(confidence * 100 + 0.5f)) / 100.0f;
  doc["timestamp_ms"] = millis();

  JsonObject raw = doc["raw"].to<JsonObject>();
  raw[rawKey1] = (float)((int)(rawVal1 * 10000 + 0.5f)) / 10000.0f;
  if (rawKey2) {
    raw[rawKey2] = (float)((int)(rawVal2 * 10000 + 0.5f)) / 10000.0f;
  }

  char buf[320];
  serializeJson(doc, buf, sizeof(buf));
  mqttSend(TOPIC_INTERP, buf);
}


// ── WiFi & MQTT connection management ────────────────────────────────────────

static unsigned long tLastWifiRetry = 0;

static void wifiMaintain() {
  if (WiFi.status() == WL_CONNECTED) return;
  unsigned long now = millis();
  if (now - tLastWifiRetry < 10000) return;   // only retry every 10 s
  tLastWifiRetry = now;
  Serial.printf("[WiFi] Reconnecting to %s...\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

static void publishRegistration() {
  JsonDocument doc;
  doc["agent_id"]   = AGENT_ID;
  doc["agent_type"] = "environmental";
  doc["location"]   = AGENT_LOCATION;
  JsonArray caps = doc["capabilities"].to<JsonArray>();
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

  Serial.printf("[MQTT] Connecting to %s:%d...\n", MQTT_HOST, MQTT_PORT);
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


// ── MPU-6050 — motion classification ─────────────────────────────────────────

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

static float rangeFit(float val, float lo, float hi) {
  float centre = (lo + hi) * 0.5f;
  float half   = (hi - lo) * 0.5f;
  if (half <= 0.0f) return 0.0f;
  return fmaxf(0.0f, 1.0f - fabsf(val - centre) / half);
}

static void classifyMotion() {
  float sum = 0.0f;
  float peak = 0.0f;
  for (int i = 0; i < MPU_WINDOW; i++) {
    sum += mpuBuf[i];
    if (mpuBuf[i] > peak) peak = mpuBuf[i];
  }
  float mean = sum / MPU_WINDOW;

  float rmsAcc = 0.0f;
  for (int i = 0; i < MPU_WINDOW; i++) {
    float v = mpuBuf[i] - mean;
    rmsAcc += v * v;
  }
  float rms = sqrtf(rmsAcc / MPU_WINDOW);

  int zcr = zeroCrossings(mpuBuf, MPU_WINDOW, mean);

  Serial.printf("[MPU] rms=%.4fg peak=%.4fg zcr=%d/s\n", rms, peak, zcr);

  const char* obs  = OBS_IDLE;
  float       conf = 0.5f;

  if (rms < TH_IDLE_RMS_MAX) {
    obs  = OBS_IDLE;
    conf = 1.0f - (rms / TH_IDLE_RMS_MAX);
    impactSent = false;

  } else if (rms >= TH_EQUIP_RMS_MIN && rms <= TH_EQUIP_RMS_MAX
             && zcr >= TH_EQUIP_ZCR_MIN) {
    obs  = OBS_EQUIP;
    float rmsScore = rangeFit(rms, TH_EQUIP_RMS_MIN, TH_EQUIP_RMS_MAX);
    float zcrScore = fminf(1.0f, (float)(zcr - TH_EQUIP_ZCR_MIN) / 40.0f);
    conf = fmaxf(0.45f, (rmsScore + zcrScore) * 0.5f);

  } else if (rms >= TH_TYPING_RMS_MIN && rms <= TH_TYPING_RMS_MAX
             && zcr >= TH_TYPING_ZCR_MIN && zcr <= TH_TYPING_ZCR_MAX) {
    obs  = OBS_TYPING;
    conf = fmaxf(0.4f, (rangeFit(rms, TH_TYPING_RMS_MIN, TH_TYPING_RMS_MAX)
                        + rangeFit((float)zcr, TH_TYPING_ZCR_MIN, TH_TYPING_ZCR_MAX)) * 0.5f);

  } else if (rms >= TH_FOOT_RMS_MIN && rms <= TH_FOOT_RMS_MAX
             && zcr >= TH_FOOT_ZCR_MIN && zcr <= TH_FOOT_ZCR_MAX
             && (peak - mean) >= TH_FOOT_PEAK_MIN) {
    obs  = OBS_FOOT;   // fix: was OBS_FOOTSTEPS (undefined)
    conf = fmaxf(0.5f, rangeFit(rms, TH_FOOT_RMS_MIN, TH_FOOT_RMS_MAX));

  } else {
    obs  = OBS_IDLE;
    conf = 0.35f;
  }

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
  float mag = sqrtf(ax*ax + ay*ay + az*az);

  float acDyn = fabsf(mag - 1.0f);
  if (acDyn >= TH_IMPACT_PEAK && !impactSent) {
    float conf = fminf(1.0f, 0.65f + (acDyn - TH_IMPACT_PEAK) / 0.4f);
    publishInterp("mpu6050", OBS_IMPACT, conf, "peak_g", acDyn);
    strlcpy(lastMotionObs, OBS_IMPACT, sizeof(lastMotionObs));
    tLastMotionPub = millis();
    impactSent     = true;
    mpuIdx  = 0;
    mpuFull = false;
    return;
  }

  mpuBuf[mpuIdx++] = mag;
  if (mpuIdx >= MPU_WINDOW) {
    mpuIdx  = 0;
    mpuFull = true;
    classifyMotion();
  }
}


// ── TCS34725 — light classification ──────────────────────────────────────────

static void classifyLight() {
  if (!tcsOk) return;

  uint16_t r, g, b, c;
  tcs.getRawData(&r, &g, &b, &c);

  if (c == 0) {
    Serial.println("[TCS] clear=0, skip");
    return;
  }

  uint16_t cct = tcs.calculateColorTemperature_dn40(r, g, b, c);
  uint16_t lux = tcs.calculateLux(r, g, b);
  float    luxF = (float)lux;
  float rRatio = (float)r / (float)c;

  Serial.printf("[TCS] cct=%uK lux=%u rRatio=%.3f\n", cct, lux, rRatio);

  const char* obs  = OBS_DARK;
  float       conf = 0.75f;

  if (luxF < TH_LUX_DARK) {
    obs  = OBS_DARK;
    conf = 1.0f - (luxF / TH_LUX_DARK) * 0.2f;

  } else if (luxF >= TH_LUX_DAYLIGHT && cct >= TH_CCT_NEUTRAL) {
    obs  = OBS_DAY;
    conf = fminf(1.0f, 0.6f + (luxF - TH_LUX_DAYLIGHT) / 800.0f);

  } else if (luxF >= TH_LUX_MEDIUM && cct >= TH_CCT_NEUTRAL && cct < TH_CCT_SCREEN) {
    obs  = OBS_OCAST;
    conf = 0.70f;

  } else if (rRatio < TH_RRATIO_SCREEN && luxF >= TH_LUX_MEDIUM) {
    obs  = OBS_SCREEN;
    conf = fmaxf(0.55f, (TH_RRATIO_SCREEN - rRatio) / TH_RRATIO_SCREEN);

  } else if (cct <= TH_CCT_WARM && luxF >= TH_LUX_DAYLIGHT) {
    obs  = OBS_AWARM;
    conf = fmaxf(0.6f, (float)(TH_CCT_WARM - cct) / (float)TH_CCT_WARM);

  } else if (cct <= TH_CCT_WARM) {
    obs  = OBS_DIM;
    conf = 0.70f;

  } else {
    obs  = OBS_AWARM;
    conf = 0.50f;
  }

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


// ── Heartbeat ─────────────────────────────────────────────────────────────────

static void heartbeat() {
  unsigned long now = millis();
  if (now - tLastHeartbeat < INTERVAL_HEARTBEAT_MS) return;
  tLastHeartbeat = now;
  if (!mqtt.connected()) return;

  JsonDocument doc;
  doc["agent_id"]    = AGENT_ID;
  doc["uptime_ms"]   = now;
  doc["rssi_dbm"]    = WiFi.RSSI();
  doc["buf_pending"] = offlineBuf.count;
  doc["sensors_ok"]  = (tcsOk ? "tcs " : "") + String(mpuOk ? "mpu" : "");

  char buf[160];
  serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(TOPIC_STATUS, buf);
  Serial.printf("[HB] uptime=%lus rssi=%ddBm buf=%d\n",
                now / 1000, WiFi.RSSI(), offlineBuf.count);
}


// ── setup / loop ──────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[BOOT] env-node-01 starting");

  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);
  delay(50);  // allow I2C bus to settle before probing sensors

  for (int attempt = 1; attempt <= 3 && !tcsOk; attempt++) {
    if (tcs.begin(TCS34725_ADDRESS, &Wire)) {
      tcsOk = true;
      Serial.println("[BOOT] TCS34725 OK");
    } else {
      Serial.printf("[BOOT] TCS34725 attempt %d/3 failed\n", attempt);
      delay(50);
    }
  }
  if (!tcsOk) Serial.println("[BOOT] TCS34725 NOT FOUND — light disabled");

  for (int attempt = 1; attempt <= 3 && !mpuOk; attempt++) {
    if (mpu.begin(MPU6050_I2CADDR_DEFAULT, &Wire)) {
      mpuOk = true;
      mpu.setAccelerometerRange(MPU6050_RANGE_2_G);
      mpu.setGyroRange(MPU6050_RANGE_250_DEG);
      mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
      Serial.println("[BOOT] MPU-6050 OK");
    } else {
      Serial.printf("[BOOT] MPU-6050 attempt %d/3 failed\n", attempt);
      delay(50);
    }
  }
  if (!mpuOk) Serial.println("[BOOT] MPU-6050 NOT FOUND — motion disabled");

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(20);
  mqtt.setBufferSize(512);

  WiFi.setAutoReconnect(true);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("[BOOT] WiFi connecting to %s...\n", WIFI_SSID);

  Serial.println("[BOOT] Setup complete — entering loop");
}

void loop() {
  wifiMaintain();
  mqttMaintain();
  flushBuffer();
  sampleMpu();
  sampleLight();
  heartbeat();
}
