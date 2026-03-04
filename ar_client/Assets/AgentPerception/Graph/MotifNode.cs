// MotifNode.cs
// Per-node behaviour attached to each motif prefab instance in the AR graph.
// Handles visual state, tap interaction, domain breakdown ring, and
// registration with MotifResonanceRenderer.
//
// Place in: Assets/AgentPerception/Graph/

using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.EventSystems;

namespace AgentPerception
{
    /// <summary>
    /// Data container for a motif as received from /api/motifs.
    /// </summary>
    [Serializable]
    public class MotifData
    {
        public string id;
        public string label;
        public int    recurrence_count;
        public string last_resonance_at;
        public string dominant_domain;
        public bool   has_embedding;

        [NonSerialized] public SensorDomain DominantDomainEnum;
        [NonSerialized] public DateTime     LastResonanceAt;

        public void Hydrate()
        {
            DominantDomainEnum = dominant_domain switch
            {
                "environmental_field" => SensorDomain.EnvironmentalField,
                "embodied_state"      => SensorDomain.EmbodiedState,
                "relational_contact"  => SensorDomain.RelationalContact,
                "high_bandwidth"      => SensorDomain.HighBandwidth,
                _                     => SensorDomain.Unknown
            };
            if (DateTime.TryParse(last_resonance_at, out var dt))
                LastResonanceAt = dt.ToLocalTime();
        }
    }

    [Serializable]
    public class MotifListResponse
    {
        public MotifData[] motifs;
        public int         total;
        public bool        bootstrap;   // true when the motifs table is empty
    }

    [Serializable]
    public class DomainBreakdownItem
    {
        public string domain;
        public int    count;
        public float  avg_distance;
    }

    [Serializable]
    public class MotifStatsResponse
    {
        public string                  motif_id;
        public DomainBreakdownItem[]   domain_breakdown;
    }

    // -----------------------------------------------------------------------

    public class MotifNode : MonoBehaviour
    {
        // -----------------------------------------------------------------------
        // Inspector — wire up in the MotifNode prefab
        // -----------------------------------------------------------------------

        [Header("Visual Components")]
        [SerializeField] Renderer               _coreRenderer;       // the central sphere
        [SerializeField] Renderer               _glowRenderer;       // outer glow shell
        [SerializeField] TMPro.TextMeshPro      _labelText;
        [SerializeField] TMPro.TextMeshPro      _countText;
        [SerializeField] Transform              _ringContainer;      // parent for domain rings
        [SerializeField] GameObject             _ringSegmentPrefab;  // thin arc segment

        [Header("Interaction")]
        [SerializeField] float                  _hoverScale     = 1.15f;
        [SerializeField] float                  _selectedScale  = 1.30f;
        [SerializeField] AudioSource            _tapSound;

        // -----------------------------------------------------------------------
        // Public state
        // -----------------------------------------------------------------------

        public MotifData Data        { get; private set; }
        public bool      IsSelected  { get; private set; }

        // -----------------------------------------------------------------------
        // Internal
        // -----------------------------------------------------------------------

        MotifResonanceRenderer  _resonanceRenderer;
        PerceptualEventClient   _client;
        PerceptualEventBus      _bus;
        Vector3                 _baseScale;
        Coroutine               _pulseCoroutine;
        List<Renderer>          _ringSegments = new();

        // Domain → colour mapping (mirrors PerceptualEventBus palette)
        static readonly Dictionary<string, Color> DomainColors = new()
        {
            { "environmental_field", new Color(0.20f, 0.80f, 0.45f) },
            { "embodied_state",      new Color(0.90f, 0.55f, 0.20f) },
            { "relational_contact",  new Color(0.40f, 0.60f, 0.95f) },
            { "high_bandwidth",      new Color(0.85f, 0.30f, 0.75f) },
        };

        // -----------------------------------------------------------------------
        // Initialisation — called by MotifGraphScene after instantiation
        // -----------------------------------------------------------------------

