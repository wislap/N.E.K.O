from __future__ import annotations

import pytest

from main_routers import tool_recommendation_router as module


pytestmark = pytest.mark.unit


class _FakeSessionManager:
    def __init__(self) -> None:
        self.agent_flags = {
            "agent_enabled": True,
            "computer_use_enabled": False,
            "browser_use_enabled": True,
            "user_plugin_enabled": True,
            "openclaw_enabled": True,
            "openclaw_ready": True,
            "openfang_enabled": True,
        }
        self.updates: list[dict[str, object]] = []

    def update_agent_flags(self, flags: dict[str, object]) -> None:
        self.updates.append(dict(flags))
        self.agent_flags.update(flags)


def _action(tool_id: str, direction: str, name: str | None = None) -> module.RecommendationApplyAction:
    return module.RecommendationApplyAction(
        toolId=tool_id,
        toolName=name or tool_id,
        direction=direction,
    )


def test_sort_actions_enables_master_and_plugin_group_before_children() -> None:
    actions = [
        _action("plugin:minecraft", "enable"),
        _action("computer_use_enabled", "enable"),
        _action("agent_master", "enable"),
        _action("user_plugin_enabled", "enable"),
        _action("openfang_enabled", "disable"),
        _action("agent_master", "disable"),
    ]

    sorted_ids = [(item.toolId, item.direction) for item in module._sort_actions(actions)]

    assert sorted_ids == [
        ("agent_master", "enable"),
        ("user_plugin_enabled", "enable"),
        ("computer_use_enabled", "enable"),
        ("plugin:minecraft", "enable"),
        ("openfang_enabled", "disable"),
        ("agent_master", "disable"),
    ]


@pytest.mark.asyncio
async def test_apply_tool_recommendation_deduplicates_and_reports_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    async def fake_apply_one(
        action: module.RecommendationApplyAction,
        *,
        lanlan_name: str,
    ) -> dict[str, object]:
        calls.append((lanlan_name, action.toolId, action.direction))
        return module._result(action, success=True, status="applied")

    monkeypatch.setattr(module, "_resolve_lanlan_name", lambda requested: requested or "Lan")
    monkeypatch.setattr(module, "_apply_one", fake_apply_one)

    response = await module.apply_tool_recommendation(
        module.RecommendationApplyRequest(
            lanlanName="Mika",
            requestId="req",
            recommendationId="rec",
            slateId="slate",
            actions=[
                _action("computer_use_enabled", "enable"),
                _action("computer_use_enabled", "enable"),
                _action("plugin:demo", "disable"),
            ],
        )
    )

    assert response["success"] is True
    assert response["status"] == "applied"
    assert response["lanlanName"] == "Mika"
    assert calls == [
        ("Mika", "computer_use_enabled", "enable"),
        ("Mika", "plugin:demo", "disable"),
    ]
    assert response["results"][1]["status"] == "skipped"
    assert response["results"][1]["detail"] == {"reason": "duplicate"}


@pytest.mark.asyncio
async def test_apply_tool_recommendation_reports_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_apply_one(
        action: module.RecommendationApplyAction,
        *,
        lanlan_name: str,
    ) -> dict[str, object]:
        if action.toolId == "plugin:bad":
            return module._result(action, success=False, status="failed", error="boom")
        return module._result(action, success=True, status="applied")

    monkeypatch.setattr(module, "_resolve_lanlan_name", lambda requested: "Lan")
    monkeypatch.setattr(module, "_apply_one", fake_apply_one)

    response = await module.apply_tool_recommendation(
        module.RecommendationApplyRequest(
            actions=[
                _action("agent_master", "enable"),
                _action("plugin:bad", "enable"),
            ],
        )
    )

    assert response["success"] is False
    assert response["status"] == "partial"
    assert [item["success"] for item in response["results"]] == [True, False]


