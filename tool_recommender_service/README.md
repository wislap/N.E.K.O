# Tool Recommender Service

Standalone protocol shell for future Stage1+Stage2 tool recommendation infra.

## Boundary

This service owns recommendation inference contracts and model lifecycle. It
does not know about frontend chat windows, Electron IPC, plugin execution, or
agent task dispatch.

Current implementation:

- FastAPI server
- Stable request/response contracts
- Service/orchestrator layer
- Model backend abstraction
- Deterministic stub backend

Future implementation should replace only the backend behind
`ToolRecommenderBackend`.

## Endpoints

- `GET /health`
- `GET /v1/status`
- `POST /v1/recommend`
- `POST /v1/feedback`
- `POST /v1/reload`

Run locally with uv:

```bash
uv run tool_recommender_service/server.py --host 127.0.0.1 --port 48919
```

Or with the active Python environment:

```bash
python -m tool_recommender_service --host 127.0.0.1 --port 48919
```

The default port can be overridden with `TOOL_RECOMMENDER_SERVER_PORT`.
