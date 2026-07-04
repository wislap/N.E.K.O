from __future__ import annotations

from abc import ABC, abstractmethod

from .contracts import (
    ReloadRequest,
    ReloadResult,
    ToolDecisionRequest,
    ToolDecisionResult,
    ToolFeedbackRequest,
    ToolFeedbackResult,
)


class ToolRecommenderBackend(ABC):
    """Model-side backend interface for recommendation inference.

    Real Stage1+Stage2 implementations should live behind this boundary. The
    HTTP server and agent integration should not import model internals.
    """

    name: str
    model_version: str
    policy_version: str

    @abstractmethod
    async def load(self) -> None:
        """Load model/runtime resources."""

    @abstractmethod
    async def recommend(self, request: ToolDecisionRequest) -> ToolDecisionResult:
        """Return a slate recommendation for the request."""

    @abstractmethod
    async def record_feedback(self, request: ToolFeedbackRequest) -> ToolFeedbackResult:
        """Record user feedback for a recommendation."""

    @abstractmethod
    async def reload(self, request: ReloadRequest) -> ReloadResult:
        """Reload model/policy parameters."""

    @abstractmethod
    async def close(self) -> None:
        """Release backend resources."""
