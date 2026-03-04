// SimTapReceiver.cs
// Connects to the Pi5 relay API simulation WebSocket so that touches
// on the physical touchscreen can trigger AR motif node taps.
//
// Attach to any GameObject in the scene (e.g. your GraphRoot or Networking manager).

using System;
using System.Collections;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;

namespace AgentPerception
{
    public class SimTapReceiver : MonoBehaviour
    {
        [Header("Relay Setup")]
        [SerializeField] string _relayHost = "192.168.1.100";
        [SerializeField] int    _relayPort = 8765;

        [Header("References")]
        [SerializeField] MotifGraphScene _graphScene;
        
        // Custom simple wait for SSE string logic since Unity's built-in WebSockets
        // often require native plugins. We'll use polling or simple HTTP for the sim
        // right now as a fallback, or if you prefer true WebSockets on mobile, consider
        // checking com.unity.modules.websockets / third party.
        // For simplicity in a prototype without external WebSocket libraries:
        // we'll implement a long-poll or simple request to a /api/sim/events/poll endpoint,
        // or just use conventional UnityWebRequest if we implemented SSE here.

        Coroutine _simLoop;

        void Start()
        {
            if (_graphScene == null)
                _graphScene = FindObjectOfType<MotifGraphScene>();

            _simLoop = StartCoroutine(SimPollLoop());
        }

        void OnDisable()
        {
            if (_simLoop != null)
                StopCoroutine(_simLoop);
        }

        IEnumerator SimPollLoop()
        {
            // Simple long-polling fallback so we don't need a C# WebSocket lib just yet.
            // The relay API will hold this request open until a tap occurs.
            while (true)
            {
                var url = $"http://{_relayHost}:{_relayPort}/api/sim/poll";
                using (var req = UnityWebRequest.Get(url))
                {
                    req.timeout = 20; // 20s long-poll timeout
                    yield return req.SendWebRequest();

                    if (req.result == UnityWebRequest.Result.Success)
                    {
                        var response = JsonUtility.FromJson<SimTapEvent>(req.downloadHandler.text);
                        if (response != null && response.type == "tap" && !string.IsNullOrEmpty(response.motif_id))
                        {
                            SimulateTap(response.motif_id);
                        }
                    }
                }
                
                // Yield briefly before next long-poll
                yield return new WaitForSeconds(0.1f);
            }
        }

        void SimulateTap(string motifId)
        {
            if (_graphScene == null) return;
            
            // We need a specific motif node. Let's find it by the name injected in MotifNode.cs
            var nodeTransform = _graphScene.GetNodeTransform(motifId);
            if (nodeTransform != null)
            {
                var motifNode = nodeTransform.GetComponent<MotifNode>();
                if (motifNode != null)
                {
                    Debug.Log($"[SimTapReceiver] Simulating tap on motif: {motifId}");
                    motifNode.OnTapped();
                }
            }
            else
            {
                Debug.LogWarning($"[SimTapReceiver] Received tap for motif {motifId} but it's not active in the scene.");
            }
        }

        [Serializable]
        class SimTapEvent
        {
            public string type;
            public string motif_id;
        }
    }
}
