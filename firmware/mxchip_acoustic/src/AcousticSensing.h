#pragma once
#include <Arduino.h>
#include "config.h"

// ---------------------------------------------------------------------------
// AcousticFeatures
// Compact feature vector computed over a sliding window of audio samples.
// All values are in natural units of the 16-bit PCM signal (range ±32767).
// ---------------------------------------------------------------------------
struct AcousticFeatures {
    float rms;              // Root-mean-square amplitude — proxy for noise floor
    float variance;         // Sample variance — sensitive to dynamic range
    float hum_level;        // Low-frequency energy envelope (IIR-smoothed)
    float texture;          // Broadband texture proxy: normalised zero-crossing rate
    float transient_ratio;  // Peak / RMS — high values indicate sharp transients
    bool  valid;            // True once the first complete window is computed
};

// ---------------------------------------------------------------------------
// AcousticSensingModule
//
// Driven by AudioClass DMA callbacks. Each callback delivers one
// AUDIO_CHUNK_BYTES (512-byte / 256-sample) chunk; the module accumulates
// FEATURE_WINDOW_CHUNKS chunks, then computes a fresh AcousticFeatures vector.
//
// Thread-safety note: onAudioChunk() is called from the DMA ISR context.
// The module uses a double-buffer swap so that getFeatures() is safe to call
// from the main loop without a lock on Cortex-M4.
// ---------------------------------------------------------------------------
class AcousticSensingModule {
public:
    AcousticSensingModule();

    // Initialise and start DMA recording. Call once in setup().
    void begin();

    // Called from the DMA callback — must be as fast as possible.
    void onAudioChunk(const int16_t* samples, int n);

    // Main-loop accessors — non-blocking.
    AcousticFeatures getFeatures()  const { return _features; }
    bool             isFeatureReady() const { return _featureReady; }
    void             consumeFeature()       { _featureReady = false; }

private:
    void computeFeatures();

    // Accumulation buffer — sized for one full feature window.
    int16_t _window[FEATURE_WINDOW_SAMPLES];
    int     _windowFill;

    // IIR hum filter state (persists between windows for continuity).
    float   _humFilter;

    // DC-block filter state.
    float   _dcFilter;

    AcousticFeatures _features;
    volatile bool    _featureReady;
};

// Free function used as the AudioClass DMA callback.
// Must be defined in AcousticSensing.cpp and registered via begin().
void acousticDmaCallback();
