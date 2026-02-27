// ProceduralArcMesh.cs
// Generates a flat arc (annulus segment) mesh at runtime.
// Attach to the ring segment prefab used by MotifNode's domain breakdown ring.
//
// The mesh is created in Awake so it's available immediately when MotifNode
// calls Instantiate() on the prefab. The arc parameters can be set before
// instantiation via the static factory, or overridden via the inspector.
//
// Place in: Assets/AgentPerception/Graph/

using UnityEngine;

namespace AgentPerception
{
    [RequireComponent(typeof(MeshFilter))]
    [RequireComponent(typeof(MeshRenderer))]
    public class ProceduralArcMesh : MonoBehaviour
    {
        // -----------------------------------------------------------------------
        // Inspector
        // -----------------------------------------------------------------------

        [Header("Arc Geometry")]
        [Tooltip("Inner radius of the arc ring")]
        [SerializeField] float _innerRadius  = 0.045f;

        [Tooltip("Outer radius of the arc ring")]
        [SerializeField] float _outerRadius  = 0.055f;

        [Tooltip("Start angle in degrees (0 = right / +X)")]
        [SerializeField] float _startAngleDeg = 0f;

        [Tooltip("Arc sweep in degrees")]
        [SerializeField] float _sweepAngleDeg = 90f;

        [Tooltip("Number of quad segments in the arc — more = smoother")]
        [SerializeField] int   _segments      = 24;

        [Tooltip("If true, rebuild mesh whenever inspector values change in Play mode")]
        [SerializeField] bool  _liveRebuild   = false;

        // -----------------------------------------------------------------------
        // Internal
        // -----------------------------------------------------------------------

        MeshFilter   _mf;
        MeshRenderer _mr;
        Mesh         _mesh;

        // Track previous values for live-rebuild
        float _prevInner, _prevOuter, _prevStart, _prevSweep;
        int   _prevSegments;

        // -----------------------------------------------------------------------
        // Unity lifecycle
        // -----------------------------------------------------------------------

        void Awake()
        {
            _mf   = GetComponent<MeshFilter>();
            _mr   = GetComponent<MeshRenderer>();
            BuildMesh();
        }

#if UNITY_EDITOR
        void Update()
        {
            if (!_liveRebuild) return;
            if (_innerRadius  != _prevInner   ||
                _outerRadius  != _prevOuter   ||
                _startAngleDeg != _prevStart  ||
                _sweepAngleDeg != _prevSweep  ||
                _segments      != _prevSegments)
            {
                BuildMesh();
            }
        }
#endif

        // -----------------------------------------------------------------------
        // Mesh construction
        // -----------------------------------------------------------------------

        public void BuildMesh()
        {
            if (_mf == null) _mf = GetComponent<MeshFilter>();

            if (_mesh == null)
            {
                _mesh      = new Mesh();
                _mesh.name = "ProceduralArc";
            }
            else
            {
                _mesh.Clear();
            }

            int   segs      = Mathf.Max(2, _segments);
            float startRad  = _startAngleDeg  * Mathf.Deg2Rad;
            float sweepRad  = _sweepAngleDeg  * Mathf.Deg2Rad;
            float stepRad   = sweepRad / segs;

            // (segs+1) stations × 2 verts (inner + outer) per station
            int   vertCount  = (segs + 1) * 2;
            var   verts      = new Vector3[vertCount];
            var   uvs        = new Vector2[vertCount];
            var   tris       = new int[segs * 6];

            for (int i = 0; i <= segs; i++)
            {
                float angle = startRad + i * stepRad;
                float cos   = Mathf.Cos(angle);
                float sin   = Mathf.Sin(angle);

                int vInner = i * 2;
                int vOuter = i * 2 + 1;

                verts[vInner] = new Vector3(cos * _innerRadius, 0, sin * _innerRadius);
                verts[vOuter] = new Vector3(cos * _outerRadius, 0, sin * _outerRadius);

                float u = (float)i / segs;
                uvs[vInner] = new Vector2(u, 0);
                uvs[vOuter] = new Vector2(u, 1);
            }

            for (int i = 0; i < segs; i++)
            {
                int ti     = i * 6;
                int vBase  = i * 2;

                // Quad: inner[i], outer[i], inner[i+1], outer[i+1]
                tris[ti + 0] = vBase;
                tris[ti + 1] = vBase + 1;
                tris[ti + 2] = vBase + 2;

                tris[ti + 3] = vBase + 1;
                tris[ti + 4] = vBase + 3;
                tris[ti + 5] = vBase + 2;
            }

            _mesh.vertices  = verts;
            _mesh.uv        = uvs;
            _mesh.triangles = tris;
            _mesh.RecalculateNormals();
            _mesh.RecalculateBounds();

            _mf.sharedMesh = _mesh;

            // Cache for live-rebuild comparison
            _prevInner    = _innerRadius;
            _prevOuter    = _outerRadius;
            _prevStart    = _startAngleDeg;
            _prevSweep    = _sweepAngleDeg;
            _prevSegments = _segments;
        }

        // -----------------------------------------------------------------------
        // Public API — MotifNode drives the arc parameters at runtime
        // -----------------------------------------------------------------------

        /// <summary>
        /// Reconfigure the arc to cover a fraction of 360°, starting at startDeg.
        /// Call this after instantiating the prefab, before it becomes visible.
        /// </summary>
        public void SetArc(float startDeg, float sweepDeg,
                           float innerR = -1, float outerR = -1)
        {
            _startAngleDeg = startDeg;
            _sweepAngleDeg = sweepDeg;
            if (innerR >= 0) _innerRadius = innerR;
            if (outerR >= 0) _outerRadius = outerR;
            BuildMesh();
        }

        /// <summary>
        /// Assign a material colour directly.
        /// </summary>
        public void SetColor(Color color)
        {
            if (_mr == null) _mr = GetComponent<MeshRenderer>();
            // Use MaterialPropertyBlock to avoid shared-material stomping
            var mpb = new MaterialPropertyBlock();
            _mr.GetPropertyBlock(mpb);
            mpb.SetColor("_Color", color);
            _mr.SetPropertyBlock(mpb);
        }

        // -----------------------------------------------------------------------
        // Editor helper — preview in Scene view
        // -----------------------------------------------------------------------
#if UNITY_EDITOR
        void OnDrawGizmosSelected()
        {
            UnityEditor.Handles.color = Color.cyan;
            float startRad = _startAngleDeg * Mathf.Deg2Rad;
            float sweepRad = _sweepAngleDeg * Mathf.Deg2Rad;

            for (int i = 0; i < 32; i++)
            {
                float a0  = startRad + (float)i       / 32 * sweepRad;
                float a1  = startRad + (float)(i + 1) / 32 * sweepRad;
                UnityEditor.Handles.DrawLine(
                    transform.position + new Vector3(Mathf.Cos(a0) * _outerRadius, 0, Mathf.Sin(a0) * _outerRadius),
                    transform.position + new Vector3(Mathf.Cos(a1) * _outerRadius, 0, Mathf.Sin(a1) * _outerRadius)
                );
                UnityEditor.Handles.DrawLine(
                    transform.position + new Vector3(Mathf.Cos(a0) * _innerRadius, 0, Mathf.Sin(a0) * _innerRadius),
                    transform.position + new Vector3(Mathf.Cos(a1) * _innerRadius, 0, Mathf.Sin(a1) * _innerRadius)
                );
            }
        }
#endif
    }
}
