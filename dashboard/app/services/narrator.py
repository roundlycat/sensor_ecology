"""
Ollama narrator service.

Generates short, grounded interpretive field notes from recent perceptual
events + active motifs + cross-corpus echoes.  The narrator is designed to:
  - Be concrete and specific (not generic poetic prose)
  - Use motif vocabulary only where it genuinely resonates
  - Surface cross-corpus connections when they are close enough to be meaningful
  - Produce 2-4 sentences at most — enough to say something, not enough to fill the room

The cached narrative is refreshed:
  - On demand when GET /api/corpus/narrative is called
  - Automatically every NARRATOR_INTERVAL_S seconds if any events have arrived
  - Immediately after a cross-domain event (confidence: high)

Requires Ollama running locally with a generative model loaded, e.g.:
    ollama pull llama3.2
    ollama pull phi3.5   # lighter, ~2 GB, good on Pi 5

Set NARRATOR_MODEL to the model tag (default: llama3.2).
"""

import asyncio
import json
import logging
import time
from typing import Optional

import aiohttp

from app.config import OLLAMA_HOST, NARRATOR_MODEL, NARRATOR_INTERVAL_S
from app.db import queries as db

logger = logging.getLogger(__name__)


# ── Cached narrative state ────────────────────────────────────────────────────

_cache: dict = {
    "text":       None,
    "generated_at": None,
    "is_running": False,
    "error":      None,
}


def get_cached_narrative() -> dict:
    return {
        "text":         _cache["text"],
        "generated_at": _cache["generated_at"],
        "is_running":   _cache["is_running"],
        "model":        NARRATOR_MODEL,
        "ollama_host":  OLLAMA_HOST,
    }


# ── Prompt construction ───────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a field observer for a distributed sensor ecology running on a "
    "Raspberry Pi 5 in Whitehorse, Yukon. You receive structured perceptual data "
    "and produce a brief, concrete field note — 2 to 4 sentences. "
    "Do not use generic poetic language. Ground every sentence in the specific "
    "data you are given. Use the motif vocabulary only where it genuinely applies."
)

_USER_TEMPLATE = """\
Recent perceptual events (newest first):
{events}

Active motifs — recurrent patterns from the conversation archive:
{motifs}
{corpus_section}
Write a field note describing what the ecology is experiencing right now. \
Be specific. Be brief. Use motif language only where it resonates with the data."""


def _fmt_events(events: list[dict]) -> str:
    if not events:
        return "  (none recorded)"
    lines = []
    for e in events[:12]:  # cap at 12 to stay within context window
        domain = e.get("domain", "?")
        label  = (e.get("event_label") or "").replace("_", " ")
        conf   = e.get("confidence", "")
        node   = e.get("node_name", "")
        snap   = e.get("feature_snapshot") or {}
        # Pull the most informative channel value if available
        channels = snap.get("channels", {}) if isinstance(snap, dict) else {}
        channel_str = ""
        for key in ("temperature", "humidity", "pressure", "proximity_raw",
                    "power_mw", "presence_score", "max_temp_c"):
            if key in channels and channels[key] is not None:
                channel_str = f" [{key.replace('_', ' ')}: {channels[key]:.1f}]"
                break
        cross = " [cross-domain]" if e.get("is_cross_domain") else ""
        lines.append(f"  {domain} · {label}{channel_str} · {conf}{cross} ({node})")
    return "\n".join(lines)


def _fmt_motifs(motifs: list[dict]) -> str:
    if not motifs:
        return "  (none active)"
    lines = []
    for m in motifs[:5]:
        n    = m.get("resonance_count", 0)
        dist = m.get("min_distance")
        dist_str = f", closest distance {dist:.3f}" if dist is not None else ""
        lines.append(f"  '{m['label']}' — {n} echo(s){dist_str}")
    return "\n".join(lines)


def _fmt_corpus(echoes: list[dict]) -> str:
    if not echoes:
        return ""
    lines = ["Cross-corpus echoes — conversation passages that resonate with recent events:"]
    for e in echoes[:3]:
        sim  = e.get("similarity", 0)
        src  = e.get("source", "archive")
        text = (e.get("chunk_text") or "").strip().replace("\n", " ")
        if len(text) > 200:
            text = text[:197] + "…"
        lines.append(f"  [{src} · sim {sim:.2f}] "{text}"")
    return "\n" + "\n".join(lines) + "\n"


