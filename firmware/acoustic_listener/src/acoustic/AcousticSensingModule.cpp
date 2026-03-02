#include "AcousticSensingModule.h"
#include <math.h>
#include "driver/i2s.h"

// I2S port assignment — using I2S_NUM_0 exclusively.
static constexpr i2s_port_t I2S_PORT = I2S_NUM_0;

// DMA layout: 8 buffers × 64 samples × 4 bytes = 2048 bytes = 128ms of headroom.
// At 32ms per window the loop can miss several iterations before overflow.
static constexpr int DMA_BUF_COUNT  = 8;
static constexpr int DMA_BUF_FRAMES = 64;   // "frames" ≡ samples for mono

// ── Public ────────────────────────────────────────────────────────────────────

bool AcousticSensingModule::begin() {
    // Biquad bandpass coefficients must be computed before any samples arrive.
    _initBandFilters();

    // Single-pole IIR alpha for hum lowpass at HUM_LOWPASS_FC.
    // Formula: alpha = 1 - exp(-2π·fc/fs)
    _humIirAlpha = 1.0f - expf(-2.0f * (float)M_PI * HUM_LOWPASS_FC / (float)SAMPLE_RATE);

    // ── I2S driver configuration ──────────────────────────────────────────────
    // Using the legacy ESP-IDF I2S driver (driver/i2s.h).
    // Deprecated in IDF 5.x but fully functional; -Wno-deprecated-declarations
    // suppresses the warning in platformio.ini.
    i2s_config_t cfg = {
        .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate          = SAMPLE_RATE,
        .bits_per_sample      = I2S_BITS_PER_SAMPLE_32BIT,
        // ONLY_LEFT: INMP441 pulls SEL/L-R low → left channel.
        // The driver discards the right channel; we get one int32_t per frame.
        .channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count        = DMA_BUF_COUNT,
        .dma_buf_len          = DMA_BUF_FRAMES,
        .use_apll             = false,
        .tx_desc_auto_clear   = false,
        .fixed_mclk           = 0
    };

    esp_err_t err = i2s_driver_install(I2S_PORT, &cfg, 0, nullptr);
    if (err != ESP_OK) return false;

    i2s_pin_config_t pins = {
        .mck_io_num   = I2S_PIN_NO_CHANGE,
        .bck_io_num   = MIC_I2S_SCK,
        .ws_io_num    = MIC_I2S_WS,
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num  = MIC_I2S_SD
    };

    err = i2s_set_pin(I2S_PORT, &pins);
    if (err != ESP_OK) { i2s_driver_uninstall(I2S_PORT); return false; }

    i2s_zero_dma_buffer(I2S_PORT);
    return true;
}

void AcousticSensingModule::sampleI2S() {
    // Backpressure: if the previous window hasn't been consumed, wait.
    // This prevents overwriting a complete window before features are extracted.
    if (_features.window_ready) return;

    // Try to read DMA_CHUNK_SAMPLES 32-bit words, non-blocking (timeout = 0 ticks).
    // i2s_read returns immediately with however many bytes are in the DMA ring.
    static int32_t chunk[DMA_CHUNK_SAMPLES];
    size_t bytes_read = 0;
    i2s_read(I2S_PORT, chunk, sizeof(chunk), &bytes_read, /*ticks_to_wait=*/0);

    const int n = (int)(bytes_read / sizeof(int32_t));

    for (int i = 0; i < n && _accumIdx < WINDOW_SIZE; i++) {
        // INMP441 is 24-bit left-justified in a 32-bit I2S word.
        // Right-shift by 8 brings the significant bits to a 24-bit integer.
        _sampleBuf[_accumIdx++] = chunk[i] >> 8;
    }

    if (_accumIdx >= WINDOW_SIZE) {
        _computeFeatures();
        _accumIdx = 0;
        _features.window_ready = true;
    }
}

const AcousticFeatures& AcousticSensingModule::getFeatures() {
    // Clear the ready flag so sampleI2S() can begin accumulating the next window.
    _features.window_ready = false;
    return _features;
}

// ── Private ───────────────────────────────────────────────────────────────────

