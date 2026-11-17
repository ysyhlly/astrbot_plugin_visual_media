"""Real copied CD bridge and AstrBot hooks; provider/network and platform are isolated."""

import json
import time


import pytest

pytest.importorskip("astrbot_plugin_chat_dynamics")
from astrbot_plugin_chat_dynamics.integration.test_persona_agent import (
    host_fixture as host_fixture,
)
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.pipeline.context_utils import call_event_hook as real_hook
from astrbot.core.star.star import StarMetadata, star_map
from astrbot.core.star.star_handler import (
    EventType,
    StarHandlerMetadata,
    StarHandlerRegistry,
)
from astrbot_plugin_visual_media.core.tools import joint_prefetch
from test_media_library import generated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,has_candidates,poke_supported",
    [
        ("text", True, False),
        ("meme_only", True, False),
        ("text_meme", True, False),
        ("silent", True, False),
        ("text", False, False),
        ("silent", False, False),
        ("poke", False, True),
        ("poke", False, False),
    ],
)
async def test_native_four_mode_plan(
    owner, host_fixture, monkeypatch, mode, has_candidates, poke_supported
):
    """Exercise the actual request hook, native runner and response parser together."""
    from astrbot.core.pipeline import context_utils
    from astrbot.core import astr_agent_hooks

    ctx, event, calls = host_fixture
    if mode == "poke":
        from types import SimpleNamespace

        event.platform_meta.name = "aiocqhttp"
        event.message_obj.sender.user_id = "123456" if poke_supported else "not-numeric"
        event.message_obj.group_id = "778899"
        event.message_obj.self_id = "998877"

        async def call_action(**kwargs):
            return {"status": "ok"}

        event.bot = SimpleNamespace(call_action=call_action)
    job, _ = await generated(owner, umo=None)
    saved = await owner.library.save_generation(
        job, public=True, description="开心大笑"
    )
    path, _ = owner.memes.plugin_api._find_index_entry_by_hash(saved["hash"])

    class Prefetch:
        started = time.monotonic()

        async def result(self, *args, **kwargs):
            return [{"path": path, "desc": "开心大笑"}] if has_candidates else []

        async def close(self):
            pass

    monkeypatch.setattr(
        joint_prefetch, "start_joint_prefetch", lambda *args: Prefetch()
    )
    registry = StarHandlerRegistry()
    module = owner.__class__.__module__
    monkeypatch.setitem(
        star_map,
        module,
        StarMetadata(
            name=owner.name, star_cls=owner, module_path=module, activated=True
        ),
    )
    name = "_prepare_joint_meme_reply"
    registry.append(
        StarHandlerMetadata(
            EventType.OnLLMRequestEvent,
            module + "." + name,
            name,
            module,
            getattr(owner, name),
            [],
        )
    )
    monkeypatch.setattr(context_utils, "star_handlers_registry", registry)
    monkeypatch.setattr(context_utils, "call_event_hook", real_hook)
    monkeypatch.setattr(astr_agent_hooks, "call_event_hook", real_hook)
    text = "哈哈。" if mode in {"text", "text_meme"} else ""

    async def chat(**kwargs):
        calls.append(kwargs)
        return LLMResponse(
            role="assistant",
            completion_text=json.dumps(
                {
                    "reply_mode": mode,
                    "text": text,
                    "meme_id": 1 if mode in {"meme_only", "text_meme"} else None,
                    **({"narration": "安安正在熟睡中"} if mode == "silent" else {}),
                }
            ),
        )

    ctx.provider.text_chat = chat
    bridge = AstrBotAgentBridge(ctx)
    if mode == "poke" and not poke_supported:
        from astrbot_plugin_chat_dynamics.core.agent_bridge import ModelReplyFailure

        with pytest.raises(ModelReplyFailure, match="invalid_reply_mode_plan"):
            await bridge.generate(
                event, (event,), "prompt", await bridge.snapshot(event), "provider"
            )
        assert len(calls) == 1 and not event.sent and not ctx.saved
        return
    out = await bridge.generate(
        event, (event,), "prompt", await bridge.snapshot(event), "provider"
    )
    expected_mode = "narration" if mode == "silent" else mode
    assert out.reply_mode == expected_mode
    assert out.requested_reply_mode == mode
    if mode == "silent":
        assert out.narration == "（安安正在熟睡中）"
    assert not event.sent and not ctx.saved and len(calls) == 1
    assert "本轮回应方式" in str(calls[0])
    assert bool(out.chains) == (mode in {"meme_only", "text_meme"})
    if out.chains:
        assert [type(c).__name__ for c in out.chains[0].chain] == (
            ["Plain", "Image"] if text else ["Image"]
        )
    else:
        assert out.text == text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,wants_meme",
    [("以后少发表情，多用文字。", False), ("今天笑得肚子疼，来张开心的表情。", True)],
)
async def test_cd_native_bridge_invokes_real_visual_hook(
    owner, host_fixture, monkeypatch, text, wants_meme
):
    from astrbot.core.pipeline import context_utils
    from astrbot.core import astr_agent_hooks

    ctx, e, calls = host_fixture
    e.message_str = text
    job, _ = await generated(owner, umo=None)
    saved = await owner.library.save_generation(
        job, public=True, description="开心大笑"
    )
    path, meta = owner.memes.plugin_api._find_index_entry_by_hash(saved["hash"])

    class Prefetch:
        started = time.monotonic()

        async def result(self, *a, **k):
            return (
                [{"path": path, "desc": "开心大笑", "emotion": "happy"}]
                if wants_meme
                else []
            )

        async def close(self):
            pass

    monkeypatch.setattr(
        joint_prefetch, "start_joint_prefetch", lambda *args: Prefetch()
    )
    registry = StarHandlerRegistry()
    module = owner.__class__.__module__
    monkeypatch.setitem(
        star_map,
        module,
        StarMetadata(
            name=owner.name, star_cls=owner, module_path=module, activated=True
        ),
    )
    name = "_prepare_joint_meme_reply"
    registry.append(
        StarHandlerMetadata(
            EventType.OnLLMRequestEvent,
            module + "." + name,
            name,
            module,
            getattr(owner, name),
            [],
        )
    )
    monkeypatch.setattr(context_utils, "star_handlers_registry", registry)
    monkeypatch.setattr(context_utils, "call_event_hook", real_hook)
    monkeypatch.setattr(astr_agent_hooks, "call_event_hook", real_hook)

    async def chat(**kwargs):
        calls.append(kwargs)
        return LLMResponse(
            role="assistant",
            completion_text=json.dumps({"media": "meme", "text": "哈哈", "meme_id": 1})
            if wants_meme
            else "记住了，以后主要用文字聊。",
        )

    ctx.provider.text_chat = chat
    bridge = AstrBotAgentBridge(ctx)
    out = await bridge.generate(
        e, (e,), "planning prompt", await bridge.snapshot(e), "provider"
    )
    assert len(calls) == 1 and not e.sent and not ctx.saved
    assert "本轮统一视觉表达" in str(calls[0])
    if wants_meme:
        assert len(out.chains) == 1 and out.chains[0]._cd_media_validate_v1()
        # Confirmation is deliberately exercised in the isolated test store, never on QQ.
        await out.chains[0]._cd_media_success_v1()
        assert owner.behavior.reason(e, {"path": path}) == "recent_exact_repeat"
    else:
        assert not out.chains and out.text
        assert owner.behavior.preferences(e)["frequency"] == "less"
        assert "少用表情" in str(calls[0])
    await owner.behavior.close()
    assert any(
        r["mode"] == ("meme" if wants_meme else "text")
        for r in owner.behavior.records()
    )