        public void Initialise(
            MotifData               data,
            MotifResonanceRenderer  resonanceRenderer,
            PerceptualEventClient   client,
            PerceptualEventBus      bus)
        {
            Data               = data;
            _resonanceRenderer = resonanceRenderer;
            _client            = client;
            _bus               = bus;
            _baseScale         = transform.localScale;

            data.Hydrate();
            ApplyBaseVisuals();

            // Register with resonance renderer so arcs can target this transform
            resonanceRenderer?.RegisterMotifNode(data.id, transform);

            // Name the GameObject with the event id so FindGlyphForEvent can locate it
            gameObject.name = data.id;

            // Subscribe to live recurrence events
            if (_bus != null)
                _bus.OnMotifRecurrence += OnRecurrenceArrived;
        }

        void OnDestroy()
        {
            _resonanceRenderer?.UnregisterMotifNode(Data?.id);
            if (_bus != null)
                _bus.OnMotifRecurrence -= OnRecurrenceArrived;
        }

        // -----------------------------------------------------------------------
        // Visual setup
        // -----------------------------------------------------------------------

        void ApplyBaseVisuals()
        {
            // Core colour from dominant domain
            if (_coreRenderer != null)
            {
                DomainColors.TryGetValue(Data.dominant_domain ?? "", out var domColor);
                if (domColor == default) domColor = new Color(0.5f, 0.5f, 0.6f);
                _coreRenderer.material.color = domColor;
            }

            // Scale node size by recurrence count (log scale to prevent monsters)
            float sizeMultiplier = 1f + Mathf.Log(1 + Data.recurrence_count) * 0.12f;
            transform.localScale = _baseScale * sizeMultiplier;
            _baseScale = transform.localScale;

            // Label
            if (_labelText != null)
                _labelText.text = Data.label ?? Data.id[..8];

            if (_countText != null)
                _countText.text = Data.recurrence_count > 0
                    ? $"↺ {Data.recurrence_count}"
                    : "";

            // Fetch domain breakdown and build ring
            if (_client != null && Data.has_embedding)
                _client.RequestEchoes(Data.id, 0.4f, _ => { });   // warm cache
            StartCoroutine(FetchAndBuildRing());
        }

        IEnumerator FetchAndBuildRing()
        {
            // Small delay so the graph finishes placing nodes before we fire requests
            yield return new WaitForSeconds(UnityEngine.Random.Range(0.5f, 2.0f));

            if (_client == null || _ringContainer == null || _ringSegmentPrefab == null)
                yield break;

            // Use the echo endpoint as a proxy for breakdown data
            bool done = false;
            string json = null;
            _client.RequestEchoes(Data.id, 0.4f, result => { json = result; done = true; });
            yield return new WaitUntil(() => done);

            if (string.IsNullOrEmpty(json)) yield break;

            // Count domain occurrences from echo results
            var stats = JsonUtility.FromJson<EchoResponse>(json);
            if (stats?.echoes == null || stats.echoes.Length == 0) yield break;

            var domainCounts = new Dictionary<string, int>();
            foreach (var echo in stats.echoes)
            {
                if (!domainCounts.ContainsKey(echo.domain))
                    domainCounts[echo.domain] = 0;
                domainCounts[echo.domain]++;
            }

            BuildDomainRing(domainCounts);
        }

        void BuildDomainRing(Dictionary<string, int> domainCounts)
        {
            // Clear existing
            foreach (Transform child in _ringContainer) Destroy(child.gameObject);
            _ringSegments.Clear();

            int total = 0;
            foreach (var kv in domainCounts) total += kv.Value;
            if (total == 0) return;

            // Reserve 2° of gap between each segment
            float gapDeg     = 2f;
            float totalGap   = gapDeg * domainCounts.Count;
            float sweepBudget = 360f - totalGap;

            float startAngle = 0f;
            foreach (var kv in domainCounts)
            {
                float fraction = (float)kv.Value / total;
                float sweepDeg = fraction * sweepBudget;

                var seg = Instantiate(_ringSegmentPrefab, _ringContainer);
                seg.transform.localPosition    = Vector3.zero;
                seg.transform.localRotation    = Quaternion.identity;
                seg.transform.localScale       = Vector3.one;

                // Drive the arc mesh component
                var arc = seg.GetComponent<ProceduralArcMesh>();
                if (arc != null)
                {
                    arc.SetArc(startAngle, sweepDeg);
                    DomainColors.TryGetValue(kv.Key, out var arcColor);
                    arc.SetColor(arcColor != default ? arcColor : Color.gray);
                }
                else
                {
                    // Fallback: just tint the renderer directly
                    DomainColors.TryGetValue(kv.Key, out var fallbackColor);
                    var r = seg.GetComponent<Renderer>();
                    if (r != null)
                    {
                        _ringSegments.Add(r);
                        r.material.color = fallbackColor != default ? fallbackColor : Color.gray;
                    }
                }

                startAngle += sweepDeg + gapDeg;
            }
        }

