#include "MemoryBlock.h"
#include <EEPROMInterface.h>
#include <string.h>

static EEPROMInterface g_eeprom;

MemoryBlockModule::MemoryBlockModule()
    : _loaded(false),
      _lastSaveMs(0),
      _lastBumpMs(0),
      _motifSlotNext(0)
{
    memset(&_data, 0, sizeof(_data));
    _data.version_major = FW_VERSION_MAJOR;
    _data.version_minor = FW_VERSION_MINOR;
}

// ---------------------------------------------------------------------------
// Checksum: simple byte sum over all fields except the checksum itself.
// Intentionally simple — the STSAFE hardware already provides tamper
// detection; we just want to catch corruption and uninitialised memory.
// ---------------------------------------------------------------------------
uint16_t MemoryBlockModule::computeChecksum(const MemoryBlockData& d) const {
    const uint8_t* p = reinterpret_cast<const uint8_t*>(&d);
    uint16_t sum = 0;
    for (size_t i = 0; i < sizeof(MemoryBlockData) - sizeof(uint16_t); i++) {
        sum += p[i];
    }
    return sum;
}

// ---------------------------------------------------------------------------
bool MemoryBlockModule::loadMemoryBlock() {
    MemoryBlockData buf;
    memset(&buf, 0, sizeof(buf));

    int nRead = g_eeprom.read(
        reinterpret_cast<uint8_t*>(&buf),
        sizeof(MemoryBlockData),
        /*offset=*/0,
        MEMORY_ZONE);

    if (nRead < (int)sizeof(MemoryBlockData)) {
        Serial.printf("[mem] short read (%d bytes) — fresh start\n", nRead);
        return false;
    }

    uint16_t expected = computeChecksum(buf);
    if (buf.checksum != expected) {
        Serial.printf("[mem] checksum fail (got 0x%04X expected 0x%04X) — fresh start\n",
                      buf.checksum, expected);
        return false;
    }

    if (buf.version_major != FW_VERSION_MAJOR) {
        Serial.printf("[mem] version mismatch (stored %u, fw %u) — migrating\n",
                      buf.version_major, FW_VERSION_MAJOR);
        migrate(buf.version_major);
        return false;
    }

    _data   = buf;
    _loaded = true;
    Serial.printf("[mem] loaded v%u.%u  concepts=%lu\n",
                  _data.version_major, _data.version_minor,
                  (unsigned long)_data.total_concepts);
    return true;
}

// ---------------------------------------------------------------------------
void MemoryBlockModule::saveMemoryBlock(
    float rms, float var, float hum, float tex, float transThresh,
    uint32_t nowMs, bool force)
{
    if (!force && _lastSaveMs != 0 &&
        (nowMs - _lastSaveMs) < MEMORY_SAVE_INTERVAL_MS) {
        return;  // Rate-limit to protect flash endurance
    }

    _data.baseline_rms      = rms;
    _data.baseline_variance = var;
    _data.baseline_hum      = hum;
    _data.baseline_texture  = tex;
    _data.transient_thresh  = transThresh;
    _data.checksum          = computeChecksum(_data);

    int result = g_eeprom.write(
        reinterpret_cast<uint8_t*>(&_data),
        sizeof(MemoryBlockData),
        MEMORY_ZONE);

    if (result == 0) {
        _lastSaveMs = nowMs;
        Serial.printf("[mem] saved v%u.%u  concepts=%lu\n",
                      _data.version_major, _data.version_minor,
                      (unsigned long)_data.total_concepts);
    } else {
        Serial.printf("[mem] write failed (err %d)\n", result);
    }
}

// ---------------------------------------------------------------------------
void MemoryBlockModule::recordMotifSummary(
    float rms, float hum, float texture, float strength)
{
    auto& slot = _data.motifs[_motifSlotNext % 3];
    slot.rms      = rms;
    slot.hum      = hum;
    slot.texture  = texture;
    slot.strength = (uint8_t)(fminf(strength, 1.f) * 255.f);
    _motifSlotNext = (_motifSlotNext + 1) % 3;
    _data.total_concepts++;
}

// ---------------------------------------------------------------------------
bool MemoryBlockModule::maybeIncrementMinorVersion(
    float compositeStability, uint32_t nowMs)
{
    if (compositeStability < MINOR_VERSION_STABILITY_THRESH) return false;
    if (_lastBumpMs != 0 && (nowMs - _lastBumpMs) < MINOR_BUMP_INTERVAL_MS) {
        return false;
    }

    _data.version_minor++;
    _lastBumpMs = nowMs;
    Serial.printf("[mem] minor version → %u.%u (stability=%.2f)\n",
                  _data.version_major, _data.version_minor, compositeStability);
    return true;
}

// ---------------------------------------------------------------------------
// Migration stub
// When FW_VERSION_MAJOR changes, implement field-by-field porting here.
// The current implementation simply triggers a fresh start; add cases as
// the layout evolves.
// ---------------------------------------------------------------------------
void MemoryBlockModule::migrate(uint8_t fromMajor) {
    // TODO v1→v2: extract baseline_rms/hum/texture from old layout offset X
    // and copy to new layout, resetting fields that don't have equivalents.
    (void)fromMajor;
    Serial.println("[mem] migration not implemented — fresh calibration");
}
