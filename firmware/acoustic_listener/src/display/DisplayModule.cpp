#include "DisplayModule.h"
#include <Wire.h>
#include "esp_random.h"   // esp_random() for better entropy on ESP32-S3

// ── Helpers ───────────────────────────────────────────────────────────────────

static inline float clamp01f(float v) {
    return v < 0.0f ? 0.0f : (v > 1.0f ? 1.0f : v);
}

static inline float lerpf(float a, float b, float t) {
    return a + (b - a) * t;
}

// ── Public ────────────────────────────────────────────────────────────────────

bool DisplayModule::begin() {
    // Configure I2C with board-specific pins before U8g2 init.
    Wire.begin(DISPLAY_SDA, DISPLAY_SCL);
    Wire.setClock(400000);

    _u8g2.setI2CAddress(DISPLAY_ADDR << 1);  // U8g2 expects 8-bit address
    _u8g2.begin();

    // Start dim — brightness ramps up with version_minor over the device's lifetime.
    _u8g2.setContrast(40);
    _lastContrastMs = 0;
    _lastContrast   = 40;

    return true;  // U8g2 doesn't report init failure; presence confirmed visually
}

void DisplayModule::update(uint32_t now,
                            const MemoryBlock&      mem,
                            const BaselineSnapshot& b,
                            MotifGrowthState        motifState,
                            bool                    conceptFormationEvent) {
    const uint32_t deltaMs = (now > _lastUpdateMs) ? (now - _lastUpdateMs) : 0;
    _lastUpdateMs = now;

    // ── Trigger merge animation on concept formation ───────────────────────────
    if (conceptFormationEvent && _mergePhase == MergePhase::NONE) {
        _mergePhase    = MergePhase::CONVERGING;
        _mergeStartMs  = now;
        // Serendipity: randomise convergence jitter and timing offset.
        // esp_random() provides hardware entropy, not the predictable rand().
        _mergeJitterX     = (int8_t)((esp_random() % 7) - 3);   // ±3 px
        _mergeJitterY     = (int8_t)((esp_random() % 7) - 3);
        _mergeTimingJitter = esp_random() % 401;                  // 0–400 ms
    }

    // ── Advance animation state ────────────────────────────────────────────────
    float mergeProgress = 0.0f;   // 0=home positions, 1=converged at centre

    if (_mergePhase != MergePhase::NONE) {
        const uint32_t elapsed = now - _mergeStartMs;
        const uint32_t convergeDur = MERGE_DURATION_MS + _mergeTimingJitter;

        if (_mergePhase == MergePhase::CONVERGING) {
            mergeProgress = clamp01f((float)elapsed / (float)convergeDur);
            if (elapsed >= convergeDur) {
                _mergePhase   = MergePhase::HOLDING;
                _mergeStartMs = now;
            }
        } else if (_mergePhase == MergePhase::HOLDING) {
            mergeProgress = 1.0f;
            if (elapsed >= MERGE_HOLD_MS) {
                _mergePhase   = MergePhase::FADING;
                _mergeStartMs = now;
            }
        } else if (_mergePhase == MergePhase::FADING) {
            mergeProgress = clamp01f(1.0f - (float)elapsed / (float)MERGE_FADE_MS);
            if (elapsed >= MERGE_FADE_MS) {
                _mergePhase   = MergePhase::NONE;
                mergeProgress = 0.0f;
            }
        }
    }

    // ── Advance indicator phases ───────────────────────────────────────────────
    const float dt_s = (float)deltaMs * 0.001f;   // delta time in seconds

    // Stability pulse breathing: slow when stable, fast when unsettled.
    // composite_stability=1.0 → breatheRate≈0.5 rad/s (period ~12s, very slow)
    // composite_stability=0.0 → breatheRate≈4.0 rad/s (period ~1.5s, restless)
    const float breatheRate = 0.5f + 3.5f * (1.0f - b.composite_stability);
    _breathePhase += breatheRate * dt_s;

    // Motif accent Lissajous drift.
    // Speed is derived from CareState (kept in CareEthicsHooks, not raw stability)
    // so the display responds to care signals, not just raw acoustics.
    float driftSpeed = 0.002f;  // rad/s default
    if (_care != nullptr) {
        switch (_care->getCurrentState()) {
            case CareState::STABLE:      driftSpeed = 0.001f; break;
            case CareState::UNSETTLED:   driftSpeed = 0.004f; break;
            case CareState::LEARNING:    driftSpeed = 0.010f; break;
            case CareState::CALIBRATING: driftSpeed = 0.020f; break;
        }
    }
    _lissajousPhase += driftSpeed * (float)deltaMs;  // deltaMs used directly for scale

    // ── Adaptive contrast: brightness scales with maturity ────────────────────
    // version_minor goes from 0 to 99. Contrast ranges from 40 to 200.
    // Soft cap: max 2 contrast changes per second to avoid SSD1306 flicker.
    const uint8_t targetContrast = (uint8_t)(40 + (160 * mem.version_minor / 99));
    _setContrast(targetContrast, now);

    // ── Compute indicator positions (home or merge-interpolated) ──────────────
    // Home positions (from spec):
    //   Stability pulse:  (20, 48)
    //   Motif accent Lissajous base: (108, 16)
    float pulseX = 20.0f, pulseY = 48.0f;
    float accentBaseX = 108.0f, accentBaseY = 16.0f;

    if (mergeProgress > 0.0f) {
        // Convergence target: centre (64,32) + serendipity jitter
        const float cx = 64.0f + _mergeJitterX;
        const float cy = 32.0f + _mergeJitterY;
        pulseX = lerpf(20.0f, cx, mergeProgress);
        pulseY = lerpf(48.0f, cy, mergeProgress);
        // Motif accent: lerp Lissajous base to centre
        accentBaseX = lerpf(108.0f, cx, mergeProgress);
        accentBaseY = lerpf(16.0f,  cy, mergeProgress);
    }

    // Lissajous offset: a=3, b=2 pattern; ±8px amplitude
    const float lx = accentBaseX + 8.0f * sinf(3.0f * _lissajousPhase + (float)M_PI / 2.0f);
    const float ly = accentBaseY + 8.0f * sinf(2.0f * _lissajousPhase);

    // ── Render frame ──────────────────────────────────────────────────────────
    // U8g2 full-frame buffer: clearBuffer then draw everything, then sendBuffer.
    // This is safe to call every loop() iteration — sendBuffer() takes ~20ms on
    // SSD1306 at 400kHz I2C, which fits the 32ms/window budget.
    _u8g2.clearBuffer();

    // 1. Maturity ring — arc grows from top (0°) clockwise as version_minor rises
    float arcExtent = (float)mem.version_minor / 99.0f * 360.0f;
    if (_mergePhase == MergePhase::HOLDING) {
        arcExtent = 360.0f;  // ring closes to full circle during hold
    } else if (_mergePhase == MergePhase::FADING) {
        // Gradually open the ring back to its earned extent
        const float earnedExtent = (float)mem.version_minor / 99.0f * 360.0f;
        const float t = 1.0f - mergeProgress;  // mergeProgress goes 1→0 during fade
        arcExtent = lerpf(360.0f, earnedExtent, t);
    }
    _drawMaturityRing(arcExtent);

    // 2. Stability pulse — breathing circle
    _drawStabilityPulse(b.composite_stability, (int)pulseX, (int)pulseY);

    // 3. Motif accent — Lissajous 3×3 square
    _drawMotifAccent(lx, ly);

    _u8g2.sendBuffer();
}