@pytest.mark.asyncio
async def test_apply_agent_master_disable_resets_session_flags_and_posts_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeSessionManager()
    posted: list[tuple[str, dict[str, object]]] = []

    async def fake_post_json(url: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        posted.append((url, dict(payload)))
        return {"success": True, "request_id": "upstream-1"}

    monkeypatch.setattr(module, "get_session_manager", lambda: {"Lan": manager})
    monkeypatch.setattr(module, "_post_json", fake_post_json)

    result = await module._apply_agent_action(_action("agent_master", "disable"), lanlan_name="Lan")

    assert result["success"] is True
    assert result["status"] == "applied"
    assert manager.updates == [{
        "agent_enabled": False,
        "computer_use_enabled": False,
        "browser_use_enabled": False,
        "user_plugin_enabled": False,
        "openclaw_enabled": False,
        "openclaw_ready": False,
        "openfang_enabled": False,
    }]
    assert posted[0][0].endswith("/agent/command")
    assert posted[0][1]["command"] == "set_agent_enabled"
    assert posted[0][1]["enabled"] is False
    assert posted[0][1]["lanlan_name"] == "Lan"


@pytest.mark.asyncio
async def test_apply_agent_flag_rolls_back_session_flags_on_upstream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeSessionManager()
    original_flags = dict(manager.agent_flags)

    async def fake_post_json(url: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        raise RuntimeError("tool server down")

    monkeypatch.setattr(module, "get_session_manager", lambda: {"Lan": manager})
    monkeypatch.setattr(module, "_post_json", fake_post_json)

    result = await module._apply_agent_action(_action("browser_use_enabled", "disable"), lanlan_name="Lan")

    assert result["success"] is False
    assert result["status"] == "failed"
    assert "tool server down" in result["error"]
    assert manager.agent_flags == original_flags


@pytest.mark.asyncio
async def test_apply_agent_rejects_unknown_agent_tool_id() -> None:
    result = await module._apply_agent_action(_action("agent_unknown", "enable"), lanlan_name="Lan")

    assert result["success"] is False
    assert result["status"] == "unsupported"


@pytest.mark.asyncio
async def test_apply_plugin_action_uses_plain_plugin_enable_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[str] = []

    async def fake_post_json(url: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        posted.append(url)
        return {"success": True, "plugin_id": "demo", "message": "ok"}

    async def fake_get_plugin_type(plugin_id: str) -> str:
        return ""

    monkeypatch.setattr(module, "_user_plugin_base_url", lambda: "http://plugin-server")
    monkeypatch.setattr(module, "_get_plugin_type", fake_get_plugin_type)
    monkeypatch.setattr(module, "_post_json", fake_post_json)

    enable = await module._apply_plugin_action(_action("plugin:demo", "enable"))
    disable = await module._apply_plugin_action(_action("plugin:demo", "disable"))

    assert enable["success"] is True
    assert disable["success"] is True
    assert posted == [
        "http://plugin-server/plugin/demo/enable",
        "http://plugin-server/plugin/demo/disable",
    ]


@pytest.mark.asyncio
async def test_apply_plugin_action_uses_extension_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[str] = []

    async def fake_post_json(url: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        posted.append(url)
        return {"success": True, "ext_id": "ext"}

    async def fake_get_plugin_type(plugin_id: str) -> str:
        return "extension"

    monkeypatch.setattr(module, "_user_plugin_base_url", lambda: "http://plugin-server")
    monkeypatch.setattr(module, "_get_plugin_type", fake_get_plugin_type)
    monkeypatch.setattr(module, "_post_json", fake_post_json)

    await module._apply_plugin_action(_action("plugin:ext", "enable"))
    await module._apply_plugin_action(_action("plugin:ext", "disable"))

    assert posted == [
        "http://plugin-server/plugin/ext/extension/enable",
        "http://plugin-server/plugin/ext/extension/disable",
    ]


@pytest.mark.asyncio
async def test_apply_plugin_action_reports_invalid_plugin_id() -> None:
    result = await module._apply_plugin_action(_action("plugin:", "enable"))

    assert result["success"] is False
    assert result["status"] == "invalid"


@pytest.mark.asyncio
async def test_apply_one_reports_unsupported_tool_id() -> None:
    result = await module._apply_one(_action("unknown_tool", "enable"), lanlan_name="Lan")

    assert result["success"] is False
    assert result["status"] == "unsupported"
