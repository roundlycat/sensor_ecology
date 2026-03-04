// MotifGraphScene.cs
// Builds the AR motif node graph around the agent anchor.
// Fetches motifs from /api/motifs, places nodes using a force-directed
// orbital layout, registers them with MotifResonanceRenderer, and
// handles tap interaction via AR raycasting.
//
// Attach to the same GameObject as AgentPerceptionVisualizer,
// or to a dedicated GraphRoot object parented to the agent anchor.
//
// Place in: Assets/AgentPerception/Graph/

using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;
using UnityEngine.InputSystem;

namespace AgentPerception
{
    public class MotifGraphScene : MonoBehaviour
    {
        // -----------------------------------------------------------------------
        // Inspector
        // -----------------------------------------------------------------------

        [Header("Layout")]
        [SerializeField] Transform  _graphRoot;         // parent for all nodes; set to agent anchor
        [SerializeField] float      _innerRadius  = 0.12f;   // closest orbit ring
        [SerializeField] float      _ringSpacing  = 0.09f;   // distance between rings
        [SerializeField] int        _nodesPerRing = 8;       // max nodes per orbit ring
        [SerializeField] float      _nodeElevationVariance = 0.04f;

        [Header("Prefabs")]
        [SerializeField] GameObject _motifNodePrefab;    // prefab with MotifNode component

        [Header("References")]
        [SerializeField] MotifResonanceRenderer _resonanceRenderer;
        [SerializeField] PerceptualEventBus     _bus;

        [Header("Fetch")]
        [SerializeField] string     _relayHost    = "192.168.1.100";
        [SerializeField] int        _relayPort    = 8765;
        [SerializeField] int        _minRecurrences = 0;
        [SerializeField] float      _refreshIntervalS = 30f;   // re-fetch motif list

        [Header("Interaction")]
        [SerializeField] ARRaycastManager   _arRaycastManager;
        [SerializeField] LayerMask          _nodeLayer;

        [Header("UI")]
        [SerializeField] TMPro.TextMeshPro  _statusText;   // optional; assign to show bootstrap / error state

        // -----------------------------------------------------------------------
        // Internal state
        // -----------------------------------------------------------------------

        string _baseUrl;
        readonly Dictionary<string, MotifNode> _nodes    = new();
        readonly List<ARRaycastHit>            _hits     = new();
        MotifNode                              _selected;
        Coroutine                              _fetchLoop;
        bool                                  _graphBuilt;

        // Force-directed layout state
        readonly Dictionary<string, Vector3> _positions   = new();
        readonly Dictionary<string, Vector3> _velocities  = new();

        // -----------------------------------------------------------------------
        // Lifecycle
        // -----------------------------------------------------------------------

        void Awake()
        {
            _baseUrl = $"http://{_relayHost}:{_relayPort}";

            if (_graphRoot == null)
                _graphRoot = transform;

            if (_bus == null)
                _bus = PerceptualEventBus.Instance;

            if (_resonanceRenderer == null)
                _resonanceRenderer = FindObjectOfType<MotifResonanceRenderer>();
        }

        void OnEnable()
        {
            _fetchLoop = StartCoroutine(FetchLoop());
        }

        void OnDisable()
        {
            if (_fetchLoop != null) StopCoroutine(_fetchLoop);
        }

        void Update()
        {
            HandleTapInput();
            if (_graphBuilt)
                StepForceLayout(Time.deltaTime);
        }

        // -----------------------------------------------------------------------
        // Fetch loop
        // -----------------------------------------------------------------------

        IEnumerator FetchLoop()
        {
            while (true)
            {
                yield return FetchMotifs();
                yield return new WaitForSeconds(_refreshIntervalS);
            }
        }

        IEnumerator FetchMotifs()
        {
            var url = $"{_baseUrl}/api/motifs?min_recurrences={_minRecurrences}&limit=200";
            using var req = UnityEngine.Networking.UnityWebRequest.Get(url);
            req.timeout = 10;
            yield return req.SendWebRequest();

            if (req.result != UnityEngine.Networking.UnityWebRequest.Result.Success)
            {
                Debug.LogWarning($"[MotifGraphScene] Fetch failed: {req.error}");
                yield break;
            }

            var response = JsonUtility.FromJson<MotifListResponse>(req.downloadHandler.text);

            if (response.bootstrap)
            {
                Debug.Log("[MotifGraphScene] Bootstrap state: motifs table is empty. Seed the corpus to begin.");
                SetStatus("Seeding in progress\nNo motifs yet");
                yield break;
            }

            SetStatus(null);

            if (response?.motifs == null || response.motifs.Length == 0)
            {
                Debug.Log("[MotifGraphScene] No motifs returned (filter may be too strict).");
                yield break;
            }

            Debug.Log($"[MotifGraphScene] Fetched {response.motifs.Length} motifs.");
            UpdateGraph(response.motifs);
        }

