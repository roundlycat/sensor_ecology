#include "MemoryBlockModule.h"
#include <string.h>

// ── Constructor ───────────────────────────────────────────────────────────────

MemoryBlockModule::MemoryBlockModule() {
    _initDefaults();
}

// ── Public ────────────────────────────────────────────────────────────────────

bool MemoryBlockModule::begin() {
    _prefs.begin(NVS_NAMESPACE, /*readOnly=*/false);

    // Attempt to load existing block from NVS.
    const size_t expected = sizeof(MemoryBlock);
    size_t got = _prefs.getBytes(NVS_KEY_BLOCK, &_block, expected);

    if (got == expected && _validate()) {
        if (_block.version_major == FIRMWARE_MAJOR) {
            _loaded = true;
            return true;
        }
        // Major version mismatch: migration stub.
        // For now, fall through to defaults (future: migrate fields).
        // TODO: migration logic for version_major != FIRMWARE_MAJOR
    }

    // No valid block found — start with factory defaults.
    _initDefaults();
    _loaded = false;
    return false;
}

void MemoryBlockModule::updateFromModules(const BaselineSnapshot& b,
                                          const MotifCandidate* ring,
                                          uint32_t total_windows) {
    _block.baseline_rms        = b.baseline_rms;
    _block.baseline_variance   = b.baseline_variance;
    _block.baseline_hum        = b.baseline_hum;
    _block.baseline_texture    = b.baseline_texture;
    _block.transient_threshold = b.transient_threshold;
    _block.learning_rate_alpha = b.learning_rate_alpha;
    _block.total_windows_seen  = total_windows;
    memcpy(_block.motifs, ring, sizeof(_block.motifs));
}

void MemoryBlockModule::maybeIncrementMinorVersion(const BaselineSnapshot& b,
                                                   uint32_t /*now*/) {
    // Track consecutive windows above the stability threshold.
    if (b.composite_stability >= STABILITY_FOR_MINOR_VERSION) {
        _consecutiveHighWindows++;
    } else {
        _consecutiveHighWindows = 0;
    }

    // Require sustained stability before bumping the minor version.
    if (_consecutiveHighWindows >= WINDOWS_FOR_MINOR_BUMP) {
        _consecutiveHighWindows = 0;    // reset so we don't bump every window
        if (_block.version_minor < 99) {
            _block.version_minor++;
            // Minor version bump is intentionally not forced to NVS immediately.
            // maybeSave() will persist it within the next 10-minute window.
        }
        // At 99, saturate. No rollover, no major bump (major is firmware-level only).
    }
}

void MemoryBlockModule::maybeSave(uint32_t now) {
    // Rate-limit: at most one NVS write per MEMORY_WRITE_MIN_INTERVAL_MS.
    // millis()-gated rather than window-count-gated — window counts can be
    // incorrect after a crash, but millis() reflects wall time since boot.
    if ((now - _lastSaveMs) >= MEMORY_WRITE_MIN_INTERVAL_MS) {
        _writeToNVS();
        _lastSaveMs = now;
    }
}

void MemoryBlockModule::forceSave() {
    _writeToNVS();
    _lastSaveMs = millis();
}

// ── Private ───────────────────────────────────────────────────────────────────

void MemoryBlockModule::_initDefaults() {
    memset(&_block, 0, sizeof(_block));
    _block.version_major       = FIRMWARE_MAJOR;
    _block.version_minor       = 0;
    _block.baseline_rms        = 0.0f;
    _block.baseline_variance   = 0.0f;
    _block.baseline_hum        = 0.0f;
    _block.baseline_texture    = 0.5f;   // start neutral on spectral flatness
    _block.transient_threshold = TRANSIENT_THRESHOLD_INIT;
    _block.learning_rate_alpha = ALPHA_DEFAULT;
    _block.total_windows_seen  = 0;
    // motifs are zeroed (all inactive) by memset
}

uint16_t MemoryBlockModule::_fletcher16(const uint8_t* data, size_t len) {
    // Fletcher-16 checksum over 'len' bytes.
    // Provides error detection without false negatives for single-bit flips.
    uint16_t sum1 = 0, sum2 = 0;
    for (size_t i = 0; i < len; i++) {
        sum1 = (sum1 + data[i]) % 255;
        sum2 = (sum2 + sum1)    % 255;
    }
    return (sum2 << 8) | sum1;
}

bool MemoryBlockModule::_validate() const {
    // Checksum covers everything before the checksum field itself.
    const size_t payload = offsetof(MemoryBlock, checksum);
    uint16_t computed = _fletcher16(reinterpret_cast<const uint8_t*>(&_block),
                                    payload);
    return computed == _block.checksum;
}

void MemoryBlockModule::_writeToNVS() {
    // Recompute checksum before writing.
    const size_t payload = offsetof(MemoryBlock, checksum);
    _block.checksum = _fletcher16(
        reinterpret_cast<const uint8_t*>(&_block), payload);

    _prefs.putBytes(NVS_KEY_BLOCK, &_block, sizeof(_block));
}
