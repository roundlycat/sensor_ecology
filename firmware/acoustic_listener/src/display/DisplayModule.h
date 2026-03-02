#pragma once
#include <Arduino.h>
#include <U8g2lib.h>
#include <math.h>
#include "../config.h"
#include "../memory/MemoryBlockModule.h"
#include "../baseline/BaselineAndConfidenceModule.h"
#include "../motif/MotifModule.h"
#include "../care/CareEthicsHooks.h"

// DisplayModule
// Responsibilities:
//   - Render three ambient indicators on the SSD1306 128×64 OLED.
//   - Never blink, flash, or invert — ambient-only visual language.
//   - Drive a concept formation merge animation when a motif is promoted.
//   - Derive visual parameters from care/baseline state, not raw numbers.
//
// Three indicators:
//   1. Maturity ring    — thin arc, centre (64,32), radius 24px
//                         Arc extent = version_minor/99 * 360°
//   2. Stability pulse  — filled circle at (20,48), radius 2–5px breathing
//                         Breath rate derived from composite_stability
//   3. Motif accent     — 3×3 square on a Lissajous path around (108,16)
//                         Drift speed from CareEthicsHooks.getCurrentState()
//
// Concept formation merge animation (triggered by concept_formation_event):
//   CONVERGING → HOLDING → FADING
//   Timing interpolated with millis(). No delay() anywhere.
//   Serendipity: ±3px jitter on convergence point, ±200ms on timing.
class DisplayModule {
public:
    DisplayModule() = default;

    // Wire up the care hooks pointer. Must be called before begin().
    void setCareHooks(CareEthicsHooks* care) { _care = care; }

    // Initialise U8g2, I2C bus, OLED contrast. Returns false if display not found.
    bool begin();

    // Render one frame. Call every loop().
    // Parameters sourced from main.cpp loop — all pass-through, no state stored here
    // except animation timing.
    void update(uint32_t now,
                const MemoryBlock&      mem,
                const BaselineSnapshot& b,
                MotifGrowthState        motifState,
                bool                    conceptFormationEvent);

private:
    // ── U8g2 display driver ───────────────────────────────────────────────────
    // Full frame buffer mode: clearBuffer() then sendBuffer().
    // Hardware I2C using Wire configured with DISPLAY_SDA/DISPLAY_SCL in begin().
    U8G2_SSD1306_128X64_NONAME_F_HW_I2C _u8g2{U8G2_R0, U8X8_PIN_NONE};

    CareEthicsHooks* _care = nullptr;

    // ── Animation state ───────────────────────────────────────────────────────
    float    _breathePhase  = 0.0f;   // radians, advances each frame
    float    _lissajousPhase = 0.0f;  // radians, advances each frame
    uint32_t _lastUpdateMs  = 0;

    // Contrast rate-limit (max 2 changes/second on some SSD1306 clones)
    uint32_t _lastContrastMs = 0;
    uint8_t  _lastContrast   = 128;

    // ── Merge animation ───────────────────────────────────────────────────────
    enum class MergePhase { NONE, CONVERGING, HOLDING, FADING };
    MergePhase _mergePhase = MergePhase::NONE;
    uint32_t   _mergeStartMs     = 0;
    int8_t     _mergeJitterX     = 0;   // ±3px serendipity
    int8_t     _mergeJitterY     = 0;
    uint32_t   _mergeTimingJitter = 0;  // ±200ms

    // ── Drawing helpers ───────────────────────────────────────────────────────
    // arcExtentDeg: 0–360, computed by update() before calling.
    void _drawMaturityRing(float arcExtentDeg);
    void _drawStabilityPulse(float composite_stability, int cx, int cy);
    void _drawMotifAccent(float lx, float ly);
    void _drawArcDeg(int cx, int cy, int r, float startDeg, float endDeg);
    void _setContrast(uint8_t level, uint32_t now);
};