        // -----------------------------------------------------------------------
        // Graph construction / update
        // -----------------------------------------------------------------------

        void UpdateGraph(MotifData[] motifs)
        {
            var incomingIds = new HashSet<string>(motifs.Select(m => m.id));

            // Remove nodes no longer in the motif list
            var toRemove = _nodes.Keys.Where(id => !incomingIds.Contains(id)).ToList();
            foreach (var id in toRemove)
            {
                if (_nodes.TryGetValue(id, out var node))
                {
                    _resonanceRenderer?.UnregisterMotifNode(id);
                    Destroy(node.gameObject);
                    _nodes.Remove(id);
                    _positions.Remove(id);
                    _velocities.Remove(id);
                }
            }

            // Sort by recurrence so high-resonance motifs land on inner rings
            var sorted = motifs.OrderByDescending(m => m.recurrence_count).ToArray();

            for (int i = 0; i < sorted.Length; i++)
            {
                var data = sorted[i];

                if (_nodes.ContainsKey(data.id))
                {
                    // Update existing node's data display without repositioning
                    // (force layout keeps positions stable across refreshes)
                    continue;
                }

                // Place new node
                var startPos = OrbitalStartPosition(i, sorted.Length);
                SpawnNode(data, _graphRoot.position + startPos);
            }

            _graphBuilt = true;
        }

        void SpawnNode(MotifData data, Vector3 worldPos)
        {
            if (_motifNodePrefab == null)
            {
                Debug.LogWarning("[MotifGraphScene] No motifNodePrefab assigned.");
                return;
            }

            var go = Instantiate(_motifNodePrefab, worldPos, Quaternion.identity, _graphRoot);
            var node = go.GetComponent<MotifNode>();
            if (node == null)
            {
                Debug.LogError("[MotifGraphScene] MotifNode component missing from prefab.");
                Destroy(go);
                return;
            }

            // Get a PerceptualEventClient from the scene
            var client = FindObjectOfType<PerceptualEventClient>();

            node.Initialise(data, _resonanceRenderer, client, _bus);

            _nodes[data.id]     = node;
            _positions[data.id] = worldPos - _graphRoot.position;
            _velocities[data.id] = Vector3.zero;

            // Stagger appearance
            StartCoroutine(AnimateIn(go, _nodes.Count * 0.05f));
        }

        IEnumerator AnimateIn(GameObject go, float delay)
        {
            go.transform.localScale = Vector3.zero;
            yield return new WaitForSeconds(delay);

            float t = 0;
            var target = go.transform.localScale;
            // Reset to zero in case Update moved it
            var finalScale = go.GetComponent<MotifNode>() != null
                ? go.transform.localScale
                : Vector3.one * 0.04f;

            go.transform.localScale = Vector3.zero;
            while (t < 1f)
            {
                t += Time.deltaTime / 0.3f;
                go.transform.localScale = Vector3.Lerp(Vector3.zero, finalScale,
                    Mathf.SmoothStep(0, 1, t));
                yield return null;
            }
        }

        // -----------------------------------------------------------------------
        // Orbital layout — concentric rings, sorted by recurrence
        // -----------------------------------------------------------------------

        Vector3 OrbitalStartPosition(int index, int total)
        {
            int ring      = index / _nodesPerRing;
            int slotInRing = index % _nodesPerRing;
            int nodesThisRing = Mathf.Min(_nodesPerRing, total - ring * _nodesPerRing);

            float radius  = _innerRadius + ring * _ringSpacing;
            float angle   = (slotInRing / (float)nodesThisRing) * Mathf.PI * 2f;
            float elevate = UnityEngine.Random.Range(-_nodeElevationVariance, _nodeElevationVariance);

            return new Vector3(
                Mathf.Cos(angle) * radius,
                elevate,
                Mathf.Sin(angle) * radius
            );
        }

        // -----------------------------------------------------------------------
        // Force-directed layout
        // Keeps nodes separated and gently orbiting so the graph breathes.
        // Runs every frame after initial build — very lightweight (n usually < 50).
        // -----------------------------------------------------------------------

        const float REPULSION     = 0.0004f;
        const float DAMPING       = 0.85f;
        const float MIN_DIST      = 0.06f;
        const float ORBIT_SPEED   = 0.04f;   // radians/second per ring
        const float MAX_VELOCITY  = 0.002f;

