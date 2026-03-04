// AgentPerceptionVisualizer.cs
// AR rendering layer. Spawns and animates visual representations of
// perceptual events in world space around the agent's physical location.
//
// Designed for Samsung Galaxy Tab A9+ with AR Foundation.
// The agent node is anchored at a tracked image (QR/ArUco on the Pi 5 case)
// or a manual world anchor set at first run.
//
// Place in: Assets/AgentPerception/Visualization/

using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.XR.ARFoundation;

namespace AgentPerception
{
    /// <summary>
    /// Spawns particle bursts, glyphs, and pulse rings for each perceptual event.
    /// Subscribes to PerceptualEventBus.
    /// </summary>
    public class AgentPerceptionVisualizer : MonoBehaviour
    {
        // -----------------------------------------------------------------------
        // Inspector references
        // -----------------------------------------------------------------------

        [Header("Agent Anchor")]
        [SerializeField] Transform _agentAnchor;     // set to tracked image transform
        [SerializeField] float     _orbitRadius = 0.18f;

        [Header("Prefabs")]
        [SerializeField] GameObject _eventGlyphPrefab;       // floating label + icon
        [SerializeField] GameObject _pulseRingPrefab;        // expanding ring on event
        [SerializeField] GameObject _motifResonancePrefab;   // special glyph for recurrences
        [SerializeField] GameObject _candidateGlyphPrefab;   // unlabelled novelty marker
        [SerializeField] ParticleSystem _ambientParticles;   // background field particles

        [Header("Timing")]
        [SerializeField] float _glyphLifetimeS    = 6.0f;
        [SerializeField] float _pulseLifetimeS    = 1.5f;
        [SerializeField] float _spawnCooldownS    = 0.2f;   // prevent visual flooding
        [SerializeField] int   _maxActiveGlyphs   = 12;

        [Header("Metabolic Skin")]
        [SerializeField] Renderer _agentBodyRenderer;        // the AR agent body mesh
        [SerializeField] string   _tempColorProperty = "_EmissionColor";
        [SerializeField] Gradient _thermalGradient;          // cool blue -> hot orange
        [SerializeField] float    _tempMin = 40f;
        [SerializeField] float    _tempMax = 80f;

        // -----------------------------------------------------------------------
        // Internal state
        // -----------------------------------------------------------------------

        readonly Queue<EnrichedEvent>   _eventQueue   = new();
        readonly List<GameObject>       _activeGlyphs = new();
        PerceptualEventBus              _bus;
        float                           _lastSpawnT;
        AgentVitals                     _currentVitals;

        // Orbital placement — distribute glyphs around the agent
        int _orbitSlot = 0;
        static readonly float[] _orbitAngles = { 0, 60, 120, 180, 240, 300 };

        // -----------------------------------------------------------------------
        // Lifecycle
        // -----------------------------------------------------------------------

        void OnEnable()
        {
            _bus = PerceptualEventBus.Instance;
            if (_bus == null) { Debug.LogError("[Visualizer] No PerceptualEventBus found."); return; }

            _bus.OnPerceptualEvent   += EnqueueEvent;
            _bus.OnVitalsUpdate      += UpdateMetabolicSkin;
            _bus.OnConnectionState   += HandleConnectionChange;
        }

        void OnDisable()
        {
            if (_bus == null) return;
            _bus.OnPerceptualEvent   -= EnqueueEvent;
            _bus.OnVitalsUpdate      -= UpdateMetabolicSkin;
            _bus.OnConnectionState   -= HandleConnectionChange;
        }

        void Update()
        {
            // Drain queue with cooldown to avoid visual overwhelm
            if (_eventQueue.Count > 0 && Time.time - _lastSpawnT > _spawnCooldownS)
            {
                SpawnEvent(_eventQueue.Dequeue());
                _lastSpawnT = Time.time;
            }

            // Cull expired glyphs
            _activeGlyphs.RemoveAll(g => g == null);
        }

        // -----------------------------------------------------------------------
        // Event queueing
        // -----------------------------------------------------------------------

        void EnqueueEvent(EnrichedEvent ev)
        {
            // Drop low-confidence events if queue is already backed up
            if (_eventQueue.Count > 5 && ev.Source.ConfidenceEnum == FusionConfidence.Low)
                return;

            _eventQueue.Enqueue(ev);
        }

        // -----------------------------------------------------------------------
        // Spawning
        // -----------------------------------------------------------------------

        void SpawnEvent(EnrichedEvent ev)
        {
            if (_agentAnchor == null) return;

            // Enforce glyph cap — remove oldest
            while (_activeGlyphs.Count >= _maxActiveGlyphs && _activeGlyphs.Count > 0)
            {
                var oldest = _activeGlyphs[0];
                _activeGlyphs.RemoveAt(0);
                if (oldest != null) Destroy(oldest);
            }

            var spawnPos = OrbitalPosition();
            SpawnPulseRing(spawnPos, ev.DomainColor);
            SpawnGlyph(spawnPos, ev);
        }

        Vector3 OrbitalPosition()
        {
            float angle = _orbitAngles[_orbitSlot % _orbitAngles.Length] * Mathf.Deg2Rad;
            _orbitSlot++;

            var offset = new Vector3(
                Mathf.Cos(angle) * _orbitRadius,
                0.05f + UnityEngine.Random.Range(0f, 0.05f),
                Mathf.Sin(angle) * _orbitRadius
            );
            return _agentAnchor.position + offset;
        }

