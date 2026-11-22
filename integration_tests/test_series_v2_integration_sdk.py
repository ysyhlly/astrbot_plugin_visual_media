"""SDK integration tests for Visual Media shared ProviderBudget, timezone, exclude_groups & character_check."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_visual_media.character_check import check_character
from astrbot_plugin_visual_media.main import VisualMediaPlugin
from astrbot_plugin_visual_media.tl.vision_handler import VisionHandler


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
            err = RuntimeError("busy")
            err.is_provider_budget_busy = True
            err.provider = provider
            err.purpose = purpose
            err.reason = "acquire_timeout"
            raise err
        val = invoke()
        return await val if asyncio.iscoroutine(val) else val


def _chat_context(*, exclude_groups=None, rhythm_timezone="America/New_York", budget=None):
    class ChatOwner:
        def get_series_shared_config(self):
            return {
                "reply_provider": "series-reply",
                "rhythm_timezone": rhythm_timezone,
                "exclude_groups": exclude_groups or [],
                "quiet_hours": {"daily_rhythm_enabled": False, "in_quiet_hours": False},
            }

    ctx = SimpleNamespace(
        get_all_stars=lambda: {
            "chat": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics",
                activated=True,
                star_cls=ChatOwner(),
            )
        },
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(
                completion_text='{"status":"pass","issues":[],"reason":"ok"}',
                output_text='{"rows":2,"cols":2}',
            )
        ),
    )
    if budget is not None:
        ctx._chat_dynamics_provider_budget = budget
    return ctx


@pytest.mark.asyncio
async def test_visual_media_plugin_exclude_groups_and_timezone(tmp_path):
    excluded_umo = "aiocqhttp:GroupMessage:4444"
    allowed_umo = "aiocqhttp:GroupMessage:5555"
    ctx = _chat_context(
        exclude_groups=[excluded_umo],
        rhythm_timezone="America/New_York",
    )
    plugin = VisualMediaPlugin.__new__(VisualMediaPlugin)
    plugin.context = ctx
    plugin._closed = False
    memes = SimpleNamespace(
        on_message=AsyncMock(),
        _prepare_emoji_after_llm=AsyncMock(),
        _prepare_emoji_response=AsyncMock(),
        _start_joint_meme_prefetch=AsyncMock(),
        _prepare_joint_meme_reply=AsyncMock(),
        plugin_config=SimpleNamespace(is_targets_allowed=lambda *a, **kw: True),
        plugin_api=SimpleNamespace(
            _get_index=lambda: {
                "a.png": {
                    "hash": "h1",
                    "desc": "测试图",
                    "category": "happy",
                    "origin_target": allowed_umo,
                    "created_at": datetime(2026, 6, 2, 2, 0, tzinfo=timezone.utc).isoformat(),
                }
            }
        ),
    )
    plugin.memes = memes
    plugin.generation = SimpleNamespace(
        rate_limiter=SimpleNamespace(allows_group=lambda *a, **kw: True),
        generation_tracker=None,
    )
    plugin.behavior = SimpleNamespace(remember_and_prompt=lambda *a, **kw: "")

    # Scope status reports 主控排除
    st_ex = plugin.get_series_scope_status(excluded_umo)
    assert st_ex["enabled"] is False
    assert st_ex["excluded_by_core"] is True
    assert "主控排除" in st_ex["summary"]

    st_ok = plugin.get_series_scope_status(allowed_umo)
    assert st_ok["enabled"] is True
    assert st_ok["excluded_by_core"] is False

    # Passive hooks skip excluded group
    ev_ex = SimpleNamespace(
        unified_msg_origin=excluded_umo,
        get_group_id=lambda: "4444",
        get_extra=lambda *a, **kw: False,
    )
    await plugin.on_message(ev_ex)
    await plugin._prepare_emoji_after_llm(ev_ex, None)
    await plugin._prepare_emoji_response(ev_ex)
    memes.on_message.assert_not_awaited()
    memes._prepare_emoji_after_llm.assert_not_awaited()
    memes._prepare_emoji_response.assert_not_awaited()

    # Journal respects America/New_York date boundary (2026-06-02 02:00 UTC -> 2026-06-01 in NY)
    events_ny_june1 = plugin.get_journal_events(scope=allowed_umo, date="2026-06-01")
    events_ny_june2 = plugin.get_journal_events(scope=allowed_umo, date="2026-06-02")
    assert len(events_ny_june1) == 1
    assert len(events_ny_june2) == 0


@pytest.mark.asyncio
async def test_character_check_and_vision_handler_budget_delegation(tmp_path):
    from PIL import Image

    img_path = tmp_path / "sample.png"
    ref_path = tmp_path / "ref.png"
    Image.new("RGB", (64, 64), (255, 0, 0)).save(img_path)
    Image.new("RGB", (64, 64), (0, 255, 0)).save(ref_path)

    budget = FakeSharedBudget(busy=False)
    ctx = _chat_context(budget=budget)
    owner = SimpleNamespace(
        context=ctx,
        raw_config={
            "media": {
                "character_check_enabled": True,
                "character_check_provider": "vision-check-p",
                "character_check_timeout": 15,
            }
        },
        memes=SimpleNamespace(
            context=ctx,
            plugin_config=SimpleNamespace(model_request_interval=0.0),
        ),
    )
    res = await check_character(
        owner,
        {"name": "小满", "appearance": "黑长直"},
        [str(img_path)],
        [str(ref_path)],
        "画小满",
    )
    assert res["status"] == "pass"
    assert budget.calls[-1]["plugin_id"] == "astrbot_plugin_visual_media"
    assert budget.calls[-1]["purpose"] == "visual_character_check"

    # Busy degradation on character_check
    busy = FakeSharedBudget(busy=True)
    ctx._chat_dynamics_provider_budget = busy
    res_busy = await check_character(
        owner,
        {"name": "小满", "appearance": "黑长直"},
        [str(img_path)],
        [str(ref_path)],
        "画小满",
    )
    assert res_busy["status"] == "unknown"
    assert res_busy["reason"] == "provider_budget_busy"

    # VisionHandler routes through shared budget and degrades cleanly on busy
    vh = VisionHandler(ctx, vision_provider_id="vision-grid-p")
    ctx._chat_dynamics_provider_budget = budget
    grid = await vh.detect_grid_rows_cols(str(img_path))
    assert grid == (2, 2)
    assert budget.calls[-1]["purpose"] == "visual_grid_detect"

    ctx._chat_dynamics_provider_budget = busy
    assert await vh.detect_grid_rows_cols(str(img_path)) is None
