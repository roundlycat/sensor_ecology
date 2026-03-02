#pragma once
#include <Arduino.h>
#include "../config.h"

// AcousticFeatures — the reduced acoustic description of one 512-sample window.
// All fields are normalised floats; downstream modules should not need raw samples.
struct AcousticFeatures {
    float rms;               // Root mean square of window (after 24-bit normalisation)
    float variance;          // Variance of window
    float hum_energy;        // RMS of signal below ~200 Hz (single-pole IIR lowpass)
    float spectral_flatness; // Wiener entropy proxy: geometric/arithmetic mean of
                             // 8 sub-band energies. 1.0 = white noise, 0.0 = tonal.
    float transient_ratio;   // peak_abs / rms — dimensionless impulsiveness measure
    bool  window_ready;      // Set true when fresh features are available
};

// ── Biquad bandpass filter state (per sub-band) ───────────────────────────────
// Transfer function: H(z) = b0*(1 - z^-2) / (1 + a1*z^-1 + a2*z^-2)
// i.e. b1=0, b2=-b0 for constant-peak-gain bandpass.
struct BiquadBP {
    float b0;   // b2 = -b0, b1 = 0 (constant after begin())
    float a1;   // IIR feedback (constant after begin())
    float a2;   // IIR feedback (constant after begin())
    float x1, x2;  // input delay line (state, updated per sample)
    float y1, y2;  // output delay line (state, updated per sample)
};

// AcousticSensingModule
// Responsibilities:
//   1. Drive the INMP441 I2S mic via ESP32 legacy DMA driver (non-blocking poll).
//   2. Accumulate samples into a 512-sample window.
//   3. When a window is full, compute AcousticFeatures atomically.
//   4. Signal consumers via windowReady() / getFeatures().
//
// Design constraints (hard):
//   - No heap allocation after begin().
//   - No delay() or blocking calls.
//   - computeFeatures() runs only inside the cooperative loop, never in an ISR.
class AcousticSensingModule {
public:
    AcousticSensingModule() = default;

    // Call once in setup(). Returns false if I2S driver install fails.
    bool begin();

    // Non-blocking DMA poll — call every loop().
    // Reads up to DMA_CHUNK_SAMPLES from the I2S DMA ring buffer and
    // appends them to the accumulation window. When the window is full,
    // computeFeatures() runs and windowReady() becomes true.
    // If windowReady() is already true, this call is a no-op (backpressure).
    void sampleI2S();

    // True when a fresh, complete window has been computed.
    bool windowReady() const { return _features.window_ready; }

    // Return the current features and CLEAR the ready flag.
    // Call only when windowReady() is true.
    const AcousticFeatures& getFeatures();

private:
    // ── I2S accumulation buffer ───────────────────────────────────────────────
    // 32-bit I2S words from INMP441, right-shifted 8 to use 24 significant bits.
    int32_t _sampleBuf[WINDOW_SIZE];
    int     _accumIdx = 0;

    // ── Feature output (computed at end of each window) ───────────────────────
    AcousticFeatures _features = {};

    // ── Hum energy IIR state ──────────────────────────────────────────────────
    // Single-pole lowpass at HUM_LOWPASS_FC. Maintained between windows so
    // the filter is continuously warm, not re-initialised each window.
    float _humIirState  = 0.0f;
    float _humIirAlpha  = 0.0f;   // set in begin(): 1 - exp(-2π*fc/fs)

    // ── 8 biquad bandpass filters for sub-band spectral flatness ─────────────
    BiquadBP _bands[SUBBANDS];

    // ── Internal helpers ──────────────────────────────────────────────────────
    void _computeFeatures();
    void _initBandFilters();
    float _processBiquad(BiquadBP& f, float x) const;
};
