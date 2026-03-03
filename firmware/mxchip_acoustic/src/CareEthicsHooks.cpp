#include "CareEthicsHooks.h"
#include <stdio.h>

CareEthicsHooks::CareEthicsHooks()
    : _baseline(nullptr),
      _motif(nullptr),
      _trustAccum(0.f),
      _thresholdHint(0.f),
      _hasThresholdHint(false)
{}

void CareEthicsHooks::bindModules(BaselineAndConfidenceModule* baseline,
                                   MotifModule*                 motif)
{
    _baseline = baseline;
    _motif    = motif;
}

// ---------------------------------------------------------------------------
// Inbound
// ---------------------------------------------------------------------------

void CareEthicsHooks::setLearningPace(float ratio) {
    if (!_baseline) return;

    // Blend suggestion into device's current multiplier (30/70 split).
    // This means a single strong suggestion nudges but doesn't override.
    float current = _baseline->getLearningRateMultiplier();
    float blended = 0.70f * current + 0.30f * ratio;
    _baseline->setLearningRateMultiplier(blended);

    Serial.printf("[care] learning pace: hint=%.2f  applied=%.2f\n",
                  ratio, blended);
}

void CareEthicsHooks::receiveEncouragement(float trustLevel) {
    if (!_baseline) return;

    // Smooth the incoming trust signal (low-pass with α=0.15)
    _trustAccum += (trustLevel - _trustAccum) * 0.15f;

    // Higher accumulated trust → higher inertia (multiplier 1.0–4.0)
    float multiplier = 1.0f + _trustAccum * 3.0f;
    _baseline->setLearningRateMultiplier(multiplier);

    Serial.printf("[care] encouragement: trust=%.2f  inertia×%.2f\n",
                  _trustAccum, multiplier);
}

void CareEthicsHooks::nudgeThreshold(float transientHint) {
    // Store for use during the calibration window (caller checks
    // hasThresholdHint() and applies it if desired during fresh boot).
    _thresholdHint    = transientHint;
    _hasThresholdHint = true;
    Serial.printf("[care] threshold hint: %.2f (held for calibration)\n",
                  transientHint);
}

// ---------------------------------------------------------------------------
// Outbound
// ---------------------------------------------------------------------------

int CareEthicsHooks::emitWellBeingSignal(
    float compositeStability, MotifGrowthState mgs)
{
    if (compositeStability < 0.45f) return 0;  // unsettled
    if (mgs == MGS_DRIFTING || mgs == MGS_STABILIZING) return 1;  // learning
    return 2;  // stable
}

int CareEthicsHooks::emitConceptSignal(
    char* buf, size_t len,
    float rms, float hum, float texture,
    uint8_t vMajor, uint8_t vMinor)
{
    return snprintf(buf, len,
        "{\"type\":\"concept\","
        "\"v\":\"%u.%u\","
        "\"rms\":%.1f,"
        "\"hum\":%.1f,"
        "\"texture\":%.3f}",
        vMajor, vMinor, rms, hum, texture);
}

uint8_t CareEthicsHooks::getSocialPresenceByte(
    float stability, MotifGrowthState mgs, uint8_t vMinor)
{
    uint8_t wb   = (uint8_t)emitWellBeingSignal(stability, mgs);  // 0–2
    uint8_t mgsN = (uint8_t)mgs & 0x03;                           // 0–3
    uint8_t vNib = vMinor & 0x0F;                                  // low nibble
    return (vNib << 4) | (mgsN << 2) | wb;
}

// ---------------------------------------------------------------------------
// Future stubs
// ---------------------------------------------------------------------------

void CareEthicsHooks::onPeerDiscovered(uint8_t peerId,
                                        uint8_t peerPresenceByte)
{
    // TODO: parse peerPresenceByte; if peer is stable and we are unsettled,
    // optionally slow our learning rate slightly (resonance / social anchoring).
    Serial.printf("[care] peer discovered: id=%u presence=0x%02X\n",
                  peerId, peerPresenceByte);
}

void CareEthicsHooks::onPeerFarewell(uint8_t peerId) {
    // TODO: if this peer was a trusted anchor (high encouragement history),
    // gently increase our learning rate to re-adapt without them.
    Serial.printf("[care] peer farewell: id=%u\n", peerId);
}

void CareEthicsHooks::onEcologyStress(float level) {
    // System-wide signal: all agents in the ecology are asked to slow down.
    // We relay it as a learning pace adjustment.
    if (level > 0.1f) {
        float ratio = 1.0f + level * 3.0f;  // stress 1.0 → 4× slower
        setLearningPace(ratio);
        Serial.printf("[care] ecology stress: level=%.2f  pace×%.2f\n",
                      level, ratio);
    }
}
