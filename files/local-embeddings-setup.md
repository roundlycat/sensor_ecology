# Local Embeddings Setup — sensor_ecology on Raspberry Pi 5

## Overview

This document records the setup of local semantic embeddings for the `sensor_ecology`
project using `sentence-transformers` on a Raspberry Pi 5 (aarch64, Debian Bookworm,
Python 3.11.2).

The goal: every observation stored by an agent carries a 384-dimensional embedding of
its `semantic_summary` field, enabling pgvector similarity queries via `find_similar()`.

---

## Decision: Local vs. API

| Option | Chosen | Reason |
|---|---|---|
| Claude API embeddings | No | Network dependency, latency, cost per call |
| Local `sentence-transformers` | **Yes** | Runs fully on-device, fast enough on Pi 5 CPU |
| Hailo NPU | Future option | Designed for this workload; keep as optimisation path once baseline is stable |

Model selected: `all-MiniLM-L6-v2`
- 384-dimensional output
- ~22M parameters — well within Pi 5 RAM
- Good quality/speed trade-off for short descriptive sentences

---

## Installation

Packages go to `~/.local/lib/python3.11/site-packages/` (user site, no root required).
This path is ahead of `/usr/lib/python3/dist-packages/` in `sys.path`, so user-installed
versions take precedence over system packages.

```bash
pip3 install sentence-transformers --break-system-packages
```

This pulled in (among others):
- `torch 2.10.0` (aarch64 wheel from PyPI)
- `transformers 5.2.0`
- `sentence-transformers 5.2.3`
- `scikit-learn`, `scipy`, `numpy 2.4.2`, `huggingface-hub 1.4.1`

### filelock conflict fix

`huggingface_hub 1.4.1` requires `filelock >= 3.13` (uses the `mode=` kwarg in
`FileLock()`). Debian Bookworm ships `filelock 3.9.0` system-wide, which lacks this
parameter. Installing a newer version into user site-packages resolved it:

```bash
pip3 install "filelock>=3.13" --upgrade --break-system-packages
# Result: filelock 3.24.3 installed to ~/.local/
```

Verify the correct version is active:
```bash
python3 -c "import filelock; print(filelock.__version__, filelock.__file__)"
# Expected: 3.24.3  /home/sean/.local/lib/python3.11/site-packages/filelock/__init__.py
```

---

## Code changes — `agent.py`

### 1. Module-level lazy singleton (`agent.py`, after imports)

```python
_embedder = None
_embedder_lock = threading.Lock()

def _get_embedder():
    """Load all-MiniLM-L6-v2 once; reuse across all agents in this process."""
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                from sentence_transformers import SentenceTransformer
                log.info("Loading embedding model all-MiniLM-L6-v2 ...")
                _embedder = SentenceTransformer("all-MiniLM-L6-v2")
                log.info("Embedding model ready (384-dim)")
    return _embedder
```

**Why a singleton?** Multiple agent instances may run in the same process. Loading the
model (~22M params) once and sharing it avoids redundant memory use and the ~1–2 second
load time per agent.

**Why lazy?** The model only loads when the first observation is actually embedded, not
at import time. This keeps startup fast and avoids loading the model in processes that
don't need embeddings (e.g. tests, queries).

**Why double-checked locking?** The outer `if _embedder is None` avoids acquiring the
lock on every call after initialisation. The inner check guards against two threads both
passing the outer check simultaneously on first call.

### 2. `Agent.embed()` method

Replaced the stub:
```python
# Before
def embed(self, text: str) -> Optional[list]:
    """Override to provide embeddings. Default returns None (no embedding).
    Step 3 will fill this in with Claude API or local model.
    """
    return None
```

With:
```python
# After
def embed(self, text: str) -> Optional[list]:
    """Return a 384-dim embedding for text using all-MiniLM-L6-v2."""
    return _get_embedder().encode(text).tolist()
```

The `.tolist()` converts the numpy `float32` array to a plain Python `list[float]`,
which is what `_vec_literal()` expects before inserting into Postgres as a `vector`.

---

## How it flows

```
Agent._loop()
  └─ interpret(raw) → ObservationMessage (with semantic_summary)
       └─ embed(obs.semantic_summary) → list[float] (384 dims)
            └─ _store_observation(conn, obs, embedding)
                 └─ INSERT ... embedding::vector INTO observations
```

Once stored, `find_similar(conn, embedding)` uses pgvector cosine similarity to find
observations from other agents that are semantically related.

---

## Verification

```python
from sentence_transformers import SentenceTransformer
import numpy as np

m = SentenceTransformer('all-MiniLM-L6-v2')

e1 = m.encode('cooling fan stopped working')
e2 = m.encode('temperature rising after fan failure')
e3 = m.encode('stock market closed today')

def cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

print(cos(e1, e2))  # 0.683 — related sensor events
print(cos(e1, e3))  # 0.198 — unrelated topic
```

---

## Model cache location

On first run the model weights are downloaded from Hugging Face and cached at:
```
~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/
```
Subsequent runs load from disk — no network required.

---

## Known warnings (safe to ignore)

| Warning | Cause | Action |
|---|---|---|
| `UNEXPECTED: embeddings.position_ids` | Standard mismatch between base BERT checkpoint and sentence-transformers wrapper | None — harmless |
| `You are sending unauthenticated requests to the HF Hub` | No `HF_TOKEN` set | None needed; model is public and already cached |

---

## Future: Hailo NPU path

The Hailo-8L on the Pi 5 AI Kit is designed for exactly this kind of inference workload.
Once the CPU baseline is stable, `all-MiniLM-L6-v2` (or a similar ONNX-exportable model)
could be compiled for the Hailo runtime, moving embedding inference off the CPU entirely
and freeing it for agent coordination work.

Entry point: `hailo-rpi5-examples/` already present at `/home/sean/hailo-rpi5-examples/`.
