# Project File Directory — Stage 6.5

## Production entry points

| File | Purpose |
|---|---|
| `docker-compose.yml` | Starts the final `api + postgres + frontend` stack. |
| `Dockerfile.compose` | Builds the FastAPI image. |
| `frontend/Dockerfile` | Builds React and serves it through Nginx. |
| `frontend/nginx.conf` | SPA fallback and same-origin `/api/` proxy with SSE buffering disabled. |
| `run_agent_api.py` | FastAPI process entry point. |
| `scripts/docker/start_compose.ps1` | Production build/start command. |
| `scripts/docker/test_stage6_5.ps1` | Final Stage 6.5 regression and Chrome acceptance. |

## Main backend areas

| Directory | Purpose |
|---|---|
| `server/api/` | HTTP routes, request/response schemas and presenters. |
| `server/task_runtime/` | Task state machine, persistence, worker, retry/cancel and SSE. |
| `application/` | Application orchestration and service boundaries. |
| `application/support/` | File loading, backtest display and model-search helpers migrated out of the retired UI. |
| `agent/` | Main/Worker Agent, DAG, context, tools, Reflection, Handoff and WriteGateway. |
| `database/` | PostgreSQL connection, migrations, repositories and runtime data access. |
| `portfolio/` | Simulated account, positions, orders and cash flows. |
| `rag/` | Hybrid retrieval and retention. |

## Frontend

| Directory | Purpose |
|---|---|
| `frontend/src/pages/` | Dashboard, stock, model, backtest, news, paper trading, Agent and monitor pages. |
| `frontend/src/api/` | Typed HTTP, Task API and SSE clients. |
| `frontend/src/components/` | Page components and protected write controls. |
| `frontend/src/stores/` | UI state and recovery identifiers only. |

## Removed legacy files

`app.py`, `app/**`, `client/api/**`, Streamlit requirements, preview Compose overlay and Streamlit browser tests were deleted in Stage 6.5. They must not be restored as compatibility shortcuts.

## Local runtime data

The following are host-owned and are not source artifacts:

```text
data/
models/
outputs/
logs/
runtime/
external_repos/
local_app_config.json
```
