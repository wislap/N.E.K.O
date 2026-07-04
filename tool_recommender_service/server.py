from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import logging
import os
import sys
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

_repo_root = Path(__file__).resolve().parents[1]
if sys.path[0:1] != [str(_repo_root)]:
    sys.path.insert(0, str(_repo_root))

from utils.logger_config import setup_logging  # noqa: E402

from tool_recommender_service.contracts import ReloadRequest, ToolDecisionRequest, ToolFeedbackRequest  # noqa: E402
from tool_recommender_service.service import ToolRecommendationService  # noqa: E402
from tool_recommender_service.stub_backend import StubToolRecommenderBackend  # noqa: E402

logger, _log_config = setup_logging(service_name="ToolRecommender", log_level=logging.INFO)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 48919


def create_app(service: ToolRecommendationService | None = None) -> FastAPI:
    recommender = service or ToolRecommendationService(StubToolRecommenderBackend(), logger=logger)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.recommender = recommender
        await app.state.recommender.start()
        try:
            yield
        finally:
            await app.state.recommender.stop()

    app = FastAPI(
        title="N.E.K.O Tool Recommender Service",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> JSONResponse:
        return _json(app.state.recommender.health())

    @app.get("/v1/status")
    async def status() -> JSONResponse:
        return _json(app.state.recommender.health())

    @app.post("/v1/recommend")
    async def recommend(payload: ToolDecisionRequest) -> JSONResponse:
        result = await app.state.recommender.recommend(payload)
        return _json(result)

    @app.post("/v1/feedback")
    async def feedback(payload: ToolFeedbackRequest) -> JSONResponse:
        result = await app.state.recommender.record_feedback(payload)
        return _json(result)

    @app.post("/v1/reload")
    async def reload_backend(payload: ReloadRequest) -> JSONResponse:
        result = await app.state.recommender.reload(payload)
        return _json(result)

    return app


def _json(model: Any) -> JSONResponse:
    if hasattr(model, "model_dump"):
        return JSONResponse(model.model_dump(by_alias=True, exclude_none=True))
    return JSONResponse(model)


app = create_app()


def _read_port() -> int:
    raw = (
        os.getenv("TOOL_RECOMMENDER_SERVER_PORT")
        or os.getenv("NEKO_TOOL_RECOMMENDER_SERVER_PORT")
        or ""
    ).strip()
    if raw:
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            logger.warning("Invalid TOOL_RECOMMENDER_SERVER_PORT=%r; using default", raw)
    return DEFAULT_PORT


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the N.E.K.O Tool Recommender Service")
    parser.add_argument("--host", default=os.getenv("TOOL_RECOMMENDER_SERVER_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=_read_port())
    parser.add_argument("--log-level", default=os.getenv("NEKO_LOG_LEVEL", "info"))
    args = parser.parse_args(argv)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=str(args.log_level).lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
