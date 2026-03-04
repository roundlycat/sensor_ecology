# Motif Graph Scene — Unity Setup Guide

Agent Perception Stack · Whitehorse YT · 2026

---

## Overview

The graph scene adds an AR node cloud around the agent anchor.
Each node represents a linguistic motif from your 18-month conversation corpus.
Node size encodes recurrence count; the domain breakdown ring shows which sensor
domains (environmental, embodied, relational) most often physically resonate
with that motif. Tap a node to open the echo panel listing matching events.

---

## Files

| File | Location in Assets |
|------|-------------------|
| `MotifNode.cs`         | AgentPerception/Graph/ |
| `MotifGraphScene.cs`   | AgentPerception/Graph/ |
| `ProceduralArcMesh.cs` | AgentPerception/Graph/ |

---

## Step 1 — Layer setup

In **Edit → Project Settings → Tags and Layers**, add a layer called `MotifNode`.
Note the layer number (e.g. layer 8 = value 256 in the LayerMask).

---

## Step 2 — Create the RingSegment prefab

This is the arc mesh used for the domain breakdown ring around each node.

1. Create an empty GameObject: **GameObject → Create Empty** → name it `RingSegmentPrefab`
2. Add component: **ProceduralArcMesh**
   - Inner Radius: `0.045`
   - Outer Radius: `0.055`
   - Start Angle: `0`
   - Sweep Angle: `90` (MotifNode will override at runtime)
   - Segments: `24`
3. Add component: **MeshRenderer**
   - Assign a simple Unlit/Color material (or use `Sprites/Default`)
4. **Drag to your Prefabs folder** → drag from the scene to
   `Assets/AgentPerception/Prefabs/RingSegmentPrefab.prefab`
5. Delete the scene instance

---

## Step 3 — Create the MotifNode prefab

Hierarchy structure:

```
MotifNodePrefab             ← root; has MotifNode.cs, SphereCollider (layer = MotifNode)
  ├── Core                  ← MeshRenderer (sphere ~0.03m); assign to _coreRenderer
  ├── Glow                  ← MeshRenderer (sphere ~0.05m, alpha blended); _glowRenderer
  ├── LabelCanvas           ← Canvas (World Space, 0.001 units/pixel)
  │     └── LabelText       ← TextMeshPro; _labelText (font size ~0.4m equivalent)
  │     └── CountText       ← TextMeshPro; _countText, smaller, below label
  └── RingContainer         ← empty Transform; _ringContainer
```

**MotifNode inspector wiring:**

| Field | Target |
|-------|--------|
| `_coreRenderer`       | Core / MeshRenderer |
| `_glowRenderer`       | Glow / MeshRenderer |
| `_labelText`          | LabelCanvas / LabelText |
| `_countText`          | LabelCanvas / CountText |
| `_ringContainer`      | RingContainer transform |
| `_ringSegmentPrefab`  | RingSegmentPrefab (from Step 2) |
| `_tapRadius`          | 0.06 |
| `_hoverScale`         | 1.15 |
| `_selectedScale`      | 1.30 |

Set the **layer** of the root GameObject and all children to `MotifNode`.

Add a **SphereCollider** to the root:
- Radius: `0.04`
- Is Trigger: off

Save as prefab: `Assets/AgentPerception/Prefabs/MotifNodePrefab.prefab`

---

## Step 4 — Materials

You need two materials on the MotifNode prefab.

**Core material**  (`MotifCore.mat`)
- Shader: `Universal Render Pipeline/Lit` or `Unlit/Color`
- Color: white (MotifNode will override per domain at runtime)
- Smoothness: 0.7

**Glow material**  (`MotifGlow.mat`)
- Shader: `Universal Render Pipeline/Lit` with alpha blending, or `Sprites/Default`
- Color: white, Alpha ~0.12
- Surface Type: Transparent
- Render Face: Both

**Ring segment material**  (`MotifRing.mat`)
- Shader: `Unlit/Color` or `Universal Render Pipeline/Unlit`
- Color: white (ProceduralArcMesh will override via MaterialPropertyBlock)

---