@pytest.mark.asyncio
async def test_native_runner_dispatches_real_image_receipt_hook(
    owner, host_fixture, monkeypatch
):
    from astrbot.core.pipeline import context_utils
    from astrbot.core import astr_agent_hooks
    from astrbot.core.tools.message_tools import SendMessageToUserTool
    from astrbot_plugin_visual_media.tl.delivery_evidence import expected_image_keys

    ctx, e, calls = host_fixture
    url = "https://example.invalid/generated.png"
    e._visual_generation_task = "native-task"
    e._visual_expected_image_keys = expected_image_keys({"image_urls": [url]})
    ctx.tools["send_message_to_user"] = SendMessageToUserTool()
    ctx.persona_manager.personas_v3[0]["tools"] = ["send_message_to_user"]
    captured = []

    async def capture(session, chain):
        captured.append(str(session))
        return True

    ctx.send_message = capture

    async def chat(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return LLMResponse(
                role="assistant",
                completion_text="",
                tools_call_name=["send_message_to_user"],
                tools_call_args=[{"messages": [{"type": "image", "url": url}]}],
                tools_call_ids=["audit-call"],
            )
        return LLMResponse(role="assistant", completion_text="图片已交付。")

    ctx.provider.text_chat = chat
    registry = StarHandlerRegistry()
    module = owner.__class__.__module__
    monkeypatch.setitem(
        star_map,
        module,
        StarMetadata(
            name=owner.name, star_cls=owner, module_path=module, activated=True
        ),
    )
    method = "_track_external_image_delivery"
    registry.append(
        StarHandlerMetadata(
            EventType.OnLLMToolRespondEvent,
            module + "." + method,
            method,
            module,
            getattr(owner, method),
            [],
        )
    )
    monkeypatch.setattr(context_utils, "star_handlers_registry", registry)
    monkeypatch.setattr(context_utils, "call_event_hook", real_hook)
    monkeypatch.setattr(astr_agent_hooks, "call_event_hook", real_hook)
    bridge = AstrBotAgentBridge(ctx)
    await bridge.generate(e, (e,), "request", await bridge.snapshot(e), "provider")
    assert captured == [e.unified_msg_origin] and not e.sent
    rows = [r for r in owner.behavior.records() if r["reason"] == "image_tool_receipt"]
    assert len(rows) == 1 and rows[0]["phase"] == "delivered"
    assert (
        rows[0]["mode"] == "generate" and rows[0]["details"]["task_id"] == "native-task"
    )
    assert rows[0]["details"]["matched_image_count"] == 1
