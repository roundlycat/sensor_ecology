#pragma once
#include <Arduino.h>
#include <Preferences.h>
#include "../config.h"
#include "../motif/MotifModule.h"    // for MotifCandidate
#include "../baseline/BaselineAndConfidenceModule.h"

// MemoryBlock — the complete persistent state of the acoustic listener.
// Written as a single binary blob under NVS key "block" using putBytes/getBytes
// to guarantee atomicity (no partial writes across power cycles).
//
// Version layout: major is set by firmware. Minor auto-increments when the
// device sustains high stability for WINDOWS_FOR_MINOR_BUMP consecutive windows.
// Minor version saturates at 99 — overflow does NOT bump major.
//
// Fletcher-16 checksum covers all bytes preceding the checksum field.
struct __attribute__((packed)) MemoryBlock {
    uint8_t  version_major;         // == FIRMWARE_MAJOR to load without migration
    uint8_t  version_minor;         // auto-increments on sustained refinement
    float    baseline_rms;
    float    baseline_variance;
    float    baseline_hum;
    float    baseline_texture;      // spectral_flatness baseline
    float    transient_threshold;
    float    learning_rate_alpha;   // persisted so care adjustments survive reboot
    MotifCandidate motifs[MOTIF_RING_SIZE];
    uint32_t total_windows_seen;    // lifetime window counter
    uint16_t checksum;              // Fletcher-16 over all preceding bytes
};

// MemoryBlockModule
// Responsibilities:
//   - Load MemoryBlock from NVS on boot (validate checksum + major version).
//   - Provide a live MemoryBlock that other modules update in-place.
//   - Rate-limited NVS write: at most once per MEMORY_WRITE_MIN_INTERVAL_MS
//     (millis()-gated, not window-count-gated, so crashes don't corrupt timing).
//   - Auto-increment version_minor when the baseline module sustains
//     composite_stability > STABILITY_FOR_MINOR_VERSION for
//     WINDOWS_FOR_MINOR_BUMP consecutive windows.
//
// Only this module touches Preferences directly.
class MemoryBlockModule {
public:
    MemoryBlockModule();

    // Open NVS namespace and attempt to load a valid MemoryBlock.
    // Returns true if a valid block was loaded (caller should seed modules).
    // Returns false if no valid block exists (factory defaults used).
    bool begin();

    // Sync live state from the running modules into the block's fields.
    // Called each window so the block stays current before a maybeSave().
    void updateFromModules(const BaselineSnapshot& b,
                           const MotifCandidate* ring,
                           uint32_t total_windows);

    // Conditionally increment version_minor if stability criteria are met.
    // Pass consecutive_high_stability_windows from the caller.
    void maybeIncrementMinorVersion(const BaselineSnapshot& b, uint32_t now);

    // Write to NVS if the rate-limit window has elapsed.
    // Safe to call every loop() — the millis() gate enforces the 10-min minimum.
    void maybeSave(uint32_t now);

    // Read-only access to the live block for Display and Publisher.
    const MemoryBlock& getBlock() const { return _block; }

    // Mutable access for seeding modules from restored block.
    const MemoryBlock& getRestoredBlock() const { return _block; }

    // Force an immediate NVS write (e.g. on calibration request).
    // Bypasses the rate-limit — use sparingly.
    void forceSave();

private:
    Preferences  _prefs;
    MemoryBlock  _block;
    bool         _loaded = false;
    uint32_t     _lastSaveMs = 0;

    // Consecutive windows above STABILITY_FOR_MINOR_VERSION threshold
    uint32_t _consecutiveHighWindows = 0;

    static uint16_t _fletcher16(const uint8_t* data, size_t len);
    bool            _validate() const;
    void            _initDefaults();
    void            _writeToNVS();
};
