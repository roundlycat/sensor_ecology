// MotifResonanceRenderer.cs
// Visualises the relationship between physical perceptual events and
// linguistic motifs. When a physical event resonates with a motif, this
// component draws a transient connection line in AR space.
//
// Also provides a motif echo panel — tap a motif node to see which physical
// events have echoed it over time.
//
// Place in: Assets/AgentPerception/Visualization/

using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.UI;

namespace AgentPerception
{
    /// <summary>
    /// Draws resonance arcs from event glyphs to motif nodes.
    /// Motif node positions should be set externally by your motif graph system.
    /// </summary>
    public class MotifResonanceRenderer : MonoBehaviour
    {
        // -----------------------------------------------------------------------
        // Inspector
        // -----------------------------------------------------------------------

        [Header("Line Rendering")]
        [SerializeField] Material     _arcMaterial;
        [SerializeField] float        _arcLineWidth     = 0.002f;
        [SerializeField] float        _arcLifetimeS     = 3.0f;
        [SerializeField] int          _arcResolution    = 24;        // segments per arc
        [SerializeField] AnimationCurve _arcHeightCurve;             // how arc bows in 3D

        [Header("Colours by resonance type")]
        [SerializeField] Color _recurrenceColor = new Color(0.3f, 1.0f, 0.6f, 0.8f);
        [SerializeField] Color _weakEchoColor   = new Color(0.8f, 0.8f, 0.3f, 0.4f);
        [SerializeField] Color _candidateColor  = new Color(0.6f, 0.4f, 1.0f, 0.6f);

        [Header("Echo Panel (UI)")]
        [SerializeField] GameObject   _echoPanelRoot;
        [SerializeField] Transform    _echoListContainer;
        [SerializeField] GameObject   _echoItemPrefab;
        [SerializeField] TMPro.TextMeshProUGUI _echoPanelTitle;

        // -----------------------------------------------------------------------
        // Motif node registry
        // External code registers motif positions here as the motif graph is built
        // -----------------------------------------------------------------------

        readonly Dictionary<string, Transform> _motifNodes = new();

        public void RegisterMotifNode(string motifId, Transform node)
        {
            _motifNodes[motifId] = node;
        }

        public void UnregisterMotifNode(string motifId)
        {
            _motifNodes.Remove(motifId);
        }

        // -----------------------------------------------------------------------
        // Active arcs pool
        // -----------------------------------------------------------------------

        readonly List<LineRenderer> _arcPool      = new();
        readonly List<LineRenderer> _activeArcs   = new();

        // -----------------------------------------------------------------------
        // Lifecycle
        // -----------------------------------------------------------------------

        PerceptualEventBus _bus;
        PerceptualEventClient _client;

        void Awake()
        {
            if (_arcHeightCurve == null || _arcHeightCurve.length == 0)
            {
                _arcHeightCurve = new AnimationCurve(
                    new Keyframe(0f, 0f),
                    new Keyframe(0.5f, 1f),
                    new Keyframe(1f, 0f)
                );
            }
        }

        void OnEnable()
        {
            _bus    = PerceptualEventBus.Instance;
            _client = FindObjectOfType<PerceptualEventClient>();

            if (_bus != null)
                _bus.OnMotifRecurrence += DrawResonanceArc;

            if (_echoPanelRoot != null)
                _echoPanelRoot.SetActive(false);
        }

        void OnDisable()
        {
            if (_bus != null)
                _bus.OnMotifRecurrence -= DrawResonanceArc;
        }

        // -----------------------------------------------------------------------
        // Arc drawing
        // -----------------------------------------------------------------------

        void DrawResonanceArc(EnrichedEvent ev)
        {
            var resonance = ev.Source.NearestResonance;
            if (resonance == null) return;

            // We need both endpoints: the event spawn position and the motif node
            if (!_motifNodes.TryGetValue(resonance.motif_id, out var motifNode))
                return;   // motif not in scene — skip

            // Find the event's glyph transform (by label tag or event id)
            // Glyphs tag themselves with their event ID if EventGlyphBehaviour is present
            var glyphRoot = FindGlyphForEvent(ev.Source.id);
            if (glyphRoot == null) return;

            Color arcColor = resonance.ResonanceTypeEnum switch
            {
                ResonanceType.Recurrence => _recurrenceColor,
                ResonanceType.WeakEcho   => _weakEchoColor,
                ResonanceType.Candidate  => _candidateColor,
                _                        => _weakEchoColor
            };

            var lr = AcquireLineRenderer();
            ConfigureArc(lr, glyphRoot.position, motifNode.position, arcColor, ev.IntensityWeight);
            _activeArcs.Add(lr);
            StartCoroutine(FadeArc(lr, _arcLifetimeS));
        }

