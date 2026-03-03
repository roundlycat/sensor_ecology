#include "AcousticSensing.h"
#include <AudioClassV2.h>
#include <math.h>
#include <string.h>

// ---------------------------------------------------------------------------
// DMA bridge
// A module-scoped pointer lets the free-function callback reach the instance.
// ---------------------------------------------------------------------------
static AcousticSensingModule* g_acousticSelf = nullptr;
static char                   g_dmaBuf[AUDIO_CHUNK_BYTES];

void acousticDmaCallback() {
    // Copy out of the driver ring buffer — must be fast.
    AudioClass::getInstance().readFromRecordBuffer(g_dmaBuf, AUDIO_CHUNK_BYTES);
    if (g_acousticSelf) {
        g_acousticSelf->onAudioChunk(
            reinterpret_cast<const int16_t*>(g_dmaBuf),
            AUDIO_CHUNK_SAMPLES);
    }
}

// ---------------------------------------------------------------------------
AcousticSensingModule::AcousticSensingModule()
    : _windowFill(0), _humFilter(0.f), _dcFilter(0.f), _featureReady(false)
{
    memset(_window,   0, sizeof(_window));
    _features = { 0.f, 0.f, 0.f, 0.f, 0.f, false };
    g_acousticSelf = this;
}

void AcousticSensingModule::begin() {
    AudioClass& audio = AudioClass::getInstance();
    audio.format(AUDIO_SAMPLE_RATE, AUDIO_BITS);
    audio.startRecord(acousticDmaCallback);
    Serial.println("[audio] recording started @ 8 kHz 16-bit mono");
}

// Called from DMA ISR — keep it tight.
void AcousticSensingModule::onAudioChunk(const int16_t* samples, int n) {
    int space  = FEATURE_WINDOW_SAMPLES - _windowFill;
    int toCopy = (n < space) ? n : space;
    memcpy(_window + _windowFill, samples, toCopy * sizeof(int16_t));
    _windowFill += toCopy;

    if (_windowFill >= FEATURE_WINDOW_SAMPLES) {
        computeFeatures();
        _windowFill  = 0;
        _featureReady = true;
    }
}

void AcousticSensingModule::computeFeatures() {
    const int N = FEATURE_WINDOW_SAMPLES;

    double sumSq    = 0.0;
    double sum      = 0.0;
    int32_t peak    = 0;
    int     zeroCrossings = 0;
    float   humAccum = 0.f;

    // Previous dc-blocked sample (for zero-crossing detection)
    float prevDcBlocked = 0.f;

    for (int i = 0; i < N; i++) {
        float x = (float)_window[i];

        // --- Leaky DC-block (simple one-pole HPF, pole at 0.995) ---
        // y[n] = x[n] - x[n-1] + 0.995*y[n-1]  approximated as:
        // dcFilter tracks a slow-moving mean; dc_blocked = x - dcFilter
        _dcFilter += (x - _dcFilter) * 0.005f;
        float dc = x - _dcFilter;

        sum   += dc;
        sumSq += (double)dc * dc;

        // Peak (absolute, on raw sample to avoid filter transients)
        int32_t absRaw = _window[i] < 0 ? -(int32_t)_window[i] : (int32_t)_window[i];
        if (absRaw > peak) peak = absRaw;

        // Zero crossings on dc-blocked signal
        if (i > 0) {
            if ((dc > 0.f && prevDcBlocked <= 0.f) ||
                (dc < 0.f && prevDcBlocked >= 0.f)) {
                zeroCrossings++;
            }
        }
        prevDcBlocked = dc;

        // Hum envelope: track absolute amplitude through a long-window IIR.
        // The hum filter has its own slower alpha (HUM_ALPHA from config).
        // We accumulate the filtered value to give a window-mean hum level.
        _humFilter += (fabsf(dc) - _humFilter) * HUM_ALPHA;
        humAccum   += _humFilter;
    }

    double mean   = sum / N;
    double meanSq = sumSq / N;

    float rms = (float)sqrt(meanSq);
    // Variance = E[x²] - E[x]²  (DC-blocked so E[x] ≈ 0, but compute properly)
    float var = (float)(meanSq - mean * mean);
    if (var < 0.f) var = 0.f;

    // Zero-crossing rate, normalised to [0,1].
    // Theoretical max = N-1 crossings (alternating sign every sample = Nyquist).
    // Practical noise ~ 0.4–0.6; pure tone ~ 0.01–0.05.
    float zcr     = (float)zeroCrossings / (float)(N - 1);
    float texture = zcr * 2.0f;   // rescale so 0.5 ZCR → 1.0
    if (texture > 1.0f) texture = 1.0f;

    // Transient ratio: peak relative to RMS.
    // Pure tone → ~1.41 (√2). Impulsive transients → 5–20.
    float transient = (rms > 1.f) ? ((float)peak / rms) : 0.f;

    _features.rms            = rms;
    _features.variance       = var;
    _features.hum_level      = humAccum / (float)N;
    _features.texture        = texture;
    _features.transient_ratio = transient;
    _features.valid          = true;
}