// ── Private ───────────────────────────────────────────────────────────────────

void DisplayModule::_drawMaturityRing(float arcExtentDeg) {
    // Draw a thin 1px arc from the top (−90°, 12 o'clock position) clockwise.
    // Minimum visible arc: 2° (prevents a lone pixel from appearing before any
    // history has accumulated).
    if (arcExtentDeg < 2.0f) return;
    if (arcExtentDeg > 360.0f) arcExtentDeg = 360.0f;
    _drawArcDeg(64, 32, 24, -90.0f, -90.0f + arcExtentDeg);
}

void DisplayModule::_drawStabilityPulse(float composite_stability,
                                         int cx, int cy) {
    // Filled circle, radius 2–5px, breathing sinusoidally.
    // 1 + 1.5*(1+sin(phase)) gives a range of 1.0 to 4.0, clamped to int 2–5.
    const int r = (int)(2.0f + 1.5f * (1.0f + sinf(_breathePhase)));
    if (r < 1) return;
    _u8g2.drawDisc(cx, cy, r);
}

void DisplayModule::_drawMotifAccent(float lx, float ly) {
    // 3×3 filled square centred at (lx, ly).
    const int ix = (int)lx - 1;
    const int iy = (int)ly - 1;
    _u8g2.drawBox(ix, iy, 3, 3);
}

void DisplayModule::_drawArcDeg(int cx, int cy, int r,
                                  float startDeg, float endDeg) {
    // Pixel-by-pixel arc using 2° steps (fine enough for r=24 without visible gaps).
    // This is in the render path, not the audio path — float trig is acceptable here.
    static constexpr float STEP = 2.0f;
    for (float a = startDeg; a <= endDeg; a += STEP) {
        const float rad = a * (float)M_PI / 180.0f;
        const int x = cx + (int)(r * cosf(rad));
        const int y = cy + (int)(r * sinf(rad));
        _u8g2.drawPixel(x, y);
    }
    // Draw the final pixel at exactly endDeg to close any gap.
    const float rad = endDeg * (float)M_PI / 180.0f;
    _u8g2.drawPixel(cx + (int)(r * cosf(rad)), cy + (int)(r * sinf(rad)));
}

void DisplayModule::_setContrast(uint8_t level, uint32_t now) {
    if (level == _lastContrast) return;
    // Rate-limit: max 2 contrast changes per second (500ms minimum gap).
    if ((now - _lastContrastMs) < 500UL) return;
    _u8g2.setContrast(level);
    _lastContrast   = level;
    _lastContrastMs = now;
}
