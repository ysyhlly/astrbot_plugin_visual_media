"""Focused tests for Visual Media shared ProviderBudget, timezone, provider fallback, exclude_groups & quiet_hours."""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

if getattr(sys.modules.get("astrbot"), "__spec__", None) is None:
    api_mod = sys.modules.get("astrbot.api")
    if api_mod is not None and not hasattr(api_mod, "__path__"):
        api_mod.__path__ = []
    if "astrbot.api.event" not in sys.modules:
        event_mod = types.ModuleType("astrbot.api.event")
        event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
        sys.modules["astrbot.api.event"] = event_mod
    if "astrbot.api.web" not in sys.modules:
        web_mod = types.ModuleType("astrbot.api.web")
        web_mod.request = SimpleNamespace(_request=None)
        web_mod.json_response = lambda data, status_code=200: (status_code, data)
        sys.modules["astrbot.api.web"] = web_mod

import series_panels  # noqa: E402
from core.processing.natural_emotion_analyzer import NaturalEmotionAnalyzer  # noqa: E402
from core.processing.vlm_call_service import VLMCallService  # noqa: E402


class FakeSharedBudget:
    supports_series_telemetry = True

    def __init__(self, *, busy: bool = False):
        self.busy = busy
        self.calls: list[dict] = []

    async def run(
        self,
        provider: str,
        purpose: str,
        invoke,
        *,
        plugin_id: str = "",
        acquire_timeout: float | None = None,
    ):
        self.calls.append(
            {
                "provider": provider,
                "purpose": purpose,
                "plugin_id": plugin_id,
                "acquire_timeout": acquire_timeout,
            }
        )
        if self.busy:
            err = RuntimeError("queue busy")
            err.is_provider_budget_busy = True
            err.provider = provider
            err.purpose = purpose
            err.reason = "queue_full"
            raise err
        val = invoke()
        return await val if asyncio.iscoroutine(val) else val


def _chat_context(
    *,
    reply_provider: str = "series-reply",
    rhythm_timezone: str = "America/New_York",
    exclude_groups: list[str] | None = None,
    in_quiet_hours: bool = False,
    budget=None,
    llm_resp=None,
):
    class ChatOwner:
        def get_series_shared_config(self):
            return {
                "reply_provider": reply_provider,
                "rhythm_timezone": rhythm_timezone,
                "exclude_groups": exclude_groups or [],
                "quiet_hours": {
                    "daily_rhythm_enabled": True,
                    "in_quiet_hours": in_quiet_hours,
                },
            }

    ctx = SimpleNamespace(
        get_all_stars=lambda: {
            "chat": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics",
                activated=True,
                star_cls=ChatOwner(),
            )
        },
        get_current_chat_provider_id=AsyncMock(return_value="session-default"),
        llm_generate=AsyncMock(
            return_value=llm_resp
            or SimpleNamespace(completion_text='{"query":"开心","emotions":["happy"]}')
        ),
    )
    if budget is not None:
        ctx._chat_dynamics_provider_budget = budget
    return ctx


