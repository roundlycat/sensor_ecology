#pragma once
#include <Arduino.h>
#include "BaselineConfidence.h"
#include "MotifModule.h"
#include "config.h"

// ---------------------------------------------------------------------------
// DisplayModule — ambient, peripheral-perception display for the AZ3166 OLED.
//
// Canvas: 128×64 pixels. Rendered into a 1024-byte framebuffer in SSD1306
// page format (128 columns × 8 pages; each byte = 8 vertical pixels, LSB top).
// Pushed to hardware via Screen.draw(0, 0, 128, 8, _fb).
//
// THREE AMBIENT INDICATORS (normally distinct, quiet, peripheral):
//
//   1. Maturity arc  — top-left corner (~24×24 px, centre 13,13)
//      A partial circular arc that sweeps clockwise as version_minor grows.
//      A small tick mark near the centre for each version_major increment.
//      Slow, organic; the arc angle changes only as the version changes.
//
//   2. Stability pulse — horizontal centre (~cx=64, cy=32)
//      A dot that breathes (radius oscillates) at a rate proportional to
//      composite_stability. Filled when stability is high; open ring when low.
//      Breath speed and depth encode how settled the device is.
//
//   3. Motif accent — right side (~cx=112, drifting y)
//      A short horizontal mark or small filled square whose vertical position
//      encodes motif_growth_state. Drifts lazily toward the target position.
//      A small horizontal oscillation adds serendipitous life.
//
// CONCEPT FORMATION MERGE (merge_t ramps 0→1→0 over CONCEPT_MERGE_MS):
//   - Arc sweeps to near-complete; its centre fills softly.
//   - Pulse locks at maximum radius, filled solid.
//   - Accent drifts to centre-right and becomes a filled dot.
//   - All three drift toward the display centre, almost touching.
//   - A seed value from the promoted motif's texture introduces small
//     variations in timing and motion (no two merges look identical).
//   - The merged state fades out gradually — no hard cut.
// ---------------------------------------------------------------------------

class DisplayModule {
public:
    DisplayModule();

    // Call once in setup().
    void begin();

    // Call every DISPLAY_UPDATE_MS from loop().
    void update(const ConfidenceSnapshot& snap,
                MotifGrowthState         mgs,
                uint8_t vMajor, uint8_t vMinor,
                uint32_t nowMs);

    // Trigger the concept-formation merge animation.
    // seed: a float derived from motif features — drives serendipitous variation.
    void triggerConceptFormation(float seed, uint32_t nowMs);

private:
    // -----------------------------------------------------------------------
    // Framebuffer (SSD1306 page format)
    // -----------------------------------------------------------------------
    uint8_t _fb[DISPLAY_FB_BYTES];

    void clearFb();
    void setPixel(int x, int y, bool on = true);
    void drawArc(int cx, int cy, int r, float startRad, float endRad, int steps = 0);
    void drawDisk(int cx, int cy, int r);       // filled circle
    void drawRing(int cx, int cy, int r);       // outline circle
    void drawHSegment(int cx, int cy, int halfW); // horizontal tick
    void pushFb();

    // -----------------------------------------------------------------------
    // Animated state
    // -----------------------------------------------------------------------

    // Breathing oscillator (shared by all indicators)
    float    _breathPhase;  // [0, 2π)
    float    _breathSpeed;  // advances each update

    // Motif accent position (smoothly drifts toward target)
    float    _accentY;
    float    _accentXOffset;  // small serendipitous horizontal wander

    // Concept-formation merge state
    bool     _merging;
    uint32_t _mergeStartMs;
    float    _mergeSeed;

    // -----------------------------------------------------------------------
    // Per-indicator draw calls
    // -----------------------------------------------------------------------
    void drawMaturityArc(int cx, int cy, uint8_t vMajor, uint8_t vMinor,
                         float merge_t);
    void drawStabilityPulse(int cx, int cy, float stability,
                            float merge_t);
    void drawMotifAccent(int cx, MotifGrowthState mgs, float merge_t);

    // Compute the merge envelope (0→1→0) given elapsed time.
    float mergeEnvelope(uint32_t nowMs) const;
};
