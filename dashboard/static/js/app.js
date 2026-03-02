/**
 * app.js — shared utilities for the Sensor Ecology Dashboard.
 * Loaded on every page via base.html.
 */

const UI = {

  /**
   * Format an ISO timestamp to a human-friendly relative or absolute string.
   */
  formatTime(isoStr) {
    if (!isoStr) return '—';
    const date   = new Date(isoStr);
    const diffMs = Date.now() - date.getTime();
    const mins   = Math.floor(diffMs / 60_000);
    const hours  = Math.floor(diffMs / 3_600_000);
    if (mins  <  1) return 'just now';
    if (mins  < 60) return `${mins}m ago`;
    if (hours < 24) return `${hours}h ago`;
    return date.toLocaleDateString() + ' ' +
           date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  },

  /**
   * Return a coloured badge span for an observation type string.
   * Extend the `colors` map as new observation types appear.
   */
  typeBadge(type) {
    const colors = {
      // high_bandwidth thermal domain (HighBandwidthPoller labels)
      thermal_shift:      'orange',
      thermal_approach:   'red',
      thermal_retreat:    'yellow',
      thermal_motion:     'orange',
      presence_detected:  'red',
      presence_departed:  'yellow',
      // legacy / observations table labels
      thermal_anomaly:    'red',
      thermal_change:     'orange',
      // other domains
      motion_detected:    'yellow',
      extreme_cold:       'red',
      high_light:         'yellow',
      nominal_conditions: 'green',
      occupancy_detected: 'orange',
      object_identified:  'purple',
      rfid_scan:          'purple',
      // ESP32 motion labels (MPU-6050)
      idle:               'green',
      typing:             'yellow',
      footsteps:          'orange',
      impact:             'red',
      equipment_running:  'orange',
      // ESP32 light labels (TCS34725)
      dark:               'purple',
      dim_warm:           'yellow',
      daylight:           'green',
      overcast:           'yellow',
      screen_dominant:    'purple',
      artificial_warm:    'orange',
      // perceptual domains
      embodied_state:     'orange',
      environmental_field:'green',
      relational_contact: 'yellow',
      high_bandwidth:     'red',
    };
    const cls = colors[type] || '';
    return `<span class="badge ${cls}">${type.replace(/_/g, ' ')}</span>`;
  },

  /**
   * Return a confidence progress-bar + percentage string.
   */
  confBar(value) {
    if (value === null || value === undefined) {
      return '<span class="muted small">—</span>';
    }
    const pct = Math.round(value * 100);
    const cls = value >= 0.80 ? '' : value >= 0.60 ? 'medium' : 'low';
    return `
      <div class="conf-bar">
        <div class="bar">
          <div class="fill ${cls}" style="width:${pct}%"></div>
        </div>
        <span class="small muted">${pct}%</span>
      </div>`;
  },

  /** Replace element content with a loading message. */
  loading(el) {
    el.innerHTML = '<div class="loading">Loading…</div>';
  },

  /** Replace element content with an error message. */
  error(el, msg) {
    el.innerHTML = `<div class="empty-state">⚠ ${msg}</div>`;
  },

  /** Replace element content with an empty-state message. */
  empty(el, msg = 'No data found.') {
    el.innerHTML = `<div class="empty-state">${msg}</div>`;
  },

};


/**
 * Thin fetch wrapper — throws on non-2xx responses.
 */
async function apiFetch(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* noop */ }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}
