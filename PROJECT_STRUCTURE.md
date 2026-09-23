# Project Structure — Stage 6.5

## Runtime

```text
Browser -> React/Nginx:3000 -> FastAPI:8010 -> Application Service -> Runtime/Domain
```

## Source layout

```text
stock_daily_app/
├── frontend/                  # React production frontend and Nginx proxy
├── server/api/                # FastAPI routers, schemas and presenters
├── server/task_runtime/       # Task API, persistence, worker and SSE
├── application/               # Application services
│   └── support/               # UI-framework-independent shared helpers
├── agent/                     # Agent orchestration, Request/Need/Worker planning, Tool DAG and write gateway
├── portfolio/                 # Paper trading and cash-flow domain
├── pipelines/                 # Prediction/RAG/scoring/paper pipelines
├── rag/                       # BM25, dense, hybrid retrieval and reranking
├── database/                  # PostgreSQL migrations and repositories
├── contracts/stage6/          # Frozen Stage 6 HTTP/Task/transport contracts
├── scripts/docker/            # Production Compose startup and acceptance
├── scripts/refactor/          # Contract and architecture checks
├── tests/                     # Backend, API and React contract tests
├── docker-compose.yml         # Final api + postgres + frontend production stack
├── Dockerfile.compose         # FastAPI image
└── requirements-compose-api.txt
```

## Removed at Stage 6.5

```text
app.py
app/**
client/api/**
requirements-compose-streamlit.txt
docker-compose.react-preview.yml
Streamlit integration/e2e tests
```

Git history preserves the removed implementation. New code must not recreate a second frontend or Python browser client.

## Commands

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_stage6_2_read_only_web.py tests\unit\test_stage6_3_paper_trading_web.py tests\unit\test_stage6_4_agent_web.py tests\unit\test_stage6_5_cutover.py -q
powershell -ExecutionPolicy Bypass -File .\scripts\docker\start_compose.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\docker\test_stage6_5.ps1
```
