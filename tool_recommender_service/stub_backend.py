from __future__ import annotations

import asyncio
import hashlib
import random

from . import __version__
from .backend import ToolRecommenderBackend
from .contracts import (
    RecommendedAction,
    ReloadRequest,
    ReloadResult,
    ToolDecisionRequest,
    ToolDecisionResult,
    ToolFeedbackRequest,
    ToolFeedbackResult,
)


class StubToolRecommenderBackend(ToolRecommenderBackend):
    """Deterministic placeholder for the future Stage1+Stage2 backend."""

    name = "stub"

    def __init__(self) -> None:
        self.model_version = "stub-random-v1"
        self.policy_version = "stub-policy-v1"
        self.loaded = False
        self.feedback_count = 0

    async def load(self) -> None:
        await asyncio.sleep(0)
        self.loaded = True

    async def close(self) -> None:
        await asyncio.sleep(0)
        self.loaded = False

    async def reload(self, request: ReloadRequest) -> ReloadResult:
        if request.model_version:
            self.model_version = request.model_version
        if request.policy_version:
            self.policy_version = request.policy_version
        if not request.dry_run:
            self.loaded = True
        return ReloadResult(
            ok=True,
            reloaded=not request.dry_run,
            service_version=__version__,
            model_version=self.model_version,
            policy_version=self.policy_version,
            message="stub reload accepted",
        )

    async def record_feedback(self, request: ToolFeedbackRequest) -> ToolFeedbackResult:
        self.feedback_count += 1
        return ToolFeedbackResult(
            ok=True,
            request_id=request.request_id,
            accepted=True,
            service_version=__version__,
        )

    async def recommend(self, request: ToolDecisionRequest) -> ToolDecisionResult:
        seed = self._seed_for(request)
        rng = random.Random(seed)
        candidates = list(request.plugin_snapshot)
        rng.shuffle(candidates)

        max_actions = min(2, len(candidates))
        if max_actions <= 0:
            actions: list[RecommendedAction] = []
        else:
            action_count = rng.randint(1, max_actions)
            actions = [
                RecommendedAction(
                    tool_id=item.tool_id,
                    tool_name=item.name,
                    direction="disable" if item.enabled else "enable",
                    source="Stage2",
                    score=round(rng.uniform(0.42, 0.93), 3),
                    confidence=round(rng.uniform(0.52, 0.88), 3),
                )
                for item in candidates[:action_count]
            ]

        inference_id = "inf-" + self._short_hash(
            request.request_id,
            request.session_id,
            request.turn_id or "",
            self.model_version,
        )
        slate_id = "slate-" + self._short_hash(inference_id, self.policy_version)
        if actions:
            summary = f"建议调整 {len(actions)} 个工具开关"
        else:
            summary = "当前不建议调整工具开关"

        return ToolDecisionResult(
            request_id=request.request_id,
            inference_id=inference_id,
            service_version=__version__,
            model_version=self.model_version,
            policy_version=self.policy_version,
            slate_id=slate_id,
            summary=summary,
            actions=actions,
            expires_at=1_900_000_000.0 + float(seed % 10_000),
            debug={
                "backend": self.name,
                "seed": seed,
                "feedbackCount": self.feedback_count,
            },
        )

    @staticmethod
    def _seed_for(request: ToolDecisionRequest) -> int:
        raw = "|".join([
            request.request_id,
            request.session_id,
            request.turn_id or "",
            request.trigger,
        ])
        return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], 16)

    @staticmethod
    def _short_hash(*parts: str) -> str:
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
