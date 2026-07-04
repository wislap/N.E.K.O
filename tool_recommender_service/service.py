from __future__ import annotations

import logging
import time

from . import __version__
from .backend import ToolRecommenderBackend
from .contracts import (
    HealthResult,
    ReloadRequest,
    ReloadResult,
    ToolDecisionRequest,
    ToolDecisionResult,
    ToolFeedbackRequest,
    ToolFeedbackResult,
)


class ToolRecommendationService:
    """Service layer between HTTP routes and model backend."""

    def __init__(self, backend: ToolRecommenderBackend, logger: logging.Logger | None = None) -> None:
        self.backend = backend
        self.logger = logger or logging.getLogger(__name__)

    async def start(self) -> None:
        started = time.perf_counter()
        await self.backend.load()
        self.logger.info(
            "[ToolRecommender] backend loaded: backend=%s model=%s policy=%s latency_ms=%.1f",
            self.backend.name,
            self.backend.model_version,
            self.backend.policy_version,
            (time.perf_counter() - started) * 1000.0,
        )

    async def stop(self) -> None:
        await self.backend.close()

    async def recommend(self, request: ToolDecisionRequest) -> ToolDecisionResult:
        started = time.perf_counter()
        result = await self.backend.recommend(request)
        self.logger.info(
            "[ToolRecommender] recommend done: request_id=%s inference_id=%s actions=%d latency_ms=%.1f",
            request.request_id,
            result.inference_id,
            len(result.actions),
            (time.perf_counter() - started) * 1000.0,
        )
        return result

    async def record_feedback(self, request: ToolFeedbackRequest) -> ToolFeedbackResult:
        result = await self.backend.record_feedback(request)
        self.logger.info(
            "[ToolRecommender] feedback accepted: request_id=%s slate_id=%s value=%s action_feedback=%d",
            request.request_id,
            request.slate_id,
            request.value or "",
            len(request.actions),
        )
        return result

    async def reload(self, request: ReloadRequest) -> ReloadResult:
        result = await self.backend.reload(request)
        self.logger.info(
            "[ToolRecommender] reload: reloaded=%s model=%s policy=%s dry_run=%s",
            result.reloaded,
            result.model_version,
            result.policy_version,
            request.dry_run,
        )
        return result

    def health(self) -> HealthResult:
        return HealthResult(
            status="ok",
            service="tool_recommender",
            service_version=__version__,
            backend=self.backend.name,
            model_version=self.backend.model_version,
            policy_version=self.backend.policy_version,
            loaded=bool(getattr(self.backend, "loaded", False)),
        )