void AcousticSensingModule::_initBandFilters() {
    // Initialise 8 biquad constant-peak-gain bandpass filters.
    // Transfer function: H(z) = b0*(1 - z^-2) / (1 + a1*z^-1 + a2*z^-2)
    // Coefficients (after normalising by a0 = 1 + alpha):
    //   b0 = alpha / a0          (b2 = -b0, b1 = 0)
    //   a1 = -2*cos(w0) / a0
    //   a2 = (1 - alpha) / a0
    // where alpha = sin(w0) / (2*Q)
    for (int k = 0; k < SUBBANDS; k++) {
        const float fc = SUBBAND_FC[k];
        const float w0 = 2.0f * (float)M_PI * fc / (float)SAMPLE_RATE;
        const float alpha = sinf(w0) / (2.0f * SUBBAND_Q);
        const float a0    = 1.0f + alpha;
        _bands[k].b0 = alpha / a0;
        _bands[k].a1 = -2.0f * cosf(w0) / a0;
        _bands[k].a2 = (1.0f - alpha) / a0;
        _bands[k].x1 = _bands[k].x2 = 0.0f;
        _bands[k].y1 = _bands[k].y2 = 0.0f;
    }
}

float AcousticSensingModule::_processBiquad(BiquadBP& f, float x) const {
    // Direct Form II transposed for biquad bandpass.
    // y = b0*x + b1*x1 + b2*x2 - a1*y1 - a2*y2
    // With b1=0, b2=-b0:  y = b0*(x - x2) - a1*y1 - a2*y2
    float y = f.b0 * (x - f.x2) - f.a1 * f.y1 - f.a2 * f.y2;
    f.x2 = f.x1;  f.x1 = x;
    f.y2 = f.y1;  f.y1 = y;
    return y;
}

void AcousticSensingModule::_computeFeatures() {
    // Normalisation factor: INMP441 24-bit range is ±2^23. Divide by 2^23 so
    // values are roughly in [-1, 1]. Use float throughout.
    static constexpr float NORM = 1.0f / (float)(1 << 23);

    // Pass 1: RMS, variance, peak_abs, hum energy, sub-band energies.
    // All computed in a single pass over WINDOW_SIZE samples to minimise cache misses.
    float sum   = 0.0f;
    float sum2  = 0.0f;
    float peak  = 0.0f;
    float humAcc = 0.0f;           // accumulates squared hum-filtered samples
    float bandEnergy[SUBBANDS] = {};

    for (int i = 0; i < WINDOW_SIZE; i++) {
        const float x = (float)_sampleBuf[i] * NORM;

        // Broadband statistics
        sum  += x;
        sum2 += x * x;
        const float ax = fabsf(x);
        if (ax > peak) peak = ax;

        // Hum IIR lowpass (single-pole, state maintained between windows)
        _humIirState = _humIirAlpha * x + (1.0f - _humIirAlpha) * _humIirState;
        humAcc += _humIirState * _humIirState;

        // 8 biquad bandpass filters — each processes x independently
        for (int k = 0; k < SUBBANDS; k++) {
            float y = _processBiquad(_bands[k], x);
            bandEnergy[k] += y * y;
        }
    }

    const float n = (float)WINDOW_SIZE;

    // RMS and variance
    const float mean = sum / n;
    _features.rms      = sqrtf(sum2 / n);
    _features.variance = (sum2 / n) - (mean * mean);
    if (_features.variance < 0.0f) _features.variance = 0.0f;  // float guard

    // Hum energy: RMS of the lowpass-filtered signal
    _features.hum_energy = sqrtf(humAcc / n);

    // Transient ratio: peak_abs / rms (dimensionless)
    _features.transient_ratio = (_features.rms > CONF_EPSILON)
        ? (peak / _features.rms) : 0.0f;

    // Spectral flatness (Wiener entropy proxy) via log-sum trick.
    // Geometric mean / arithmetic mean of 8 band energies.
    // To avoid log(0): add a tiny epsilon to each band before taking log.
    static constexpr float BAND_EPS = 1e-10f;
    float logSum  = 0.0f;
    float arithSum = 0.0f;
    for (int k = 0; k < SUBBANDS; k++) {
        const float e = bandEnergy[k] / n + BAND_EPS;  // average band power
        logSum  += logf(e);
        arithSum += e;
    }
    const float geomMean  = expf(logSum / (float)SUBBANDS);
    const float arithMean = arithSum / (float)SUBBANDS;
    _features.spectral_flatness = (arithMean > BAND_EPS)
        ? (geomMean / arithMean) : 0.0f;
    // Clamp to [0,1] (ratio can exceed 1 due to floating point)
    if (_features.spectral_flatness > 1.0f) _features.spectral_flatness = 1.0f;
}
