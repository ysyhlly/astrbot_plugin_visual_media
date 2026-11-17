"""Tests for read-only series shared defaults inheritance in Visual Media."""

from __future__ import annotations

import sys
import types
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

from core.processing.natural_emotion_analyzer import NaturalEmotionAnalyzer  # noqa: E402


def _chat_context(*, activated: bool = True, chat_provider: str = "session-default"):
    class ChatOwner:
        def get_series_shared_config(self):
            return {
                "reply_provider": "series-reply",
                "persona_id": "series-persona",
            }

    return SimpleNamespace(
        get_all_stars=lambda: {
            "chat": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics",
                activated=activated,
                star_cls=ChatOwner(),
            )
        },
        get_current_chat_provider_id=AsyncMock(return_value=chat_provider),
    )


def _analyzer(configured_provider: str, context):
    analyzer = NaturalEmotionAnalyzer.__new__(NaturalEmotionAnalyzer)
    analyzer.plugin = SimpleNamespace(
        plugin_config=SimpleNamespace(emotion_analysis_provider_id=configured_provider),
        context=context,
    )
    return analyzer


@pytest.mark.asyncio
async def test_visual_media_inherits_series_provider_when_empty():
    ctx = _chat_context()
    analyzer = _analyzer("", ctx)
    event = SimpleNamespace(unified_msg_origin="qq:GroupMessage:100")
    assert await analyzer._get_text_provider(event) == "series-reply"


@pytest.mark.asyncio
async def test_visual_media_explicit_provider_takes_priority():
    ctx = _chat_context()
    analyzer = _analyzer("explicit-emotion-provider", ctx)
    event = SimpleNamespace(unified_msg_origin="qq:GroupMessage:100")
    assert await analyzer._get_text_provider(event) == "explicit-emotion-provider"


@pytest.mark.asyncio
async def test_visual_media_safe_fallback_when_chat_dynamics_missing_or_disabled():
    event = SimpleNamespace(unified_msg_origin="qq:GroupMessage:100")
    for ctx in (
        SimpleNamespace(
            get_all_stars=lambda: {},
            get_current_chat_provider_id=AsyncMock(return_value="session-default"),
        ),
        _chat_context(activated=False),
    ):
        analyzer = _analyzer("", ctx)
        assert await analyzer._get_text_provider(event) == "session-default"
