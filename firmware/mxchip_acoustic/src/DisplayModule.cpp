#include "DisplayModule.h"
#include "config.h"
#include <OledDisplay.h>
#include <math.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979f
#endif
#define TWO_PI (2.0f * M_PI)

// ---------------------------------------------------------------------------
// Constructor
// ---------------------------------------------------------------------------
DisplayModule::DisplayModule()
    : _breathPhase(0.f),
      _breathSpeed(0.0025f),
      _accentY(32.f),
      _accentXOffset(0.f),
      _merging(false),
      _mergeStartMs(0),
      _mergeSeed(0.f)
{
    memset(_fb, 0, sizeof(_fb));
}

void DisplayModule::begin() {
    Screen.init();
    Screen.clean();
}

// ---------------------------------------------------------------------------
// Framebuffer primitives
// ---------------------------------------------------------------------------
void DisplayModule::clearFb() {
    memset(_fb, 0, sizeof(_fb));
}

void DisplayModule::setPixel(int x, int y, bool on) {
    if (x < 0 || x >= DISPLAY_FB_W || y < 0 || y >= 64) return;
    int page = y >> 3;          // y / 8
    int bit  = y & 7;           // y % 8
    uint8_t& cell = _fb[page * DISPLAY_FB_W + x];
    if (on) cell |=  (1u << bit);
    else     cell &= ~(1u << bit);
}

void DisplayModule::drawArc(int cx, int cy, int r,
                            float startRad, float endRad, int steps)
{
    if (steps <= 0) steps = max(8, r * 6);
    float span = endRad - startRad;
    for (int i = 0; i <= steps; i++) {
        float a = startRad + span * ((float)i / steps);
        int x = cx + (int)(r * cosf(a) + 0.5f);
        int y = cy + (int)(r * sinf(a) + 0.5f);
        setPixel(x, y);
    }
}

void DisplayModule::drawRing(int cx, int cy, int r) {
    drawArc(cx, cy, r, 0.f, TWO_PI);
}

void DisplayModule::drawDisk(int cx, int cy, int r) {
    for (int dy = -r; dy <= r; dy++) {
        for (int dx = -r; dx <= r; dx++) {
            if (dx*dx + dy*dy <= r*r) setPixel(cx+dx, cy+dy);
        }
    }
}

void DisplayModule::drawHSegment(int cx, int cy, int halfW) {
    for (int dx = -halfW; dx <= halfW; dx++) setPixel(cx+dx, cy);
}

void DisplayModule::pushFb() {
    // Screen.draw(x0, y0_page, x1, y1_page, bmp[])
    Screen.draw(0, 0, DISPLAY_FB_W, DISPLAY_FB_PAGES, _fb);
}

// ---------------------------------------------------------------------------
// merge_t envelope: fast rise (0→0.2 → t=1), slow fall (0.2→1.0 → t=0)
// Modulated slightly by the motif seed for serendipitous variation.
// ---------------------------------------------------------------------------
float DisplayModule::mergeEnvelope(uint32_t nowMs) const {
    if (!_merging) return 0.f;
    float progress = (float)(nowMs - _mergeStartMs) / (float)CONCEPT_MERGE_MS;
    if (progress >= 1.0f) return 0.f;

    float t;
    if (progress < 0.18f) {
        t = progress / 0.18f;               // fast rise
    } else {
        t = 1.0f - (progress - 0.18f) / 0.82f;  // slow fall
    }
    // Serendipity: small sinusoidal wobble driven by seed
    t *= (0.88f + 0.12f * sinf(_mergeSeed + progress * 5.3f));
    return (t < 0.f) ? 0.f : (t > 1.f ? 1.f : t);
}

// ---------------------------------------------------------------------------
// 1. Maturity arc — top-left, centre (13, 13), outer radius 11
//
// Arc sweeps from -π/2 (top) clockwise by (vMinor / 255 * 2π).
// Tick marks at the centre for version_major (up to 4, one per quarter).
// At high merge_t the arc completes and the interior gently fills.
// ---------------------------------------------------------------------------
void DisplayModule::drawMaturityArc(int cx, int cy,
                                    uint8_t vMajor, uint8_t vMinor,
                                    float merge_t)
{
    const int r = 11;

    // Normal arc end-angle based on version_minor progress
    float baseEnd = -M_PI/2.f + ((float)vMinor / 255.f) * TWO_PI;
    // During merge: arc sweeps further toward completion
    float endAngle = baseEnd + merge_t * ((-M_PI/2.f + TWO_PI) - baseEnd);

    // Draw the main arc
    drawArc(cx, cy, r, -M_PI/2.f, endAngle);

    // Small tick marks for version_major (progress inward from arc rim)
    for (int m = 0; m < (int)vMajor && m < 4; m++) {
        float a = -M_PI/2.f + (float)m * (M_PI / 4.f);
        int tx = cx + (int)((r - 3) * cosf(a) + 0.5f);
        int ty = cy + (int)((r - 3) * sinf(a) + 0.5f);
        setPixel(tx, ty);
        // Two-pixel tick
        setPixel(tx + (int)(cosf(a) + 0.5f), ty + (int)(sinf(a) + 0.5f));
    }

    // Soft interior fill during merge — radius grows with merge_t
    if (merge_t > 0.35f) {
        int fillR = (int)((merge_t - 0.35f) / 0.65f * (float)(r - 3) + 0.5f);
        if (fillR >= 1) {
            // Draw as a soft ring, not a hard disk, to stay ambient
            drawRing(cx, cy, fillR);
            if (fillR > 3) drawRing(cx, cy, fillR - 2);
        }
    }
}

