// PerceptualModels.cs
// Data models mirroring the PostgreSQL schema.
// Serialised/deserialised from the Pi 5 relay API via JsonUtility or Newtonsoft.
//
// Place in: Assets/AgentPerception/Models/

using System;
using System.Collections.Generic;
using UnityEngine;

namespace AgentPerception
{
    // -----------------------------------------------------------------------
    // Enums — mirror the SQL enum types
    // -----------------------------------------------------------------------

    public enum SensorDomain
    {
        EnvironmentalField,
        EmbodiedState,
        RelationalContact,
        HighBandwidth,
        Unknown
    }

    public enum FusionConfidence
    {
        High,
        Moderate,
        Low,
        Synthetic
    }

    public enum ResonanceType
    {
        Recurrence,
        Candidate,
        WeakEcho
    }

    // -----------------------------------------------------------------------
    // Core event model
    // -----------------------------------------------------------------------

    [Serializable]
    public class PerceptualEvent
    {
        public string          id;
        public string          agent_node_id;
        public string          domain;                 // raw string from JSON
        public string          event_label;
        public string          confidence;
        public string          event_start;            // ISO 8601
        public bool            is_cross_domain;
        public string[]        domains_involved;
        public float           agent_power_mw;
        public float           agent_temp_c;
        public int             agent_cpu_load_pct;
        public FeatureSnapshot feature_snapshot;

        // Derived — populated client-side after deserialisation
        [NonSerialized] public SensorDomain     DomainEnum;
        [NonSerialized] public FusionConfidence ConfidenceEnum;
        [NonSerialized] public DateTime         Timestamp;
        [NonSerialized] public MotifResonance   NearestResonance; // may be null

        public void Hydrate()
        {
            DomainEnum     = ParseDomain(domain);
            ConfidenceEnum = ParseConfidence(confidence);
            if (DateTime.TryParse(event_start, out var dt))
                Timestamp = dt.ToLocalTime();
        }

        static SensorDomain ParseDomain(string s) => s switch
        {
            "environmental_field" => SensorDomain.EnvironmentalField,
            "embodied_state"      => SensorDomain.EmbodiedState,
            "relational_contact"  => SensorDomain.RelationalContact,
            "high_bandwidth"      => SensorDomain.HighBandwidth,
            _                     => SensorDomain.Unknown
        };

        static FusionConfidence ParseConfidence(string s) => s switch
        {
            "high"      => FusionConfidence.High,
            "moderate"  => FusionConfidence.Moderate,
            "low"       => FusionConfidence.Low,
            "synthetic" => FusionConfidence.Synthetic,
            _           => FusionConfidence.Moderate
        };
    }

    // -----------------------------------------------------------------------
    // Feature snapshot — key sensor values at event time
    // -----------------------------------------------------------------------

    [Serializable]
    public class FeatureSnapshot
    {
        public string  domain;
        public string  label;
        public float   agent_power;
        public float   agent_temp;
        public int     agent_cpu;
        public int     n_readings;

        // Channel values stored as parallel arrays for JsonUtility compatibility
        public string[] channel_keys;
        public float[]  channel_values;

        public bool TryGetChannel(string key, out float value)
        {
            value = 0f;
            if (channel_keys == null) return false;
            for (int i = 0; i < channel_keys.Length; i++)
            {
                if (channel_keys[i] == key)
                {
                    value = channel_values[i];
                    return true;
                }
            }
            return false;
        }
    }

    // -----------------------------------------------------------------------
    // Motif resonance
    // -----------------------------------------------------------------------

    [Serializable]
    public class MotifResonance
    {
        public string id;
        public string perceptual_event_id;
        public string motif_id;
        public float  cosine_distance;
        public bool   is_nearest;
        public string resonance_type;
        public string observed_at;

        [NonSerialized] public ResonanceType ResonanceTypeEnum;

        public void Hydrate()
        {
            ResonanceTypeEnum = resonance_type switch
            {
                "recurrence" => ResonanceType.Recurrence,
                "candidate"  => ResonanceType.Candidate,
                "weak_echo"  => ResonanceType.WeakEcho,
                _            => ResonanceType.WeakEcho
            };
        }
    }

    // -----------------------------------------------------------------------
    // Agent node vitals  (from /api/agent/{id}/vitals)
    // -----------------------------------------------------------------------

    [Serializable]
    public class AgentVitals
    {
        public string node_name;
        public float  power_mw;
        public float  temp_c;
        public int    cpu_load_pct;
        public string last_heartbeat;
        public bool   is_online;
    }

    // -----------------------------------------------------------------------
    // API response wrappers
    // -----------------------------------------------------------------------

    [Serializable]
    public class EventListResponse
    {
        public PerceptualEvent[] events;
        public int               total;
        public string            cursor;      // for pagination
    }

    [Serializable]
    public class ResonanceListResponse
    {
        public MotifResonance[] resonances;
    }

    // -----------------------------------------------------------------------
    // Perceptual event enriched with colour/weight for rendering
    // Produced by PerceptualEventBus before broadcast
    // -----------------------------------------------------------------------

    public class EnrichedEvent
    {
        public PerceptualEvent Source;
        public Color           DomainColor;
        public float           IntensityWeight;   // 0-1, derived from confidence + distance
        public string          DisplayLabel;
        public bool            HasMotifResonance => Source.NearestResonance != null;
    }

    // -----------------------------------------------------------------------
    // Motif structural data & Drift
    // -----------------------------------------------------------------------


    [Serializable]
    public class MotifDriftLog
    {
        public string id;
        public string motif_id;
        public string centroid_before; // vector as string or parsed later
        public string centroid_after;
        public string trigger_event_id;
        public int    n_events_included;
        public string computed_at;
    }

    [Serializable]
    public class MotifDriftResponse
    {
        public MotifDriftLog[] drifts;
    }

}