        void ConfigureArc(
            LineRenderer lr,
            Vector3 from, Vector3 to,
            Color color, float weight)
        {
            lr.positionCount = _arcResolution;
            lr.startWidth    = _arcLineWidth * weight;
            lr.endWidth      = _arcLineWidth * weight * 0.5f;

            if (_arcMaterial != null)
                lr.material = _arcMaterial;

            lr.startColor = color;
            lr.endColor   = new Color(color.r, color.g, color.b, color.a * 0.3f);

            float arcHeight = Vector3.Distance(from, to) * 0.4f;
            var mid = (from + to) * 0.5f + Vector3.up * arcHeight;

            for (int i = 0; i < _arcResolution; i++)
            {
                float t = i / (float)(_arcResolution - 1);
                // Quadratic Bezier
                var pos = Mathf.Pow(1 - t, 2) * from
                        + 2 * (1 - t) * t * mid
                        + Mathf.Pow(t, 2) * to;
                lr.SetPosition(i, pos);
            }

            lr.gameObject.SetActive(true);
        }

        IEnumerator FadeArc(LineRenderer lr, float lifetime)
        {
            var startA = lr.startColor.a;
            var endA   = lr.endColor.a;
            float t = 0;
            while (t < 1f)
            {
                if (lr == null) yield break;
                t += Time.deltaTime / lifetime;
                float fade = Mathf.Lerp(1f, 0f, t);
                var sc = lr.startColor; sc.a = startA * fade; lr.startColor = sc;
                var ec = lr.endColor;   ec.a = endA   * fade; lr.endColor   = ec;
                yield return null;
            }
            ReturnToPool(lr);
        }

        // -----------------------------------------------------------------------
        // Line renderer pool
        // -----------------------------------------------------------------------

        LineRenderer AcquireLineRenderer()
        {
            for (int i = 0; i < _arcPool.Count; i++)
            {
                var lr = _arcPool[i];
                if (!lr.gameObject.activeSelf)
                {
                    _arcPool.RemoveAt(i);
                    return lr;
                }
            }
            var go = new GameObject("ResonanceArc");
            go.transform.SetParent(transform);
            var newLr = go.AddComponent<LineRenderer>();
            newLr.useWorldSpace = true;
            return newLr;
        }

        void ReturnToPool(LineRenderer lr)
        {
            if (lr == null) return;
            lr.gameObject.SetActive(false);
            _activeArcs.Remove(lr);
            _arcPool.Add(lr);
        }

        // -----------------------------------------------------------------------
        // Glyph lookup
        // EventGlyphBehaviour should set gameObject.name to the event id
        // -----------------------------------------------------------------------

        Transform FindGlyphForEvent(string eventId)
        {
            var go = GameObject.Find(eventId);
            return go != null ? go.transform : null;
        }

        // -----------------------------------------------------------------------
        // Echo panel — shown when user taps a motif node
        // -----------------------------------------------------------------------

        public void ShowEchoPanel(string motifId, string motifLabel)
        {
            if (_echoPanelRoot == null || _client == null) return;

            _echoPanelRoot.SetActive(true);

            if (_echoPanelTitle != null)
                _echoPanelTitle.text = $"Physical echoes: {motifLabel}";

            // Clear previous items
            foreach (Transform child in _echoListContainer)
                Destroy(child.gameObject);

            _client.RequestEchoes(motifId, 0.25f, json =>
            {
                PopulateEchoPanel(json);
            });
        }

        public void HideEchoPanel()
        {
            if (_echoPanelRoot != null)
                _echoPanelRoot.SetActive(false);
        }

        void PopulateEchoPanel(string json)
        {
            // Parse echo response manually (JsonUtility doesn't handle root objects well)
            // In production, swap for Newtonsoft for cleaner deserialisation
            var response = JsonUtility.FromJson<EchoResponse>(json);
            if (response?.echoes == null) return;

            foreach (var echo in response.echoes)
            {
                if (_echoItemPrefab == null) break;
                var item = Instantiate(_echoItemPrefab, _echoListContainer);
                var texts = item.GetComponentsInChildren<TMPro.TextMeshProUGUI>();
                if (texts.Length >= 2)
                {
                    texts[0].text = echo.event_label ?? echo.domain;
                    texts[1].text = $"d={echo.cosine_distance:F3}  {echo.event_start?[..10]}";
                }
            }
        }

        // -----------------------------------------------------------------------
        // Supporting types for echo response parsing
        // -----------------------------------------------------------------------

        [System.Serializable]
        class EchoResponse
        {
            public EchoItem[] echoes;
        }

        [System.Serializable]
        class EchoItem
        {
            public string event_id;
            public string node_name;
            public string domain;
            public string event_label;
            public string event_start;
            public float  cosine_distance;
        }
    }
}
