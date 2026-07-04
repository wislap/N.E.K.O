from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class RecentMessage(ContractModel):
    role: str = Field(min_length=1)
    text: str = ""


class PluginEntrySnapshot(ContractModel):
    entry_id: str = Field(alias="entryId", min_length=1)
    name: str = ""
    description: str = ""


class PluginSnapshotItem(ContractModel):
    tool_id: str = Field(alias="toolId", min_length=1)
    name: str = Field(min_length=1)
    enabled: bool = False
    capability_summary: str = Field(default="", alias="capabilitySummary")
    risk_level: str | None = Field(default=None, alias="riskLevel")
    kind: Literal["agent_feature", "plugin", "plugin_group"] = "plugin"
    status: str = ""
    entries: list[PluginEntrySnapshot] = Field(default_factory=list)


class PreferenceSnapshot(ContractModel):
    data: dict[str, Any] = Field(default_factory=dict)


class ToolDecisionRequest(ContractModel):
    request_id: str = Field(alias="requestId", min_length=1)
    session_id: str = Field(alias="sessionId", min_length=1)
    turn_id: str | None = Field(default=None, alias="turnId")
    trigger: str = "unknown"
    recent_messages: list[RecentMessage] = Field(default_factory=list, alias="recentMessages")
    plugin_snapshot: list[PluginSnapshotItem] = Field(default_factory=list, alias="pluginSnapshot")
    preference_snapshot: dict[str, Any] | None = Field(default=None, alias="preferenceSnapshot")
    policy_mode: Literal["suggest_only", "auto_apply_candidate"] = Field(
        default="suggest_only",
        alias="policyMode",
    )


class RecommendedAction(ContractModel):
    tool_id: str = Field(alias="toolId", min_length=1)
    tool_name: str = Field(alias="toolName", min_length=1)
    direction: Literal["enable", "disable"]
    source: str = "Stage2"
    score: float | None = None
    confidence: float | None = None


class ToolDecisionResult(ContractModel):
    request_id: str = Field(alias="requestId", min_length=1)
    inference_id: str = Field(alias="inferenceId", min_length=1)
    service_version: str = Field(alias="serviceVersion", min_length=1)
    model_version: str = Field(alias="modelVersion", min_length=1)
    policy_version: str = Field(alias="policyVersion", min_length=1)
    slate_id: str = Field(alias="slateId", min_length=1)
    summary: str = Field(min_length=1)
    actions: list[RecommendedAction] = Field(default_factory=list)
    expires_at: float | None = Field(default=None, alias="expiresAt")
    debug: dict[str, Any] | None = None


class FeedbackAction(ContractModel):
    tool_id: str = Field(alias="toolId", min_length=1)
    direction: Literal["enable", "disable"]
    value: Literal["positive", "negative"]


class ToolFeedbackRequest(ContractModel):
    request_id: str = Field(alias="requestId", min_length=1)
    inference_id: str | None = Field(default=None, alias="inferenceId")
    slate_id: str = Field(alias="slateId", min_length=1)
    session_id: str | None = Field(default=None, alias="sessionId")
    turn_id: str | None = Field(default=None, alias="turnId")
    value: Literal["up", "down"] | None = None
    actions: list[FeedbackAction] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolFeedbackResult(ContractModel):
    ok: bool
    request_id: str = Field(alias="requestId")
    accepted: bool = True
    service_version: str = Field(alias="serviceVersion")


class ReloadRequest(ContractModel):
    model_version: str | None = Field(default=None, alias="modelVersion")
    policy_version: str | None = Field(default=None, alias="policyVersion")
    dry_run: bool = Field(default=False, alias="dryRun")


class ReloadResult(ContractModel):
    ok: bool
    reloaded: bool
    service_version: str = Field(alias="serviceVersion")
    model_version: str = Field(alias="modelVersion")
    policy_version: str = Field(alias="policyVersion")
    message: str = ""


class HealthResult(ContractModel):
    status: Literal["ok"]
    service: str
    service_version: str = Field(alias="serviceVersion")
    backend: str
    model_version: str = Field(alias="modelVersion")
    policy_version: str = Field(alias="policyVersion")
    loaded: bool
