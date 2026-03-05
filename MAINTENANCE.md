# Sensor Ecology — Maintenance Checklist

---

## Nodes at a Glance

| Node | Hardware | USB chip | Port | Firmware |
|---|---|---|---|---|
| **env-node-01** | Freenove ESP32-S3 WROOM | Native CDC | `/dev/ttyACM0` | `firmware/node01_pio/` |
| **bme280-328564** | NodeMCU ESP8266 | CP2102 | `/dev/ttyUSB0` | `nodemcu_bme280/` |
| **rfid-32bd8d** | NodeMCU ESP8266 | CP2102 | `/dev/ttyUSB0` | `nodemcu_rfid/` |
| **pi5-thermal** | Raspberry Pi (thermal cam) | — | network | `thermal_publisher.py` |
| **pi5-main** | Raspberry Pi 5 | — | — | (host machine) |

**Sensors on env-node-01:** TCS34725 light (SDA→GPIO8, SCL→GPIO9) · MPU-6050 accel (same I2C bus)
**Sensor on bme280:** BME280 temp/humidity/pressure (SDA→D2, SCL→D1) at I2C 0x77
**WiFi SSID:** xraycanard · **MQTT broker:** 192.168.0.25:1883

---

## 1 — Check Everything is Running

```bash
# All 6 services should show "active (running)"
systemctl status mosquitto postgresql@16-main \
  sensor-ingestion esp32-bridge mqtt-esp32-bridge \
  mqtt-kanban-bridge sensor-dashboard thermal-publisher
```

Quick one-liner — should print 8 lines all saying `active`:
```bash
systemctl is-active mosquitto postgresql@16-main sensor-ingestion \
  esp32-bridge mqtt-esp32-bridge mqtt-kanban-bridge \
  sensor-dashboard thermal-publisher
```

---

## 2 — Check Nodes are Sending Data

```bash
# Shows each node, its last event, and total event count
psql sensor_ecology -c "
SELECT an.node_name, an.last_heartbeat_at,
       COUNT(pe.id) AS events_total,
       MAX(pe.event_start) AS last_event
FROM agent_nodes an
LEFT JOIN perceptual_events pe ON pe.agent_node_id = an.id
GROUP BY an.id, an.node_name, an.last_heartbeat_at
ORDER BY last_event DESC NULLS LAST;"
```

**Healthy:** `last_event` within the last few minutes for pi5-main and pi5-thermal.
**Stale:** Any node with `last_event` more than ~1 hour old needs attention.

Check live MQTT traffic (5 seconds):
```bash
mosquitto_sub -h localhost -t "agents/#" -t "thermal/#" -W 5
```
You should see thermal frames from pi5-thermal and interpretation messages from env-node-01.

---

## 3 — Restart a Service

```bash
sudo systemctl restart <service-name>
```

| Problem | Service to restart |
|---|---|
| No events in dashboard | `sensor-ingestion` |
| ESP32/NodeMCU data not reaching DB | `mqtt-esp32-bridge` |
| Dashboard not loading | `sensor-dashboard` |
| Thermal frames missing | `thermal-publisher` |
| Kanban cards not appearing | `mqtt-kanban-bridge` |

View live logs for any service:
```bash
journalctl -u sensor-ingestion -f
journalctl -u mqtt-esp32-bridge -f
```

---

## 4 — Reflash env-node-01 (ESP32-S3)

**Plug in:** Use the port labelled **USB** (not UART) with a **data cable**.

**If the board isn't detected** (`ls /dev/ttyACM0` shows nothing):
→ Hold **BOOT**, tap **RST**, release BOOT — this forces ROM bootloader mode.
→ Run `lsusb` and confirm `303a:1001 Espressif` appears.

```bash
cd /home/sean/sensor_ecology/firmware/node01_pio
pio run -t upload
```

Monitor after flashing:
```bash
pio device monitor --port /dev/ttyACM0 --baud 115200
```

**Healthy boot output looks like:**
```
[TCS] cct=3600K lux=45 rRatio=0.38
[MQTT→] agents/env-node-01/interpretation : { ... "observation":"dim_warm" ... }
[MPU] rms=0.001g peak=1.08g zcr=22/s
```

---

## 5 — Reflash bme280 NodeMCU (ESP8266)

**Plug in:** Any USB port, standard micro-USB data cable.
Port will appear as `/dev/ttyUSB0` (CP2102 chip).

```bash
cd /home/sean/sensor_ecology/nodemcu_bme280
pio run -t upload --upload-port /dev/ttyUSB0
```

Monitor after flashing:
```bash
pio device monitor --port /dev/ttyUSB0 --baud 115200
```

**Healthy boot output looks like:**
```
WiFi → xraycanard ... connected (192.168.0.xx)
MQTT connected
Registered: nodemcu_bme280_3c71bf328564
[obs] nominal_conditions — Stable conditions: 17.1°C, 38% RH, 923 hPa.
```

---

## 6 — Reflash rfid NodeMCU (ESP8266)

Same process as BME280 — different directory:

```bash
cd /home/sean/sensor_ecology/nodemcu_rfid
pio run -t upload --upload-port /dev/ttyUSB0
```

---

## 7 — Telling Retained MQTT Messages from Live Data

NodeMCU registration messages are **retained** — the broker replays them even when the node is offline. A node showing `status: online` in a retained message does **not** mean it is currently running.

**To confirm a node is truly live:** check `last_event` in the DB (step 2) or watch for new `observation` / `interpretation` messages arriving in real time (`mosquitto_sub -W 30`).

---

## 8 — Dashboard

| URL | What it shows |
|---|---|
| `http://localhost:9500` | Main dashboard (Field / Stream / Resonance views) |
| `http://localhost:8765` | Relay API (Unity / AR client) |
| `http://localhost:8765/api/events` | Raw JSON event feed |

The **Stream** view shows all nodes with their names. If only `pi5-main` is visible, the other nodes are offline.
The **Nodes** indicator in the top bar shows `active/total` (e.g. `2/3` = 2 nodes fired in the last hour).

---

## 9 — Quick Diagnostic Sequence (start here when something feels off)

```
1. systemctl is-active mosquitto postgresql@16-main   ← infrastructure first
2. psql sensor_ecology -c "SELECT node_name, MAX(event_start) FROM perceptual_events JOIN agent_nodes ON id=agent_node_id GROUP BY node_name ORDER BY 2 DESC;"
3. mosquitto_sub -h localhost -t "agents/#" -W 10     ← is anything publishing?
4. journalctl -u mqtt-esp32-bridge -n 30              ← any bridge errors?
5. ls /dev/ttyUSB* /dev/ttyACM*                       ← if reflashing needed
```

---

*`/home/sean/sensor_ecology/MAINTENANCE.md` — update as the system grows.*