        void StepForceLayout(float dt)
        {
            var ids   = _positions.Keys.ToArray();
            int count = ids.Length;
            if (count < 2) return;

            // Repulsion between all pairs
            for (int i = 0; i < count; i++)
            {
                for (int j = i + 1; j < count; j++)
                {
                    var diff = _positions[ids[i]] - _positions[ids[j]];
                    float dist = Mathf.Max(diff.magnitude, MIN_DIST);
                    var force  = diff.normalized * (REPULSION / (dist * dist));

                    if (_velocities.ContainsKey(ids[i])) _velocities[ids[i]] += force;
                    if (_velocities.ContainsKey(ids[j])) _velocities[ids[j]] -= force;
                }
            }

            // Slow orbital drift — each node drifts around its ring axis
            for (int i = 0; i < count; i++)
            {
                var id  = ids[i];
                var pos = _positions[id];
                float ringRadius = new Vector2(pos.x, pos.z).magnitude;
                if (ringRadius < 0.001f) continue;

                // Tangent in XZ plane
                var tangent = new Vector3(-pos.z, 0, pos.x).normalized;
                int ring = Mathf.RoundToInt((ringRadius - _innerRadius) / Mathf.Max(_ringSpacing, 0.001f));
                float speed = ORBIT_SPEED * (1f + ring * 0.3f) * dt;
                _velocities[id] += tangent * speed;
            }

            // Integrate + dampen + apply
            foreach (var id in ids)
            {
                _velocities[id] *= DAMPING;
                _velocities[id]  = Vector3.ClampMagnitude(_velocities[id], MAX_VELOCITY);
                _positions[id]  += _velocities[id];

                if (_nodes.TryGetValue(id, out var node) && node != null)
                    node.transform.position = _graphRoot.position + _positions[id];
            }
        }

        // -----------------------------------------------------------------------
        // Tap interaction
        // -----------------------------------------------------------------------

        void HandleTapInput()
        {
#if ENABLE_INPUT_SYSTEM
            if (Touchscreen.current == null) return;
            var touch = Touchscreen.current.primaryTouch;
            if (!touch.press.wasPressedThisFrame) return;
            var screenPos = touch.position.ReadValue();
#else
            if (!Input.GetMouseButtonDown(0)) return;
            var screenPos = (Vector2)Input.mousePosition;
#endif
            // First try AR raycast against physical planes
            if (_arRaycastManager != null
                && _arRaycastManager.Raycast(screenPos, _hits, TrackableType.AllTypes))
            {
                // AR hit found — not a node tap (graph interaction handled below)
            }

            // Raycast against node colliders
            if (Camera.main == null) return;
            var ray = Camera.main.ScreenPointToRay(screenPos);

            if (Physics.Raycast(ray, out var hit, 5f, _nodeLayer))
            {
                var node = hit.collider.GetComponentInParent<MotifNode>();
                if (node != null)
                {
                    HandleNodeTap(node);
                    return;
                }
            }

            // Tap on empty space — deselect
            if (_selected != null)
            {
                _selected.Deselect();
                _selected = null;
            }
        }

        void HandleNodeTap(MotifNode node)
        {
            if (_selected != null && _selected != node)
            {
                _selected.Deselect();
            }

            node.OnTapped();
            _selected = node.IsSelected ? node : null;
        }

        // -----------------------------------------------------------------------
        // Status text helper
        // -----------------------------------------------------------------------

        void SetStatus(string message)
        {
            if (_statusText == null) return;
            _statusText.text    = message ?? "";
            _statusText.enabled = message != null;
        }

        // -----------------------------------------------------------------------
        // Public API — for external systems
        // -----------------------------------------------------------------------

        /// Returns the transform of a motif node by id, or null if not in scene.
        public Transform GetNodeTransform(string motifId)
        {
            _nodes.TryGetValue(motifId, out var node);
            return node != null ? node.transform : null;
        }

        /// Force an immediate refresh of the motif list.
        public void RefreshNow()
        {
            if (_fetchLoop != null) StopCoroutine(_fetchLoop);
            _fetchLoop = StartCoroutine(FetchLoop());
        }

        /// Returns all currently active motif nodes.
        public IEnumerable<MotifNode> AllNodes => _nodes.Values;

#if UNITY_EDITOR
        // -----------------------------------------------------------------------
        // Editor gizmos — shows ring layout in Scene view
        // -----------------------------------------------------------------------
        void OnDrawGizmosSelected()
        {
            if (_graphRoot == null) return;
            Gizmos.color = new Color(0.3f, 1f, 0.6f, 0.2f);
            for (int ring = 0; ring < 4; ring++)
            {
                float r = _innerRadius + ring * _ringSpacing;
                DrawCircleGizmo(_graphRoot.position, r);
            }
        }

        void DrawCircleGizmo(Vector3 center, float radius)
        {
            int segments = 32;
            Vector3 prev = center + new Vector3(radius, 0, 0);
            for (int i = 1; i <= segments; i++)
            {
                float a   = i / (float)segments * Mathf.PI * 2f;
                var  next = center + new Vector3(Mathf.Cos(a) * radius, 0, Mathf.Sin(a) * radius);
                Gizmos.DrawLine(prev, next);
                prev = next;
            }
        }
#endif
    }
}