@pytest.mark.asyncio
async def test_visual_media_vlm_and_emotion_budget_delegation_and_busy(tmp_path):
    budget = FakeSharedBudget(busy=False)
    ctx = _chat_context(
        budget=budget,
        llm_resp=SimpleNamespace(completion_text="一只开心的小猫"),
    )

    # 1. VLMCallService delegates through shared ProviderBudget
    plugin_stub = SimpleNamespace(
        context=ctx,
        plugin_config=SimpleNamespace(
            vision_provider_id="vision-p",
            model_request_interval=0.0,
        ),
        vision_max_retries=3,
        vision_retry_delay=0.0,
    )
    vlm = VLMCallService(plugin_stub)
    out = await vlm._do_vlm_call("vision-p", "描述图片", str(tmp_path / "x.png"))
    assert out == "一只开心的小猫"
    assert budget.calls[-1]["plugin_id"] == "astrbot_plugin_visual_media"
    assert budget.calls[-1]["provider"] == "vision-p"
    assert budget.calls[-1]["purpose"] == "visual_vlm_analysis"

    # Busy VLM raises ProviderBudgetBusyError immediately without retrying 3 times
    busy = FakeSharedBudget(busy=True)
    ctx._chat_dynamics_provider_budget = busy
    with pytest.raises(series_panels.ProviderBudgetBusyError):
        await vlm._do_vlm_call("vision-p", "描述图片", str(tmp_path / "x.png"))
    assert len(busy.calls) == 1

    # 2. NaturalEmotionAnalyzer degrades cleanly (returns None) on busy budget
    analyzer = NaturalEmotionAnalyzer.__new__(NaturalEmotionAnalyzer)
    analyzer.plugin = SimpleNamespace(
        plugin_config=SimpleNamespace(emotion_analysis_provider_id=""),
        context=ctx,
    )
    analyzer._emotion_analysis_template = "{emotion_list}{llm_reply}{user_message}"
    analyzer._build_emotion_list_text = lambda: "happy, sad"
    ev = SimpleNamespace(unified_msg_origin="aiocqhttp:GroupMessage:1001")
    res = await analyzer._analyze_with_llm(ev, "今天好开心", user_message="哈哈")
    assert res is None
    assert busy.calls[-1]["provider"] == "series-reply"
    assert busy.calls[-1]["purpose"] == "visual_emotion_analysis"


@pytest.mark.asyncio
async def test_visual_media_standalone_fallback_and_provider_priority(tmp_path):
    ctx = SimpleNamespace(
        get_all_stars=lambda: {},
        get_current_chat_provider_id=AsyncMock(return_value="host-session-p"),
        llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text="ok")),
    )
    analyzer = NaturalEmotionAnalyzer.__new__(NaturalEmotionAnalyzer)
    analyzer.plugin = SimpleNamespace(
        plugin_config=SimpleNamespace(emotion_analysis_provider_id=""),
        context=ctx,
    )
    ev = SimpleNamespace(unified_msg_origin="aiocqhttp:GroupMessage:1001")
    assert await analyzer._get_text_provider(ev) == "host-session-p"

    # Explicit config overrides shared reply_provider
    shared_ctx = _chat_context(reply_provider="series-reply")
    analyzer.plugin = SimpleNamespace(
        plugin_config=SimpleNamespace(emotion_analysis_provider_id="explicit-emo"),
        context=shared_ctx,
    )
    assert await analyzer._get_text_provider(ev) == "explicit-emo"


def test_visual_media_series_helpers_timezone_exclude_and_quiet():
    excluded_umo = "aiocqhttp:GroupMessage:5555"
    allowed_umo = "aiocqhttp:GroupMessage:6666"
    ctx = _chat_context(
        rhythm_timezone="America/New_York",
        exclude_groups=[excluded_umo],
        in_quiet_hours=True,
    )

    tz = series_panels.resolve_series_timezone(ctx)
    assert str(tz) == "America/New_York"
    utc_dt = datetime(2026, 6, 2, 2, 0, tzinfo=timezone.utc)
    assert utc_dt.astimezone(tz).strftime("%Y-%m-%d") == "2026-06-01"

    assert series_panels.is_series_group_excluded(ctx, umo=excluded_umo) is True
    assert series_panels.is_series_group_excluded(ctx, group_id="5555") is True
    assert series_panels.is_series_group_excluded(ctx, umo=allowed_umo) is False
    assert series_panels.is_series_in_quiet_hours(ctx) is True

    # Standalone context (no chat_dynamics)
    standalone_ctx = SimpleNamespace(get_all_stars=lambda: {})
    assert str(series_panels.resolve_series_timezone(standalone_ctx)) == "Asia/Shanghai"
    assert series_panels.is_series_group_excluded(standalone_ctx, umo=excluded_umo) is False
    assert series_panels.is_series_in_quiet_hours(standalone_ctx) is False