        void SpawnPulseRing(Vector3 position, Color color)
        {
            if (_pulseRingPrefab == null) return;

            var ring = Instantiate(_pulseRingPrefab, position, Quaternion.identity);
            var renderer = ring.GetComponentInChildren<Renderer>();
            if (renderer != null)
                renderer.material.color = color;

            StartCoroutine(ExpandAndFade(ring, _pulseLifetimeS));
        }

        void SpawnGlyph(Vector3 position, EnrichedEvent ev)
        {
            // Choose prefab based on resonance type
            GameObject prefab;
            if (ev.Source.NearestResonance?.ResonanceTypeEnum == ResonanceType.Recurrence)
                prefab = _motifResonancePrefab ?? _eventGlyphPrefab;
            else if (ev.Source.NearestResonance?.ResonanceTypeEnum == ResonanceType.Candidate)
                prefab = _candidateGlyphPrefab ?? _eventGlyphPrefab;
            else
                prefab = _eventGlyphPrefab;

            if (prefab == null) return;

            var glyph = Instantiate(prefab, position, Quaternion.identity, _agentAnchor);
            _activeGlyphs.Add(glyph);

            // Apply colour and label
            var glyphBehaviour = glyph.GetComponent<EventGlyphBehaviour>();
            if (glyphBehaviour != null)
                glyphBehaviour.Initialise(ev);

            // Scale by intensity weight
            float scale = Mathf.Lerp(0.6f, 1.2f, ev.IntensityWeight);
            glyph.transform.localScale = Vector3.one * scale;

            StartCoroutine(FloatAndFade(glyph, _glyphLifetimeS));
        }

        // -----------------------------------------------------------------------
        // Metabolic skin
        // Updates the agent body material to reflect current thermal state
        // -----------------------------------------------------------------------

        void UpdateMetabolicSkin(AgentVitals vitals)
        {
            _currentVitals = vitals;

            if (_agentBodyRenderer == null) return;

            float t = Mathf.InverseLerp(_tempMin, _tempMax, vitals.temp_c);
            Color thermalColor = _thermalGradient.Evaluate(t);

            _agentBodyRenderer.material.SetColor(_tempColorProperty, thermalColor);

            // Pulse ambient particles faster when CPU load is high
            if (_ambientParticles != null)
            {
                var emission = _ambientParticles.emission;
                emission.rateOverTime = Mathf.Lerp(5f, 40f, vitals.cpu_load_pct / 100f);
            }
        }

        // -----------------------------------------------------------------------
        // Connection indicator
        // -----------------------------------------------------------------------

        void HandleConnectionChange(bool connected)
        {
            if (_ambientParticles == null) return;
            if (connected)
                _ambientParticles.Play();
            else
                _ambientParticles.Pause();
        }

        // -----------------------------------------------------------------------
        // Animations
        // -----------------------------------------------------------------------

        IEnumerator ExpandAndFade(GameObject obj, float duration)
        {
            if (obj == null) yield break;
            var startScale = obj.transform.localScale;
            var targetScale = startScale * 3f;
            var renderer = obj.GetComponentInChildren<Renderer>();
            float t = 0;
            while (t < 1f)
            {
                if (obj == null) yield break;
                t += Time.deltaTime / duration;
                obj.transform.localScale = Vector3.Lerp(startScale, targetScale, t);
                if (renderer != null)
                {
                    var c = renderer.material.color;
                    renderer.material.color = new Color(c.r, c.g, c.b, Mathf.Lerp(0.8f, 0f, t));
                }
                yield return null;
            }
            if (obj != null) Destroy(obj);
        }

        IEnumerator FloatAndFade(GameObject obj, float lifetime)
        {
            if (obj == null) yield break;
            var startPos = obj.transform.position;
            var renderer = obj.GetComponentInChildren<Renderer>();
            var startColor = renderer != null ? renderer.material.color : Color.white;

            float t = 0;
            while (t < 1f)
            {
                if (obj == null) yield break;
                t += Time.deltaTime / lifetime;
                // Gentle upward drift
                obj.transform.position = startPos + Vector3.up * (t * 0.06f);
                // Fade out in last 30%
                if (renderer != null && t > 0.7f)
                {
                    float alpha = Mathf.Lerp(1f, 0f, (t - 0.7f) / 0.3f);
                    var c = startColor;
                    renderer.material.color = new Color(c.r, c.g, c.b, alpha);
                }
                // Always face camera (billboard)
                if (Camera.main != null)
                    obj.transform.LookAt(Camera.main.transform.position);
                yield return null;
            }
            if (obj != null)
            {
                _activeGlyphs.Remove(obj);
                Destroy(obj);
            }
        }
    }

    // -----------------------------------------------------------------------
    // Per-glyph behaviour component
    // Attach to the EventGlyph prefab root
    // -----------------------------------------------------------------------

    public class EventGlyphBehaviour : MonoBehaviour
    {
        [SerializeField] TMPro.TextMeshPro _labelText;
        [SerializeField] Renderer          _iconRenderer;
        [SerializeField] Renderer          _backgroundRenderer;

        public void Initialise(EnrichedEvent ev)
        {
            if (_labelText != null)
                _labelText.text = ev.DisplayLabel;

            if (_iconRenderer != null)
                _iconRenderer.material.color = ev.DomainColor;

            if (_backgroundRenderer != null)
            {
                var bg = ev.DomainColor;
                bg.a = 0.15f + ev.IntensityWeight * 0.25f;
                _backgroundRenderer.material.color = bg;
            }
        }
    }
}