def _build_prompt(
    events: list[dict],
    motifs: list[dict],
    corpus_echoes: list[dict],
) -> str:
    return _USER_TEMPLATE.format(
        events=_fmt_events(events),
        motifs=_fmt_motifs(motifs),
        corpus_section=_fmt_corpus(corpus_echoes),
    )


# ── Ollama call ───────────────────────────────────────────────────────────────

async def _call_ollama(prompt: str) -> str:
    url = f"{OLLAMA_HOST.rstrip('/')}/api/generate"
    payload = {
        "model":  NARRATOR_MODEL,
        "prompt": prompt,
        "system": _SYSTEM_PROMPT,
        "stream": False,
        "options": {
            "temperature": 0.5,
            "num_predict": 180,   # enough for 3-4 sentences
            "top_p": 0.9,
        },
    }
    timeout = aiohttp.ClientTimeout(total=120)  # Pi 5 may need up to 2 min for first token
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("response", "").strip()


# ── Public: generate (with caching) ──────────────────────────────────────────

async def generate_narrative(force: bool = False) -> dict:
    """
    Generate a new field narrative if:
      - force=True
      - No narrative has been generated yet
      - Cache is older than NARRATOR_INTERVAL_S

    Returns the cache dict (with is_running=True while generation is in progress).
    """
    now = time.time()
    stale = (
        _cache["generated_at"] is None or
        (now - _cache["generated_at"]) > NARRATOR_INTERVAL_S
    )

    if not force and not stale:
        return get_cached_narrative()

    if _cache["is_running"]:
        return get_cached_narrative()  # already composing

    # Launch async generation without blocking the caller
    asyncio.create_task(_generate_in_background())
    return get_cached_narrative()


async def _generate_in_background() -> None:
    _cache["is_running"] = True
    _cache["error"] = None
    try:
        # Gather context in parallel
        events_task = asyncio.create_task(
            db.get_perceptual_events(limit=20)
        )
        motifs_task = asyncio.create_task(
            db.get_active_motifs(limit=10)
        )
        events, motifs = await asyncio.gather(events_task, motifs_task)

        # Try to get cross-corpus echoes for the most recent event
        corpus_echoes: list[dict] = []
        if events:
            from app.db.corpus_queries import find_corpus_echoes_for_event
            try:
                corpus_echoes = await find_corpus_echoes_for_event(
                    str(events[0]["id"]), limit=3, threshold=0.40
                )
            except Exception as e:
                logger.debug("Corpus echo lookup skipped: %s", e)

        prompt   = _build_prompt(events, motifs, corpus_echoes)
        text     = await _call_ollama(prompt)

        _cache["text"]         = text
        _cache["generated_at"] = time.time()
        logger.info("Narrator: generated %d chars", len(text))

    except Exception as e:
        _cache["error"] = str(e)
        logger.warning("Narrator generation failed: %s", e)
    finally:
        _cache["is_running"] = False


# ── Background refresh loop ───────────────────────────────────────────────────

async def start_narrator_loop() -> None:
    """
    Run as a background task.  Triggers generate_narrative() when stale and
    the ecology has seen at least one event in the last interval.
    """
    if not NARRATOR_MODEL or not OLLAMA_HOST:
        logger.info("Narrator disabled (NARRATOR_MODEL or OLLAMA_HOST not set)")
        return

    logger.info(
        "Narrator loop started — model: %s, interval: %ds",
        NARRATOR_MODEL, NARRATOR_INTERVAL_S,
    )

    # Initial generation on startup (non-blocking)
    await asyncio.sleep(15)  # let ingestion services settle first
    await generate_narrative(force=True)

    while True:
        await asyncio.sleep(NARRATOR_INTERVAL_S)
        try:
            # Only regenerate if there have been recent events
            recent = await db.get_perceptual_events(limit=1)
            if recent:
                await generate_narrative(force=True)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("Narrator loop error: %s", e)
