// PerceptualEventBus.cs
// Central dispatcher. Sits between the network client and rendering components.
// Enriches raw events with rendering metadata (colour, weight, display label)
// and broadcasts them to any registered listener.
//
// Place in: Assets/AgentPerception/Core/

using System;
using System.Collections.Generic;
using UnityEngine;

namespace AgentPerception
{
    /// <summary>
    /// Singleton bus. Survives scene loads. All rendering components subscribe here.
    /// </summary>
    public class PerceptualEventBus : MonoBehaviour
    {
        // -----------------------------------------------------------------------
        // Singleton
        // -----------------------------------------------------------------------

        public static PerceptualEventBus Instance { get; private set; }

        void Awake()
        {
            if (Instance != null && Instance != this) { Destroy(gameObject); return; }
            Instance = this;
            DontDestroyOnLoad(gameObject);
            BuildColorMap();
        }

        // -----------------------------------------------------------------------
        // Public events
        // -----------------------------------------------------------------------

        /// Fired for every new event, enriched with rendering metadata.
        public event Action<EnrichedEvent>       OnPerceptualEvent;

        /// Fired when a motif recurrence is detected in a physical event.
        public event Action<EnrichedEvent>       OnMotifRecurrence;

        /// Fired when a novel candidate event arrives (no close motif match).
        public event Action<EnrichedEvent>       OnCandidateEvent;

        /// Fired when agent vitals update.
        public event Action<AgentVitals>         OnVitalsUpdate;

        /// Fired on connection state change.
        public event Action<bool>                OnConnectionState;

        // -----------------------------------------------------------------------
        // Domain colour palette
        // Override in Inspector via _domainColors if you want theming
        // -----------------------------------------------------------------------

        [Header("Domain Colours")]
        [SerializeField] Color _environmentalColor = new Color(0.20f, 0.80f, 0.45f);  // teal-green
        [SerializeField] Color _embodiedColor      = new Color(0.90f, 0.55f, 0.20f);  // amber
        [SerializeField] Color _relationalColor    = new Color(0.40f, 0.60f, 0.95f);  // soft blue
        [SerializeField] Color _highBandwidthColor = new Color(0.85f, 0.30f, 0.75f);  // violet
        [SerializeField] Color _unknownColor       = new Color(0.60f, 0.60f, 0.60f);

        Dictionary<SensorDomain, Color> _colorMap;

        void BuildColorMap()
        {
            _colorMap = new Dictionary<SensorDomain, Color>
            {
                { SensorDomain.EnvironmentalField, _environmentalColor },
                { SensorDomain.EmbodiedState,      _embodiedColor      },
                { SensorDomain.RelationalContact,  _relationalColor    },
                { SensorDomain.HighBandwidth,      _highBandwidthColor },
                { SensorDomain.Unknown,            _unknownColor       },
            };
        }

        // -----------------------------------------------------------------------
        // Confidence -> intensity weight
        // -----------------------------------------------------------------------

        static float ConfidenceWeight(FusionConfidence c) => c switch
        {
            FusionConfidence.High      => 1.0f,
            FusionConfidence.Moderate  => 0.65f,
            FusionConfidence.Low       => 0.35f,
            FusionConfidence.Synthetic => 0.50f,
            _                          => 0.50f,
        };

        // Resonance distance -> additional weight (closer = more salient)
        static float ResonanceBoost(MotifResonance r)
        {
            if (r == null) return 0f;
            return Mathf.Clamp01(1f - (float)r.cosine_distance / 0.4f) * 0.3f;
        }

        // -----------------------------------------------------------------------
        // Recent event cache (for UI history panels)
        // -----------------------------------------------------------------------

        [Header("Cache")]
        [SerializeField] int _cacheSize = 100;

        readonly Queue<EnrichedEvent>  _recentEvents    = new();
        readonly Queue<EnrichedEvent>  _recentMotifHits = new();

        public IEnumerable<EnrichedEvent> RecentEvents    => _recentEvents;
        public IEnumerable<EnrichedEvent> RecentMotifHits => _recentMotifHits;

        // -----------------------------------------------------------------------
        // Wiring to PerceptualEventClient
        // -----------------------------------------------------------------------

        PerceptualEventClient _client;

        void OnEnable()
        {
            _client = GetComponent<PerceptualEventClient>();
            if (_client == null)
                _client = GetComponentInChildren<PerceptualEventClient>();

            if (_client != null)
            {
                _client.OnEventsReceived    += HandleRawEvents;
                _client.OnVitalsReceived    += HandleVitals;
                _client.OnConnectionChanged += state => OnConnectionState?.Invoke(state);
            }
            else
            {
                Debug.LogWarning("[PerceptualEventBus] No PerceptualEventClient found.");
            }
        }

        void OnDisable()
        {
            if (_client != null)
            {
                _client.OnEventsReceived    -= HandleRawEvents;
                _client.OnVitalsReceived    -= HandleVitals;
                _client.OnConnectionChanged -= state => OnConnectionState?.Invoke(state);
            }
        }

        // -----------------------------------------------------------------------
        // Enrichment and dispatch
        // -----------------------------------------------------------------------

        void HandleRawEvents(PerceptualEvent[] events)
        {
            foreach (var ev in events)
                Dispatch(Enrich(ev));
        }

        void HandleVitals(AgentVitals vitals)
        {
            OnVitalsUpdate?.Invoke(vitals);
        }

        EnrichedEvent Enrich(PerceptualEvent ev)
        {
            _colorMap.TryGetValue(ev.DomainEnum, out var color);

            float weight = ConfidenceWeight(ev.ConfidenceEnum)
                         + ResonanceBoost(ev.NearestResonance);

            string label = BuildDisplayLabel(ev);

            return new EnrichedEvent
            {
                Source          = ev,
                DomainColor     = color,
                IntensityWeight = Mathf.Clamp01(weight),
                DisplayLabel    = label,
            };
        }

        string BuildDisplayLabel(PerceptualEvent ev)
        {
            var label = string.IsNullOrEmpty(ev.event_label)
                ? ev.domain.Replace("_", " ")
                : ev.event_label.Replace("_", " ");

            if (ev.NearestResonance != null)
            {
                label += ev.NearestResonance.ResonanceTypeEnum switch
                {
                    ResonanceType.Recurrence => " ↺",
                    ResonanceType.WeakEcho   => " ∿",
                    ResonanceType.Candidate  => " ✦",
                    _                        => ""
                };
            }

            return label;
        }

        void Dispatch(EnrichedEvent enriched)
        {
            // Add to cache
            Enqueue(_recentEvents, enriched);

            // Fire general event
            OnPerceptualEvent?.Invoke(enriched);

            // Fire specialised events
            if (enriched.Source.NearestResonance != null)
            {
                var rt = enriched.Source.NearestResonance.ResonanceTypeEnum;
                if (rt == ResonanceType.Recurrence)
                {
                    Enqueue(_recentMotifHits, enriched);
                    OnMotifRecurrence?.Invoke(enriched);
                }
                else if (rt == ResonanceType.Candidate)
                {
                    OnCandidateEvent?.Invoke(enriched);
                }
            }
        }

        void Enqueue(Queue<EnrichedEvent> queue, EnrichedEvent ev)
        {
            queue.Enqueue(ev);
            while (queue.Count > _cacheSize) queue.Dequeue();
        }
    }
}
