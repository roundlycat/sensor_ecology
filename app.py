"""
Sensor Ecology — Live Feed Server
FastAPI + SSE + vanilla JS, single-file.

Run:
    uvicorn app:app --port 8765 --reload
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse

_pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = await asyncpg.create_pool(
        "postgresql:///sensor_ecology",
        min_size=1,
        max_size=5,
    )
    yield
    await _pool.close()


app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_COLS = """
    c.id::text,
    c.title,
    c.description,
    c.priority,
    c.card_type,
    c.related_node,
    c.created_at,
    col.name AS column_name
FROM cards c
LEFT JOIN "columns" col ON col.id = c.column_id
"""

_SQL_RECENT = f"SELECT {_COLS} ORDER BY c.created_at DESC LIMIT $1"
_SQL_SINCE  = f"SELECT {_COLS} WHERE c.created_at > $1 ORDER BY c.created_at ASC"


def _row(row) -> dict:
    return {
        "id":           row["id"],
        "title":        row["title"] or "",
        "description":  row["description"] or "",
        "priority":     row["priority"] or "",
        "card_type":    row["card_type"] or "",
        "related_node": row["related_node"] or "",
        "created_at":   row["created_at"].isoformat(),
        "column_name":  row["column_name"] or "",
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/cards")
async def cards_history():
    async with _pool.acquire() as conn:
        rows = await conn.fetch(_SQL_RECENT, 20)
    result = [_row(r) for r in rows]
    result.reverse()          # oldest-first so the frontend appends in order
    return result


async def _sse(request: Request):
    async with _pool.acquire() as conn:
        seed = await conn.fetch(_SQL_RECENT, 1)
    last_ts = seed[0]["created_at"] if seed else datetime.now(timezone.utc)

    while True:
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            return

        if await request.is_disconnected():
            return

        try:
            async with _pool.acquire() as conn:
                rows = await conn.fetch(_SQL_SINCE, last_ts)
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            continue

        for r in rows:
            card = _row(r)
            last_ts = r["created_at"]
            yield f"data: {json.dumps(card)}\n\n"


@app.get("/stream")
async def stream(request: Request):
    return StreamingResponse(
        _sse(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Sensor Ecology · Feed</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: #0c0c0c;
  color: #909090;
  font-family: 'Courier New', Courier, monospace;
  font-size: 12px;
  line-height: 1.45;
  padding: 28px 20px;
}

header {
  max-width: 760px;
  display: flex;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid #1c1c1c;
  padding-bottom: 12px;
  margin-bottom: 20px;
}

header h1 {
  font-size: 10px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: #444;
  flex: 1;
}

#dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #252525;
  flex-shrink: 0;
  transition: background 0.4s, box-shadow 0.4s;
}
#dot.on  { background: #1b541b; box-shadow: 0 0 6px #1b541b; }
#dot.err { background: #541b1b; }

#count {
  font-size: 10px;
  color: #2e2e2e;
  letter-spacing: 0.05em;
  min-width: 5ch;
  text-align: right;
}

#feed {
  max-width: 760px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.card {
  background: #131313;
  border: 1px solid #1c1c1c;
  border-left: 3px solid #272727;
  border-radius: 2px;
  padding: 10px 14px;
  animation: drop .22s ease-out;
}

@keyframes drop {
  from { opacity: 0; transform: translateY(-9px); }
  to   { opacity: 1; transform: translateY(0);    }
}

.card-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 4px;
}

.card-title {
  color: #cccccc;
  font-weight: bold;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-time {
  color: #333333;
  font-size: 10px;
  white-space: nowrap;
  flex-shrink: 0;
}

.card-col {
  font-size: 9px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: #2c2c2c;
  margin-bottom: 4px;
}

.card-desc {
  color: #606060;
  font-size: 11px;
  word-break: break-word;
}

.n { color: #bf8414; font-weight: bold; }

.empty {
  color: #222222;
  text-align: center;
  padding: 52px 0;
  letter-spacing: 0.14em;
  font-size: 11px;
}
</style>
</head>
<body>

<header>
  <div id="dot"></div>
  <h1>Sensor Ecology &middot; Thermal Event Feed</h1>
  <span id="count"></span>
</header>

<div id="feed"><div class="empty">&#8212; awaiting events &#8212;</div></div>

<script>
const feed  = document.getElementById('feed');
const dot   = document.getElementById('dot');
const count = document.getElementById('count');
let total   = 0;

// ── util ──────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
       + '\u2009' + d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

// Highlight numbers (int or float, optionally negative) in amber.
// Input is already HTML-escaped so we only need to add our own tags.
function hlNums(text) {
  return esc(text).replace(/(-?\d+(?:\.\d+)?)/g, '<span class="n">$1</span>');
}

// ── temperature tint ──────────────────────────────────────────────────────

function maxTempFrom(card) {
  const m = card.description.match(/[Mm]ax\s+temp\s+([-\d.]+)/);
  return m ? parseFloat(m[1]) : null;
}

function tempStyle(t_c) {
  // 18 °C → barely tinted; ≥ 22 °C → deep red
  const t = Math.max(0, Math.min(1, (t_c - 18) / 4));
  // border-left: #272727 → #7a1414
  const lr = Math.round(39  + t * (122 - 39));
  const lg = Math.round(39  + t * (20  - 39));
  const lb = Math.round(39  + t * (20  - 39));
  // background: #131313 → #200b0b
  const br = Math.round(19  + t * (32  - 19));
  const bg = Math.round(19  + t * (11  - 19));
  const bb = Math.round(19  + t * (11  - 19));
  return `background:rgb(${br},${bg},${bb});border-left:3px solid rgb(${lr},${lg},${lb})`;
}

// ── card DOM ──────────────────────────────────────────────────────────────

function makeCard(card) {
  const el = document.createElement('div');
  el.className = 'card';
  el.dataset.id = card.id;

  const maxT = maxTempFrom(card);
  if (maxT !== null) el.setAttribute('style', tempStyle(maxT));

  const col = card.column_name
    ? `<div class="card-col">${esc(card.column_name)}</div>` : '';

  const meta = [card.related_node, card.priority]
    .filter(Boolean).map(esc).join(' &middot; ');

  el.innerHTML =
    `<div class="card-row">` +
      `<span class="card-title">${esc(card.title)}</span>` +
      `<span class="card-time">${fmtTime(card.created_at)}</span>` +
    `</div>` +
    col +
    (meta ? `<div class="card-col">${meta}</div>` : '') +
    `<div class="card-desc">${hlNums(card.description)}</div>`;

  return el;
}

function bump() {
  total++;
  count.textContent = total + (total === 1 ? ' event' : ' events');
}

function prependCard(card) {
  document.querySelector('.empty')?.remove();
  feed.insertBefore(makeCard(card), feed.firstChild);
  bump();
  while (feed.children.length > 100) feed.removeChild(feed.lastChild);
}

function appendCard(card) {
  document.querySelector('.empty')?.remove();
  feed.appendChild(makeCard(card));
  bump();
}

// ── boot ──────────────────────────────────────────────────────────────────

fetch('/cards')
  .then(r => r.json())
  .then(cards => { cards.forEach(appendCard); })   // already oldest-first
  .catch(console.error)
  .finally(connectSSE);

function connectSSE() {
  const es = new EventSource('/stream');

  es.onopen = () => { dot.className = 'on'; };

  es.onmessage = e => {
    try {
      const card = JSON.parse(e.data);
      if (card.error) { console.warn('server error:', card.error); return; }
      prependCard(card);
    } catch (err) { console.error(err); }
  };

  es.onerror = () => { dot.className = 'err'; };
  // EventSource reconnects automatically on error
}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML
