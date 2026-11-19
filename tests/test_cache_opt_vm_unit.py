"""Unit tests for VM-1..VM-4 compatible with tests/conftest.py stub environment."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS
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

from core.processing.natural_emotion_analyzer import NaturalEmotionAnalyzer  # noqa: E402
from core.tools.media_policy import selection_prompt, turn_media_context  # noqa: E402
from tl import background_notify as notify  # noqa: E402


def _tools(*names: str):
    return NS(tools=[NS(name=name, active=True) for name in names])


@pytest.mark.parametrize("reply_modes", [False, True])
@pytest.mark.parametrize("poke_supported", [False, True])
def test_selection_prompt_static_and_turn_context_dynamic(reply_modes, poke_supported):
    s0 = selection_prompt(
        0, _tools(), reply_modes=reply_modes, poke_supported=poke_supported
    )
    s3 = selection_prompt(
        3,
        _tools("gemini_image_generation", "generate_character_image"),
        reply_modes=reply_modes,
        poke_supported=poke_supported,
    )
    assert s0 == s3
    assert "本轮有 3 张" not in s3
    assert "本轮可见的生图工具：" not in s3

    ctx0 = turn_media_context(0, _tools(), reply_modes=reply_modes)
    ctx3 = turn_media_context(
        3,
        _tools("gemini_image_generation"),
        reply_modes=reply_modes,
        attached_previews=2,
    )
    ctx_gated = turn_media_context(
        3,
        _tools("gemini_image_generation"),
        reply_modes=reply_modes,
        preview_gated=True,
    )
    assert "本轮没有可用现成表情候选" in ctx0
    assert "本轮未直接暴露生图工具" in ctx0
    assert "本轮有 3 张现成表情候选" in ctx3 and "前 2 张附有预览图" in ctx3
    assert "本轮可见的生图工具：gemini_image_generation" in ctx3
    assert "仅提供文字元数据" in ctx_gated


@pytest.mark.asyncio
async def test_emotion_analyzer_short_circuits_when_joint_reply_owned_and_splits_prompt():
    calls = []

    async def fake_llm_generate(**kwargs):
        calls.append(kwargs)
        return NS(completion_text='{"query":"轻松回应","emotions":["happy"]}')

    cfg = NS(
        get_categories=lambda: ["happy", "sad"],
        category_info={
            "happy": {"name": "开心", "desc": "快乐"},
            "sad": {"name": "难过", "desc": "低落"},
        },
        emotion_analysis_prompt="",
        emotion_analysis_provider_id="small-model",
        enable_natural_emotion_analysis=True,
        get_keyword_map=lambda: {},
        normalize_category_strict=lambda x: x if x in {"happy", "sad"} else None,
    )
    analyzer = NaturalEmotionAnalyzer(
        NS(plugin_config=cfg, context=NS(llm_generate=fake_llm_generate))
    )

    owned_event = NS(
        unified_msg_origin="qq:GroupMessage:1",
        get_extra=lambda k, d=None: True if k == "stealer_joint_reply_v1" else d,
    )
    assert (
        await analyzer.analyze_for_reply(owned_event, "普通回复文本", user_message="你好")
        is None
    )
    assert calls == []

    normal_event_1 = NS(
        unified_msg_origin="qq:GroupMessage:1",
        get_extra=lambda k, d=None: d,
    )
    normal_event_2 = NS(
        unified_msg_origin="qq:GroupMessage:1",
        get_extra=lambda k, d=None: d,
    )
    assert (
        await analyzer.analyze_for_reply(
            normal_event_1, "辛苦啦早点休息", user_message="刚加完班"
        )
        is not None
    )
    assert (
        await analyzer.analyze_for_reply(
            normal_event_2, "祝你旅途顺利", user_message="准备去旅游"
        )
        is not None
    )
    assert len(calls) == 2
    assert calls[0]["system_prompt"] == calls[1]["system_prompt"]
    assert "happy(快乐)" in calls[0]["system_prompt"]
    assert "刚加完班" not in calls[0]["system_prompt"]
    assert "刚加完班" in calls[0]["prompt"]
    assert "准备去旅游" in calls[1]["prompt"]


@pytest.mark.asyncio
async def test_background_notify_static_system_prompt_and_temp_user_part(monkeypatch):
    class FakeTextPart:
        def __init__(self, text: str):
            self.text = text
            self._no_save = False

        def mark_as_temp(self):
            self._no_save = True
            return self

    captured = []

    class CronEvent:
        def __init__(self, *, context, **kwargs):
            self._has_send_oper = False

    async def get_conv(*, event, plugin_context):
        return NS(history="[]")

    async def build_agent(*, event, plugin_context, req, **kwargs):
        captured.append(req)

        async def steps(limit):
            event._has_send_oper = True
            yield None

        return NS(agent_runner=NS(step_until_done=steps))

    host = NS(
        get_config=lambda **kwargs: {},
        conversation_manager=object(),
        get_llm_tool_manager=lambda: NS(get_builtin_tool=lambda cls: object()),
    )
    monkeypatch.setattr(notify, "_OFFICIAL_API_READY", True)
    monkeypatch.setattr(notify, "TextPart", FakeTextPart)
    monkeypatch.setattr(
        notify, "MessageSession", NS(from_str=lambda umo: NS(message_type="group"))
    )
    monkeypatch.setattr(notify, "CronMessageEvent", CronEvent)
    monkeypatch.setattr(notify, "MainAgentBuildConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(notify, "get_session_tool_timeout", lambda *args: 60)
    monkeypatch.setattr(notify, "_get_session_conv", get_conv)
    monkeypatch.setattr(notify, "build_main_agent", build_agent)
    monkeypatch.setattr(
        notify,
        "BACKGROUND_TASK_RESULT_WOKE_SYSTEM_PROMPT",
        "WAKE RULES:\n{background_task_result}",
    )
    monkeypatch.setattr(
        notify,
        "ProviderRequest",
        lambda: NS(system_prompt="", func_tool=None, extra_user_content_parts=[]),
    )
    monkeypatch.setattr(notify, "ToolSet", lambda: NS(add_tool=lambda tool: None))
    monkeypatch.setattr(notify, "persist_agent_history", AsyncMock())
    monkeypatch.setattr(notify, "_bump_delivery_count", AsyncMock())

    plugin = NS(context=host)
    ev = NS(unified_msg_origin="qq:GroupMessage:1", role="member")
    assert await notify.notify_llm_background_result(
        plugin, ev, {"task_id": "t1", "result": "ok1"}, notice="n1", scene="s"
    )
    assert await notify.notify_llm_background_result(
        plugin, ev, {"task_id": "t2", "result": "ok2"}, notice="n2", scene="s"
    )
    assert captured[0].system_prompt == captured[1].system_prompt
    assert "t1" not in captured[0].system_prompt
    assert "t2" not in captured[1].system_prompt
    assert captured[0].extra_user_content_parts[0]._no_save is True
    assert "t1" in captured[0].extra_user_content_parts[0].text
    assert "t2" in captured[1].extra_user_content_parts[0].text


def test_conf_schema_vm3_keys_valid_json():
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    items = schema["memes"]["items"]
    assert items["joint_reply_max_preview_images"]["default"] == 3
    assert items["joint_reply_preview_max_size"]["default"] == 320
    assert items["joint_reply_preview_format"]["default"] == "webp"
    assert items["joint_reply_preview_quality"]["default"] == 75
    assert items["joint_reply_preview_context_gate"]["default"] is True
