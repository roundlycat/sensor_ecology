#pragma once
#include <Arduino.h>
#include "config.h"

// ---------------------------------------------------------------------------
// MemoryBlockData — the single, fixed-size persistent record.
//
// Layout is intentionally flat and fixed-width. Any structural change
// (adding/removing fields) must bump FW_VERSION_MAJOR and implement a
// migration path in MemoryBlockModule::migrate().
//
// Stored in STSAFE-A100 zone 7 (784 bytes); this struct is ~72 bytes.
// The remaining ~700 bytes are reserved for future expansion without
// requiring a version bump.
// ---------------------------------------------------------------------------
#pragma pack(push, 1)
struct MemoryBlockData {
    uint8_t  version_major;      // 1
    uint8_t  version_minor;      // 1  ← self-increments at runtime

    float    baseline_rms;       // 4
    float    baseline_variance;  // 4
    float    baseline_hum;       // 4
    float    baseline_texture;   // 4
    float    transient_thresh;   // 4

    // Compact summaries of up to 3 promoted motifs.
    // Written when a motif is promoted; oldest overwritten in round-robin.
    struct MotifSummary {
        float   rms;       // 4
        float   hum;       // 4
        float   texture;   // 4
        uint8_t strength;  // 1  (0–255 mapped from float strength * 255)
        uint8_t _pad;      // 1  alignment
    } motifs[3];            // 3 × 14 = 42 bytes

    uint32_t total_concepts;  // lifetime concept-formation counter
    uint16_t checksum;        // simple byte-sum over all preceding bytes
};
#pragma pack(pop)

static_assert(sizeof(MemoryBlockData) <= 72,
              "MemoryBlockData exceeds expected size");

// ---------------------------------------------------------------------------
class MemoryBlockModule {
public:
    MemoryBlockModule();

    // Read from STSAFE flash; validate checksum and version.
    // Returns true if the block is valid and version-compatible.
    bool loadMemoryBlock();

    // Write current state to flash. Rate-limited by MEMORY_SAVE_INTERVAL_MS;
    // pass force=true to bypass the rate limit (e.g., at concept formation).
    void saveMemoryBlock(float rms, float var, float hum,
                         float tex, float transThresh,
                         uint32_t nowMs, bool force = false);

    // Add a compact motif summary to the memory block's rotating motif array.
    void recordMotifSummary(float rms, float hum, float texture, float strength);

    // Increment version_minor if stability and time thresholds are met.
    // Returns true if the version was actually bumped.
    bool maybeIncrementMinorVersion(float compositeStability, uint32_t nowMs);

    // Accessors
    uint8_t  getVersionMajor()   const { return _data.version_major;   }
    uint8_t  getVersionMinor()   const { return _data.version_minor;   }
    uint32_t getTotalConcepts()  const { return _data.total_concepts;   }
    const MemoryBlockData& getData() const { return _data; }
    bool isLoaded() const { return _loaded; }

private:
    MemoryBlockData _data;
    bool            _loaded;
    uint32_t        _lastSaveMs;
    uint32_t        _lastBumpMs;
    uint8_t         _motifSlotNext;  // round-robin index into motifs[3]

    uint16_t computeChecksum(const MemoryBlockData& d) const;

    // Stub: called when version_major mismatches. Implement migration logic
    // here when the memory layout changes between firmware versions.
    void migrate(uint8_t fromMajor);
};
