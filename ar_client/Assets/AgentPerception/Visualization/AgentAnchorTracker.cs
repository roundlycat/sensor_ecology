// AgentAnchorTracker.cs
// Looks for a reference image (e.g. ArUco/QR printed on the Pi5 case).
// When detected, updates the shared anchor transform so AgentPerceptionVisualizer
// and MotifGraphScene lock to the physical hardware in the real world.
//
// Attach to the AR Session Origin GameObject.

using UnityEngine;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

namespace AgentPerception
{
    [RequireComponent(typeof(ARTrackedImageManager))]
    public class AgentAnchorTracker : MonoBehaviour
    {
        [Header("Anchor Target")]
        [Tooltip("The actual transform that the rest of the app uses as the Pi5 origin.")]
        [SerializeField] Transform _agentAnchor; 

        // The name of the image in your ReferenceImageLibrary (e.g., "pi5_marker")
        [SerializeField] string _markerName = "pi5_marker";

        ARTrackedImageManager _imageManager;
        bool _anchorLocked = false;

        void Awake()
        {
            _imageManager = GetComponent<ARTrackedImageManager>();
        }

        void OnEnable()
        {
            if (_imageManager != null)
                _imageManager.trackedImagesChanged += OnTrackedImagesChanged;
        }

        void OnDisable()
        {
            if (_imageManager != null)
                _imageManager.trackedImagesChanged -= OnTrackedImagesChanged;
        }

        void OnTrackedImagesChanged(ARTrackedImagesChangedEventArgs args)
        {
            // Update anchor based on image detection
            foreach (var trackedImage in args.added)
                UpdateAnchor(trackedImage);
            
            foreach (var trackedImage in args.updated)
                UpdateAnchor(trackedImage);

            // Note: If tracking falls back to Limited/None, we usually keep the anchor 
            // where it was last seen, as ARCore/ARKit tends to drift slightly but 
            // retaining the physical origin is better than having the graph vanish.
        }

        void UpdateAnchor(ARTrackedImage trackedImage)
        {
            // Only care about our specific Pi5 marker
            if (trackedImage.referenceImage.name != _markerName)
                return;

            if (trackedImage.trackingState == TrackingState.Tracking)
            {
                if (_agentAnchor != null)
                {
                    // Move the central anchor point to match the physical printed marker on the Pi5
                    _agentAnchor.position = trackedImage.transform.position;
                    // Usually you want to align Y-axis, but ignore full rotation to 
                    // prevent the graph from tilting if the marker is read at a strange angle.
                    // This forces the "up" vector to stay true to gravity.
                    Vector3 forwardFlat = trackedImage.transform.up;
                    forwardFlat.y = 0;
                    if (forwardFlat.sqrMagnitude > 0.001f)
                        _agentAnchor.rotation = Quaternion.LookRotation(forwardFlat, Vector3.up);

                    if (!_anchorLocked)
                    {
                        Debug.Log($"[AgentAnchorTracker] Locked onto physical Pi5 marker: {_markerName}");
                        _anchorLocked = true;
                    }
                }
            }
        }

        /// <summary>
        /// Fallback: if the marker can't be found, call this (e.g., via a UI button)
        /// to place the anchor directly in front of the camera using a floor raycast.
        /// </summary>
        public void ForceManualAnchor()
        {
            if (Camera.main != null && _agentAnchor != null)
            {
                // Place it 0.8m in front of the user, roughly on the floor
                var camPos = Camera.main.transform.position;
                var camFwd = Camera.main.transform.forward;
                camFwd.y = 0; // flatten

                _agentAnchor.position = camPos + (camFwd.normalized * 0.8f) - new Vector3(0, 1.2f, 0); // approx desk/floor height
                _agentAnchor.rotation = Quaternion.LookRotation((_agentAnchor.position - camPos).normalized, Vector3.up);
                
                Debug.Log("[AgentAnchorTracker] Manual override forced. Placing anchor centrally.");
                _anchorLocked = true;

                // Stop the image manager to save battery since we've manually placed it
                if (_imageManager != null)
                    _imageManager.enabled = false;
            }
        }
    }
}