// ---------------------------------------------------------------------------
// 2. Stability pulse — centre (64, 32), breathing dot
//
// Radius oscillates between minR and maxR at a rate set by stability.
// Filled disk when stable; open ring when unsettled.
// During merge: radius locks at maxR and the dot fills.
// ---------------------------------------------------------------------------
void DisplayModule::drawStabilityPulse(int cx, int cy,
                                       float stability,
                                       float merge_t)
{
    // minR and maxR scale with stability: more stable → larger dot
    float minR = 1.5f + stability * 2.5f;
    float maxR = 3.0f + stability * 5.0f;

    // Breathe: sinf goes -1→1, map to 0→1
    float breathe = (sinf(_breathPhase) + 1.0f) * 0.5f;
    float floatR  = minR + breathe * (maxR - minR);

    // Merge: lock to maxR (gives sense of quiet culmination)
    floatR = floatR + merge_t * (maxR - floatR);
    int ri = (int)(floatR + 0.5f);
    if (ri < 1) ri = 1;

    bool filled = (stability > 0.55f) || (merge_t > 0.25f);
    if (filled) {
        drawDisk(cx, cy, ri);
    } else {
        // Two concentric rings for visual weight without full fill
        drawRing(cx, cy, ri);
        if (ri > 2) drawRing(cx, cy, ri - 1);
    }
}

// ---------------------------------------------------------------------------
// 3. Motif accent — right side, x around 112, y drifts to target
//
// Target y encodes growth state: higher (lower on screen) = more unsettled.
// Horizontal wander (±4 px) driven by the breath phase for organic feel.
// During merge: drifts toward centre-right and solidifies into a disk.
// ---------------------------------------------------------------------------
void DisplayModule::drawMotifAccent(int baseX, MotifGrowthState mgs,
                                    float merge_t)
{
    // Map growth state to a target y (0=top, 63=bottom; 32=centre)
    float targetY;
    switch (mgs) {
        case MGS_IDLE:                targetY = 32.f; break;
        case MGS_DRIFTING:            targetY = 44.f; break;  // below centre → restless
        case MGS_STABILIZING:         targetY = 22.f; break;  // above centre → gathering
        case MGS_READY_FOR_PROMOTION: targetY = 14.f; break;  // near top → about to crystallise
        default:                      targetY = 32.f;
    }

    // Ease current position toward target (slow drift)
    _accentY += (targetY - _accentY) * 0.04f;

    // Serendipitous horizontal wander (slow sinusoid)
    _accentXOffset += 0.007f;
    if (_accentXOffset > TWO_PI) _accentXOffset -= TWO_PI;
    float wander = sinf(_accentXOffset) * 4.0f;

    // Merge: pull accent toward (96, 32) — approaching the pulse
    float mergeTargetX = 96.f;
    float mergeTargetY = 32.f;
    float ax = (float)baseX + wander + merge_t * (mergeTargetX - (float)baseX - wander);
    float ay = _accentY + merge_t * (mergeTargetY - _accentY);

    int ix = (int)(ax + 0.5f);
    int iy = (int)(ay + 0.5f);

    if (merge_t > 0.55f) {
        // Solidify into a small filled dot during merge
        int dotR = (int)(merge_t * 3.f + 0.5f);
        if (dotR < 1) dotR = 1;
        drawDisk(ix, iy, dotR);
    } else {
        // Normally: a 5-pixel horizontal tick mark
        drawHSegment(ix, iy, 2);
        // Second row for visual weight
        setPixel(ix, iy + 1);
    }
}

// ---------------------------------------------------------------------------
// Public: trigger concept-formation merge
// ---------------------------------------------------------------------------
void DisplayModule::triggerConceptFormation(float seed, uint32_t nowMs) {
    _merging      = true;
    _mergeStartMs = nowMs;
    _mergeSeed    = seed;
}

// ---------------------------------------------------------------------------
// Main update
// ---------------------------------------------------------------------------
void DisplayModule::update(
    const ConfidenceSnapshot& snap,
    MotifGrowthState          mgs,
    uint8_t vMajor, uint8_t vMinor,
    uint32_t nowMs)
{
    // Check if merge has expired
    if (_merging) {
        float elapsed = (float)(nowMs - _mergeStartMs);
        if (elapsed >= (float)CONCEPT_MERGE_MS) _merging = false;
    }

    // Advance breath oscillator.
    // Breath is calmer (slower) when stable — a subtle cue.
    _breathSpeed = 0.0018f + snap.composite_stability * 0.003f;
    _breathPhase += _breathSpeed;
    if (_breathPhase >= TWO_PI) _breathPhase -= TWO_PI;

    float merge_t = mergeEnvelope(nowMs);

    clearFb();

    // Fixed centres for the three indicators
    const int ARC_CX   = 13, ARC_CY = 13;   // top-left
    const int PULSE_CX = 64, PULSE_CY = 32;  // centre
    const int ACCENT_X = 112;                 // right edge, y floats

    drawMaturityArc(ARC_CX, ARC_CY, vMajor, vMinor, merge_t);
    drawStabilityPulse(PULSE_CX, PULSE_CY, snap.composite_stability, merge_t);
    drawMotifAccent(ACCENT_X, mgs, merge_t);

    pushFb();
}