        // -----------------------------------------------------------------------
        // Live recurrence response
        // -----------------------------------------------------------------------

        void OnRecurrenceArrived(EnrichedEvent ev)
        {
            if (ev.Source.NearestResonance?.motif_id != Data.id) return;

            // Increment count display
            Data.recurrence_count++;
            if (_countText != null)
                _countText.text = $"↺ {Data.recurrence_count}";

            // Pulse the node
            if (_pulseCoroutine != null) StopCoroutine(_pulseCoroutine);
            _pulseCoroutine = StartCoroutine(PulseScale());
        }

        IEnumerator PulseScale()
        {
            float t = 0;
            var peak = _baseScale * 1.4f;
            while (t < 0.5f)
            {
                t += Time.deltaTime / 0.15f;
                transform.localScale = Vector3.Lerp(_baseScale, peak, Mathf.Sin(t * Mathf.PI));
                yield return null;
            }
            transform.localScale = _baseScale;
        }

        // -----------------------------------------------------------------------
        // Interaction — tap via AR raycast
        // -----------------------------------------------------------------------

        /// Called by MotifGraphScene when a raycast hits this node's collider.
        public void OnTapped()
        {
            if (_tapSound != null) _tapSound.Play();

            if (IsSelected)
            {
                Deselect();
            }
            else
            {
                Select();
            }
        }

        public void Select()
        {
            IsSelected = true;
            StartCoroutine(ScaleTo(_baseScale * _selectedScale, 0.15f));

            if (_glowRenderer != null)
            {
                var c = _glowRenderer.material.color;
                c.a = 0.6f;
                _glowRenderer.material.color = c;
            }

            // Show echo panel in resonance renderer
            _resonanceRenderer?.ShowEchoPanel(Data.id, Data.label ?? Data.id[..8]);
        }

        public void Deselect()
        {
            IsSelected = false;
            StartCoroutine(ScaleTo(_baseScale, 0.15f));

            if (_glowRenderer != null)
            {
                var c = _glowRenderer.material.color;
                c.a = 0.15f;
                _glowRenderer.material.color = c;
            }

            _resonanceRenderer?.HideEchoPanel();
        }

        public void OnHoverEnter()
        {
            if (IsSelected) return;
            StartCoroutine(ScaleTo(_baseScale * _hoverScale, 0.1f));
        }

        public void OnHoverExit()
        {
            if (IsSelected) return;
            StartCoroutine(ScaleTo(_baseScale, 0.1f));
        }

        IEnumerator ScaleTo(Vector3 target, float duration)
        {
            var start = transform.localScale;
            float t = 0;
            while (t < 1f)
            {
                t += Time.deltaTime / duration;
                transform.localScale = Vector3.Lerp(start, target, Mathf.SmoothStep(0, 1, t));
                yield return null;
            }
            transform.localScale = target;
        }

        // -----------------------------------------------------------------------
        // Billboard — always face camera
        // -----------------------------------------------------------------------

        void LateUpdate()
        {
            if (Camera.main != null && _labelText != null)
                _labelText.transform.LookAt(Camera.main.transform.position);
        }

        // -----------------------------------------------------------------------
        // Echo response type (local to node — mirrors MotifResonanceRenderer's)
        // -----------------------------------------------------------------------

        [Serializable]
        class EchoResponse
        {
            public EchoItem[] echoes;
        }

        [Serializable]
        class EchoItem
        {
            public string domain;
            public float  cosine_distance;
        }
    }
}
