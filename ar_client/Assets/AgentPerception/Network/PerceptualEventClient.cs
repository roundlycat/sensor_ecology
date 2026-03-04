// PerceptualEventClient.cs
// HTTP/SSE client for the Pi 5 relay API.
// Handles polling, SSE streaming, connection health, and retry backoff.
//
// Place in: Assets/AgentPerception/Network/

using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;

namespace AgentPerception
{
    /// <summary>
    /// Low-level client for the Pi 5 relay API.
    /// Does not dispatch events itself — feeds raw data to PerceptualEventBus.
    /// </summary>
    public class PerceptualEventClient : MonoBehaviour
    {
        // -----------------------------------------------------------------------
        // Configuration (set in Inspector or via AgentPerceptionConfig)
        // -----------------------------------------------------------------------

        [Header("Relay API")]
        [SerializeField] string  _relayHost    = "192.168.1.100";   // Pi 5 LAN IP
        [SerializeField] int     _relayPort    = 8765;
        [SerializeField] string  _nodeId       = "";                 // UUID of target agent
        [SerializeField] float   _pollInterval = 1.0f;              // seconds between polls
        [SerializeField] int     _pollBatchSize = 50;               // events per request

        [Header("Resilience")]
        [SerializeField] float _baseBackoffS   = 1.0f;
        [SerializeField] float _maxBackoffS    = 30.0f;
        [SerializeField] float _timeoutS       = 10.0f;

        // -----------------------------------------------------------------------
        // Events — PerceptualEventBus subscribes to these
        // -----------------------------------------------------------------------

        public event Action<PerceptualEvent[]> OnEventsReceived;
        public event Action<AgentVitals>       OnVitalsReceived;
        public event Action<bool>              OnConnectionChanged;  // true = connected

        // -----------------------------------------------------------------------
        // Internal state
        // -----------------------------------------------------------------------

        string  _baseUrl;
        string  _cursor;        // ISO datetime — used to request only new events
        bool    _connected;
        int     _consecutiveFailures;
        Coroutine _pollCoroutine;
        Coroutine _vitalsCoroutine;

        // -----------------------------------------------------------------------
        // Lifecycle
        // -----------------------------------------------------------------------

        void Awake()
        {
            _baseUrl = $"http://{_relayHost}:{_relayPort}";
        }

        void OnEnable()
        {
            _pollCoroutine   = StartCoroutine(PollLoop());
            _vitalsCoroutine = StartCoroutine(VitalsLoop());
        }

        void OnDisable()
        {
            if (_pollCoroutine   != null) StopCoroutine(_pollCoroutine);
            if (_vitalsCoroutine != null) StopCoroutine(_vitalsCoroutine);
        }

        // -----------------------------------------------------------------------
        // Public API
        // -----------------------------------------------------------------------

        public void SetNode(string nodeId)
        {
            _nodeId = nodeId;
            _cursor  = null;   // reset cursor when switching nodes
        }

        public void RequestEchoes(string motifId, float threshold, Action<string> onJson)
        {
            StartCoroutine(FetchEchoes(motifId, threshold, onJson));
        }

        // -----------------------------------------------------------------------
        // Polling loop
        // -----------------------------------------------------------------------

        IEnumerator PollLoop()
        {
            while (true)
            {
                yield return FetchEvents();
                float backoff = ComputeBackoff();
                yield return new WaitForSeconds(
                    _consecutiveFailures == 0 ? _pollInterval : backoff
                );
            }
        }

        IEnumerator FetchEvents()
        {
            var url = BuildEventUrl();
            using var request = UnityWebRequest.Get(url);
            request.timeout = (int)_timeoutS;
            yield return request.SendWebRequest();

            if (request.result != UnityWebRequest.Result.Success)
            {
                HandleFailure($"Events fetch failed: {request.error}");
                yield break;
            }

            HandleSuccess();

            var wrapper = JsonUtility.FromJson<EventListResponse>(request.downloadHandler.text);
            if (wrapper?.events == null || wrapper.events.Length == 0)
                yield break;

            // Hydrate and advance cursor
            foreach (var ev in wrapper.events)
            {
                ev.Hydrate();
                if (ev.NearestResonance != null) ev.NearestResonance.Hydrate();
            }

            if (!string.IsNullOrEmpty(wrapper.cursor))
                _cursor = wrapper.cursor;

            OnEventsReceived?.Invoke(wrapper.events);
        }

        string BuildEventUrl()
        {
            var sb = new StringBuilder($"{_baseUrl}/api/events?limit={_pollBatchSize}");
            if (!string.IsNullOrEmpty(_nodeId))  sb.Append($"&node_id={_nodeId}");
            if (!string.IsNullOrEmpty(_cursor))  sb.Append($"&since={Uri.EscapeDataString(_cursor)}");
            return sb.ToString();
        }

        // -----------------------------------------------------------------------
        // Vitals loop  (slower — 5s)
        // -----------------------------------------------------------------------

        IEnumerator VitalsLoop()
        {
            while (true)
            {
                if (!string.IsNullOrEmpty(_nodeId))
                    yield return FetchVitals();
                yield return new WaitForSeconds(5f);
            }
        }

        IEnumerator FetchVitals()
        {
            var url = $"{_baseUrl}/api/agent/{_nodeId}/vitals";
            using var request = UnityWebRequest.Get(url);
            request.timeout = (int)_timeoutS;
            yield return request.SendWebRequest();

            if (request.result != UnityWebRequest.Result.Success)
                yield break;

            var vitals = JsonUtility.FromJson<AgentVitals>(request.downloadHandler.text);
            if (vitals != null)
                OnVitalsReceived?.Invoke(vitals);
        }

        // -----------------------------------------------------------------------
        // Echo query (one-shot)
        // -----------------------------------------------------------------------

        IEnumerator FetchEchoes(string motifId, float threshold, Action<string> callback)
        {
            var url = $"{_baseUrl}/api/motifs/{motifId}/echoes?threshold={threshold:F2}";
            using var request = UnityWebRequest.Get(url);
            request.timeout = (int)_timeoutS;
            yield return request.SendWebRequest();

            if (request.result == UnityWebRequest.Result.Success)
                callback?.Invoke(request.downloadHandler.text);
        }

        // -----------------------------------------------------------------------
        // Connection health
        // -----------------------------------------------------------------------

        void HandleSuccess()
        {
            _consecutiveFailures = 0;
            if (!_connected)
            {
                _connected = true;
                OnConnectionChanged?.Invoke(true);
            }
        }

        void HandleFailure(string message)
        {
            _consecutiveFailures++;
            Debug.LogWarning($"[PerceptualEventClient] {message} (failure #{_consecutiveFailures})");
            if (_connected && _consecutiveFailures >= 3)
            {
                _connected = false;
                OnConnectionChanged?.Invoke(false);
            }
        }

        float ComputeBackoff()
        {
            float backoff = _baseBackoffS * Mathf.Pow(2, _consecutiveFailures - 1);
            return Mathf.Min(backoff, _maxBackoffS);
        }
    }
}
