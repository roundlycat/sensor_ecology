// BillboardLabel.cs
// Makes a child TextMeshPro label always face the main camera.
// Attach to the Text child object inside MotifNodePrefab.

using UnityEngine;

namespace AgentPerception
{
    public class BillboardLabel : MonoBehaviour
    {
        void LateUpdate()
        {
            if (Camera.main == null) return;
            transform.LookAt(Camera.main.transform);
            transform.Rotate(0, 180f, 0); // flip to face camera correctly
        }
    }
}
