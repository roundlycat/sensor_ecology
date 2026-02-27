# Sensor Ecology Dashboard

A lightweight FastAPI dashboard for exploring observations from the distributed
sensor ecology. Modular by design — add new pages, API endpoints, and
visualization widgets as the ecology grows.

---

## Directory layout

```
dashboard/
├── main.py                    # FastAPI app + page routes
├── requirements.txt
├── .env.example               # copy to .env and edit
├── app/
│   ├── config.py              # env-var loading
│   ├── api/
│   │   ├── agents.py          # GET /api/agents
│   │   ├── observations.py    # GET /api/observations/recent
│   │   ├── semantic.py        # POST /api/semantic/search
│   │   ├── stats.py           # GET /api/stats
│   │   └── live.py            # GET /live/feed  (SSE)
│   ├── db/
│   │   ├── connection.py      # asyncpg pool
│   │   └── queries.py         # all SQL — one place to extend
│   ├── models/
│   │   ├── agent.py           # Pydantic Agent model
│   │   └── observation.py     # Pydantic Observation + SearchRequest
│   └── services/
│       └── embeddings.py      # sentence-transformers lazy singleton
├── templates/
│   ├── base.html              # shared nav + layout
│   ├── index.html             # overview / stats
│   ├── agents.html            # agent roster
│   ├── observations.html      # filterable observation table
│   ├── semantic.html          # semantic search UI
│   └── live.html              # live SSE feed
└── static/
    ├── css/style.css
    └── js/app.js              # shared UI utilities
```

---

## Setup

### 1. Install dependencies

The dashboard needs `fastapi` and `asyncpg` in addition to the packages
already installed for the agents:

```bash
pip3 install fastapi asyncpg python-dotenv jinja2 --break-system-packages
```

`uvicorn` and `sentence-transformers` are already installed.

### 2. Create your `.env`

```bash
cd /home/sean/sensor_ecology/dashboard
cp .env.example .env
```

Edit `.env` if your Postgres password or port differs. The defaults assume
the local `sensor_ecology` database accessible as user `sean`.

### 3. Run

```bash
cd /home/sean/sensor_ecology/dashboard
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Access from any browser on your network:
```
http://<pi-ip>:8000
```

---

## Pages

| URL             | Description                                      |
|-----------------|--------------------------------------------------|
| `/`             | Overview — stats cards + recent observations     |
| `/agents`       | Agent roster with last-active and obs count      |
| `/observations` | Filterable table (agent type, obs type, since)   |
| `/semantic`     | Semantic search — text → embedding → similarity  |
| `/stream`       | Live SSE feed — new observations pushed every 3s |

---

## API endpoints

| Method | Path                          | Description                              |
|--------|-------------------------------|------------------------------------------|
| GET    | `/api/agents/`                | List all agents                          |
| GET    | `/api/agents/meta/types`      | Distinct agent types                     |
| GET    | `/api/agents/{id}`            | Single agent detail                      |
| GET    | `/api/observations/recent`    | Recent observations (filterable)         |
| GET    | `/api/observations/meta/types`| Distinct observation types               |
| POST   | `/api/semantic/search`        | Semantic similarity search               |
| GET    | `/api/stats/`                 | Dashboard summary stats                  |
| GET    | `/live/feed`                  | SSE stream of new observations           |

Interactive docs: `http://<pi-ip>:8000/docs`

---

## Adding a new page

1. Add a template to `templates/`.
2. Add a `@app.get(...)` route in `main.py`.
3. Optionally add a new API file in `app/api/` and include the router in
   `main.py`.

## Adding a new observation type

New observation types appear automatically in the filters and tables.
To add a custom badge colour, extend the `colors` map in `static/js/app.js`.

## Adding a new agent type

Nothing to change in the dashboard — agents self-register in Postgres and
appear in `/api/agents` as soon as they connect.

---

## Run as a systemd service

```ini
# /etc/systemd/system/ecology-dashboard.service
[Unit]
Description=Sensor Ecology Dashboard
After=network.target postgresql.service

[Service]
User=sean
WorkingDirectory=/home/sean/sensor_ecology/dashboard
ExecStart=/home/sean/.local/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now ecology-dashboard
```
