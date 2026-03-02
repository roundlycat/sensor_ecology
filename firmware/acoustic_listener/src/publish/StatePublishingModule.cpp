#include "StatePublishingModule.h"

// ── Constructor ───────────────────────────────────────────────────────────────

StatePublishingModule::StatePublishingModule(IPublisher& publisher)
    : _pub(publisher) {}

// ── Public ────────────────────────────────────────────────────────────────────

void StatePublishingModule::maybeSendStatus(uint32_t now,
                                             const BaselineSnapshot& b,
                                             MotifGrowthState state,
                                             const MemoryBlock& mem) {
    const uint32_t interval = _pickStatusInterval(b, mem, now);
    if ((now - _lastStatusMs) < interval) return;
    _lastStatusMs = now;

    // Build status JSON.
    // StaticJsonDocument size: ~300 bytes, well within 512-byte default stack.
    StaticJsonDocument<320> doc;
    doc["type"]               = "status";
    doc["v"]                  = FIRMWARE_VERSION_STR;
    doc["baseline_conf"]      = (float)((int)(b.baseline_confidence   * 100 + 0.5f)) / 100.0f;
    doc["hum_conf"]           = (float)((int)(b.hum_confidence        * 100 + 0.5f)) / 100.0f;
    doc["broadband_conf"]     = (float)((int)(b.broadband_confidence  * 100 + 0.5f)) / 100.0f;
    doc["transient_clarity"]  = (float)((int)(b.transient_clarity     * 100 + 0.5f)) / 100.0f;
    doc["motif_state"]        = motifStateStr(state);
    doc["composite_stability"]= (float)((int)(b.composite_stability   * 100 + 0.5f)) / 100.0f;
    doc["windows_seen"]       = mem.total_windows_seen;
    doc["uptime_ms"]          = now;
    doc["ver_minor"]          = mem.version_minor;

    char buf[320];
    serializeJson(doc, buf, sizeof(buf));
    _pub.publish(TOPIC_STATUS, buf);
}

void StatePublishingModule::maybeSendMotifEvent(uint32_t now,
                                                 const MotifCandidate* promoted,
                                                 int promoted_idx,
                                                 bool has_event) {
    if (!has_event || promoted == nullptr) return;
    // Rate-limit motif events to at most one per second (shouldn't matter in
    // practice since promotions are rare, but guards against edge cases).
    if ((now - _lastMotifMs) < 1000UL) return;
    _lastMotifMs = now;

    StaticJsonDocument<280> doc;
    doc["type"]            = "motif_event";
    doc["event"]           = "promoted";
    doc["motif_index"]     = promoted_idx;
    doc["rms"]             = (float)((int)(promoted->rms             * 10000 + 0.5f)) / 10000.0f;
    doc["variance"]        = (float)((int)(promoted->variance        * 10000 + 0.5f)) / 10000.0f;
    doc["hum_energy"]      = (float)((int)(promoted->hum_energy      * 10000 + 0.5f)) / 10000.0f;
    doc["transient_ratio"] = (float)((int)(promoted->transient_ratio * 100   + 0.5f)) / 100.0f;
    doc["stability_count"] = promoted->stability_count;

    char buf[280];
    serializeJson(doc, buf, sizeof(buf));
    _pub.publish(TOPIC_MOTIF, buf);
}

// ── Private ───────────────────────────────────────────────────────────────────

uint32_t StatePublishingModule::_pickStatusInterval(const BaselineSnapshot& b,
                                                      const MemoryBlock& mem,
                                                      uint32_t now) const {
    // Early-life: frequent status so the dashboard tracks startup quickly.
    const bool earlyLife = (mem.version_minor < 3) || (now < 300000UL);
    if (earlyLife) return STATUS_FAST_INTERVAL_MS;

    // Mature and stable: go quiet.
    if (b.composite_stability > 0.8f) return STATUS_SLOW_INTERVAL_MS;

    // Default: mid cadence.
    return STATUS_MID_INTERVAL_MS;
}