## Step 5 — Add MotifGraphScene to the scene

1. In your AR scene, locate the **Agent Anchor** GameObject
   (the tracked image target or manual anchor used by `AgentPerceptionVisualizer`)
2. Create a child: **GraphRoot** (empty)
3. Add **MotifGraphScene** component to GraphRoot

**MotifGraphScene inspector wiring:**

| Field | Value / Target |
|-------|----------------|
| `_graphRoot`         | GraphRoot transform (self) |
| `_innerRadius`       | 0.12 |
| `_ringSpacing`       | 0.09 |
| `_nodesPerRing`      | 8 |
| `_motifNodePrefab`   | MotifNodePrefab (Step 3) |
| `_resonanceRenderer` | MotifResonanceRenderer in scene |
| `_bus`               | PerceptualEventBus singleton (leave null → auto-found) |
| `_relayHost`         | Pi 5 IP on your local network (e.g. `192.168.1.xx`) |
| `_relayPort`         | 8765 |
| `_minRecurrences`    | 0 (raise to 1+ once the motifs table is populated) |
| `_refreshIntervalS`  | 30 |
| `_arRaycastManager`  | ARRaycastManager in scene |
| `_nodeLayer`         | MotifNode layer (LayerMask) |

---

## Step 6 — Connect MotifResonanceRenderer

`MotifGraphScene` registers node transforms with `MotifResonanceRenderer` automatically
when nodes spawn. Verify that the `MotifResonanceRenderer` in your scene already has:

- `_echoPanel` assigned (the UI panel for echo results)
- `_client` assigned (PerceptualEventClient)

These should already be wired from the previous `AgentPerceptionVisualizer` session.

---

## Step 7 — First run checklist

Before hitting Play:

- [ ] Pi 5 is on the same network; relay running: `uvicorn relay_api:app --host 0.0.0.0 --port 8765`
- [ ] `/api/motifs` returns JSON (test with curl from your dev machine)
- [ ] `MotifNode` layer is set on prefab root **and all children**
- [ ] `_nodeLayer` mask in MotifGraphScene inspector includes the MotifNode layer
- [ ] RingSegmentPrefab is assigned in `_ringSegmentPrefab`
- [ ] `_resonanceRenderer` reference is set (not null)

When the scene starts, MotifGraphScene fetches `/api/motifs` and spawns nodes.
With an empty motifs table you will see no nodes — that is correct.
Populate the motifs table from your conversation archive, then tap **Refresh** or wait 30s.

---

## Interaction flow

```
User taps node
    ↓
MotifGraphScene.HandleTapInput()
    ↓ Physics.Raycast on MotifNode layer
MotifNode.OnTapped()
    ↓ if not selected:
MotifNode.Select()
    ↓
MotifResonanceRenderer.ShowEchoPanel(motif_id, label)
    ↓
GET /api/motifs/{id}/echoes → populates echo panel list
```

Tapping empty space or the same node again deselects and hides the panel.

---

## Known constraints

- **Motifs table must be seeded** from the conversation corpus before any nodes appear.
  The DriftUpdater (planned) will eventually maintain centroids automatically.
- **Domain ring** is built from the echoes endpoint, not a dedicated stats endpoint.
  When the `/api/motifs/{id}/stats` endpoint is wired into Unity, `FetchAndBuildRing`
  can be upgraded to use it directly for more accurate breakdowns.
- **Force layout** is very lightweight (O(n²) repulsion) — fine for n < 80.
  Beyond that, switch to a Barnes-Hut approximation or a fixed orbital layout.
- **SSE streaming** is not used for motif updates — nodes poll at `_refreshIntervalS`.
  Live recurrence events arrive via EventBus, so glyph pulses are real-time even
  though the node list refreshes slowly.

---

## Planned

| Item | Notes |
|------|-------|
| MotifDriftUpdater (Python) | Recomputes centroids as events accrete |
| `/api/motifs/{id}/stats` wired to Unity | Direct domain breakdown per motif |
| Thermal comms protocol | CPU load modulation as inter-agent signal |
| Motif-to-motif arcs | Semantic similarity edges between nodes |
