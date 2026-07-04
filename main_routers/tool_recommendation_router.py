# -*- coding: utf-8 -*-
"""Tool recommendation application router.

This router is the main-server side orchestration point for interactive
recommendation bubbles. The frontend confirms a slate once; main_server then
fans out to agent_server and user_plugin_server with bounded, supervised calls.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Literal

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from config import TOOL_SERVER_PORT, USER_PLUGIN_BASE, USER_PLUGIN_SERVER_PORT
from main_routers.cookies_login_router import verify_local_access
from main_routers.shared_state import get_config_manager, get_session_manager
from utils.logger_config import get_module_logger


router = APIRouter(
    prefix="/api/tool-recommendations",
    tags=["tool-recommendations"],
    dependencies=[Depends(verify_local_access)],
)
logger = get_module_logger(__name__, "Main")

TOOL_SERVER_BASE = f"http://127.0.0.1:{TOOL_SERVER_PORT}"
AGENT_FLAG_KEYS = {
    "computer_use_enabled",
    "browser_use_enabled",
    "user_plugin_enabled",
    "openclaw_enabled",
    "openfang_enabled",
}


class RecommendationApplyAction(BaseModel):
    toolId: str = Field(..., min_length=1, max_length=160)
    toolName: str | None = Field(default=None, max_length=160)
    direction: Literal["enable", "disable"]


class RecommendationApplyRequest(BaseModel):
    requestId: str | None = Field(default=None, max_length=160)
    recommendationId: str | None = Field(default=None, max_length=160)
    slateId: str | None = Field(default=None, max_length=160)
    turnId: str | None = Field(default=None, max_length=160)
    lanlanName: str | None = Field(default=None, max_length=160)
    actions: list[RecommendationApplyAction] = Field(default_factory=list, max_length=12)


def _user_plugin_base_url() -> str:
    explicit_port = os.getenv("NEKO_USER_PLUGIN_SERVER_PORT", "").strip()
    if explicit_port:
        try:
            port = int(explicit_port)
            if 0 < port <= 65535:
                return f"http://127.0.0.1:{port}"
        except ValueError:
            logger.warning("Invalid NEKO_USER_PLUGIN_SERVER_PORT value %r", explicit_port)
    configured = str(USER_PLUGIN_BASE or "").strip().rstrip("/")
    if configured:
        return configured
    return f"http://127.0.0.1:{USER_PLUGIN_SERVER_PORT}"


def _resolve_lanlan_name(requested: str | None) -> str:
    if requested:
        return requested.strip()
    try:
        _, her_name_current, _, _, _, _, _, _, _ = get_config_manager().get_character_data()
        return str(her_name_current or "").strip()
    except Exception:
        return ""


def _sort_actions(actions: list[RecommendationApplyAction]) -> list[RecommendationApplyAction]:
    def _rank(action: RecommendationApplyAction) -> int:
        enable = action.direction == "enable"
        tool_id = action.toolId
        if tool_id == "agent_master":
            return 0 if enable else 90
        if tool_id == "user_plugin_enabled":
            return 10 if enable else 80
        if tool_id in AGENT_FLAG_KEYS:
            return 20 if enable else 70
        if tool_id.startswith("plugin:"):
            return 30 if enable else 60
        return 50

    return sorted(actions, key=_rank)


def _result(
    action: RecommendationApplyAction,
    *,
    success: bool,
    status: str,
    error: str | None = None,
    detail: object | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "toolId": action.toolId,
        "toolName": action.toolName or action.toolId,
        "direction": action.direction,
        "success": success,
        "status": status,
    }
    if error:
        payload["error"] = error
    if detail is not None:
        payload["detail"] = detail
    return payload


async def _post_json(url: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 1.0)), proxy=None, trust_env=False) as client:
        response = await client.post(url, json=payload)
    if not response.is_success:
        raise RuntimeError(f"upstream responded {response.status_code}: {response.text[:240]}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("upstream returned non-object response")
    if data.get("success") is False:
        raise RuntimeError(str(data.get("error") or data.get("message") or "upstream rejected request"))
    return data


async def _apply_agent_action(action: RecommendationApplyAction, *, lanlan_name: str) -> dict[str, object]:
    enabled = action.direction == "enable"
    tool_id = action.toolId
    if tool_id == "agent_master":
        command_payload: dict[str, object] = {
            "request_id": f"tool-rec-apply-{uuid.uuid4().hex[:8]}",
            "command": "set_agent_enabled",
            "enabled": enabled,
            "lanlan_name": lanlan_name,
        }
    elif tool_id in AGENT_FLAG_KEYS:
        command_payload = {
            "request_id": f"tool-rec-apply-{uuid.uuid4().hex[:8]}",
            "command": "set_flag",
            "key": tool_id,
            "value": enabled,
            "lanlan_name": lanlan_name,
        }
    else:
        return _result(action, success=False, status="unsupported", error="unsupported agent tool id")

    mgr = get_session_manager().get(lanlan_name) if lanlan_name else None
    old_flags = dict(getattr(mgr, "agent_flags", {}) or {}) if mgr else None
    if mgr:
        if tool_id == "agent_master":
            if enabled:
                mgr.update_agent_flags({"agent_enabled": True})
            else:
                mgr.update_agent_flags({
                    "agent_enabled": False,
                    "computer_use_enabled": False,
                    "browser_use_enabled": False,
                    "user_plugin_enabled": False,
                    "openclaw_enabled": False,
                    "openclaw_ready": False,
                    "openfang_enabled": False,
                })
        else:
            update = {tool_id: enabled}
            if tool_id == "openclaw_enabled":
                update["openclaw_ready"] = False
            mgr.update_agent_flags(update)

    try:
        data = await _post_json(f"{TOOL_SERVER_BASE}/agent/command", command_payload, timeout=8.0)
        return _result(action, success=True, status="applied", detail={"request_id": data.get("request_id")})
    except Exception as exc:
        if mgr and old_flags is not None:
            mgr.update_agent_flags(old_flags)
        return _result(action, success=False, status="failed", error=str(exc))


async def _get_plugin_type(plugin_id: str) -> str:
    base = _user_plugin_base_url()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(1.5, connect=0.5), proxy=None, trust_env=False) as client:
            response = await client.get(f"{base}/plugins")
        if not response.is_success:
            return ""
        data = response.json()
        plugins = data.get("plugins") if isinstance(data, dict) else None
        if not isinstance(plugins, list):
            return ""
        for item in plugins:
            if isinstance(item, dict) and str(item.get("id") or item.get("plugin_id") or "") == plugin_id:
                return str(item.get("type") or "").strip()
    except Exception:
        return ""
    return ""


async def _apply_plugin_action(action: RecommendationApplyAction) -> dict[str, object]:
    plugin_id = action.toolId.split(":", 1)[1].strip() if action.toolId.startswith("plugin:") else ""
    if not plugin_id:
        return _result(action, success=False, status="invalid", error="missing plugin id")

    plugin_type = await _get_plugin_type(plugin_id)
    base = _user_plugin_base_url()
    if plugin_type == "extension":
        suffix = "extension/enable" if action.direction == "enable" else "extension/disable"
        url = f"{base}/plugin/{plugin_id}/{suffix}"
    else:
        verb = "enable" if action.direction == "enable" else "disable"
        url = f"{base}/plugin/{plugin_id}/{verb}"

    try:
        data = await _post_json(url, {}, timeout=15.0)
        return _result(action, success=True, status="applied", detail={
            "plugin_id": data.get("plugin_id") or data.get("ext_id") or plugin_id,
            "message": data.get("message"),
        })
    except Exception as exc:
        return _result(action, success=False, status="failed", error=str(exc))


async def _apply_one(action: RecommendationApplyAction, *, lanlan_name: str) -> dict[str, object]:
    if action.toolId == "agent_master" or action.toolId in AGENT_FLAG_KEYS:
        return await _apply_agent_action(action, lanlan_name=lanlan_name)
    if action.toolId.startswith("plugin:"):
        return await _apply_plugin_action(action)
    return _result(action, success=False, status="unsupported", error="unsupported tool id")


@router.post("/apply")
async def apply_tool_recommendation(request: RecommendationApplyRequest) -> dict[str, object]:
    started = time.perf_counter()
    lanlan_name = _resolve_lanlan_name(request.lanlanName)
    results: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()

    for action in _sort_actions(request.actions):
        key = (action.toolId, action.direction)
        if key in seen:
            results.append(_result(action, success=True, status="skipped", detail={"reason": "duplicate"}))
            continue
        seen.add(key)
        results.append(await _apply_one(action, lanlan_name=lanlan_name))

    success_count = sum(1 for item in results if item.get("success") is True)
    failed_count = sum(1 for item in results if item.get("success") is False)
    if failed_count == 0:
        status = "applied"
    elif success_count:
        status = "partial"
    else:
        status = "failed"

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.info(
        "[ToolRecommendationApply] recommendation=%s slate=%s status=%s success=%s failed=%s elapsed_ms=%s",
        request.recommendationId or request.requestId,
        request.slateId,
        status,
        success_count,
        failed_count,
        elapsed_ms,
    )
    return {
        "success": failed_count == 0,
        "status": status,
        "requestId": request.requestId,
        "recommendationId": request.recommendationId,
        "slateId": request.slateId,
        "turnId": request.turnId,
        "lanlanName": lanlan_name,
        "results": results,
        "elapsedMs": elapsed_ms,
    }
