// MotifDriftVisualizer.cs
// Polls the relay API for perceptual motif drift records and visualizes 
// the semantic drift of the motif centroid as a trail in AR space.
//
// Attach to the central GraphRoot or a dedicated manager in your AR Scene.

using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Networking;

namespace AgentPerception
{
    public class MotifDriftVisualizer : MonoBehaviour
    {
        [Header("Relay Setup")]
        [SerializeField] string _relayHost = "192.168.1.100";
        [SerializeField] int    _relayPort = 8765;
        [SerializeField] float  _pollIntervalSec = 60f;

        [Header("Visuals")]
        [SerializeField] Gradient _driftTrailColor;
        [SerializeField] float    _trailWidth = 0.005f;
        [SerializeField] Material _trailMaterial;

        [Header("References")]
        [SerializeField] MotifGraphScene _graphScene;
        
        // Caches line renderers per motif
        Dictionary<string, LineRenderer> _driftTrails = new();

        void Start()
        {
            if (_graphScene == null)
                _graphScene = FindObjectOfType<MotifGraphScene>();

            StartCoroutine(PollDriftLoop());
        }

        IEnumerator PollDriftLoop()
        {
            while (true)
            {
                // Note: The /api/motifs/drift endpoint needs to be implemented in relay_api.py
                // to return the perceptual_motif_drift records.
                var url = $"http://{_relayHost}:{_relayPort}/api/motifs/drift?limit=100";
                using var req = UnityWebRequest.Get(url);
                yield return req.SendWebRequest();

                if (req.result == UnityWebRequest.Result.Success)
                {
                    try
                    {
                        var response = JsonUtility.FromJson<MotifDriftResponse>(req.downloadHandler.text);
                        if (response?.drifts != null)
                        {
                            UpdateDriftTrails(response.drifts);
                        }
                    }
                    catch (System.Exception e)
                    {
                        Debug.LogWarning($"[MotifDriftVisualizer] Failed to parse drift data: {e.Message}");
                    }
                }

                yield return new WaitForSeconds(_pollIntervalSec);
            }
        }

        void UpdateDriftTrails(MotifDriftLog[] drifts)
        {
            if (_graphScene == null) return;

            // Group drift logs by motif_id
            var grouped = new Dictionary<string, List<MotifDriftLog>>();
            foreach (var d in drifts)
            {
                if (!grouped.ContainsKey(d.motif_id))
                    grouped[d.motif_id] = new List<MotifDriftLog>();
                grouped[d.motif_id].Add(d);
            }

            foreach (var kvp in grouped)
            {
                var motifId = kvp.Key;
                var logs = kvp.Value;
                
                // Ensure logs are sorted by computed_at (oldest first)
                logs.Sort((a, b) => System.String.Compare(a.computed_at, b.computed_at, System.StringComparison.Ordinal));

                var nodeTransform = _graphScene.GetNodeTransform(motifId);
                if (nodeTransform == null) continue;

                if (!_driftTrails.TryGetValue(motifId, out var line))
                {
                    var go = new GameObject($"DriftTrail_{motifId}");
                    go.transform.SetParent(nodeTransform, false);
                    line = go.AddComponent<LineRenderer>();
                    line.material = _trailMaterial ? _trailMaterial : new Material(Shader.Find("Sprites/Default"));
                    line.startWidth = _trailWidth;
                    line.endWidth = _trailWidth;
                    line.useWorldSpace = false; // local to the node
                    line.colorGradient = _driftTrailColor;
                    _driftTrails[motifId] = line;
                }

                // In a true semantic projection, we would map the 384D pgvector down to 3D.
                // For this AR graph, motifs are arranged by force-directed physics, not raw embeddings.
                // We'll simulate the "drift tail" by projecting a small local offset 
                // based on the consecutive drift record indices or a hash of the vector to show "movement".
                
                line.positionCount = logs.Count + 1;
                
                // Start of trail is the current node center (0,0,0 local space)
                line.SetPosition(0, Vector3.zero);

                Vector3 currentTailPos = Vector3.zero;
                for (int i = 0; i < logs.Count; i++)
                {
                    // A simple pseudo-random walk based on the UUID to visualize the concept of 'drift' 
                    // without doing PCA down from 384 dimensions in C#.
                    var hash1 = logs[i].trigger_event_id.GetHashCode();
                    var hash2 = logs[i].computed_at.GetHashCode();
                    
                    float dx = (hash1 % 100) / 10000f; 
                    float dz = (hash2 % 100) / 10000f;
                    float dy = -0.01f; // Trail drops downwards slightly

                    currentTailPos += new Vector3(dx, dy, dz);
                    line.SetPosition(i + 1, currentTailPos);
                }
            }
        }
    }
}
