import json
from types import SimpleNamespace as NS

import httpx
import pytest
from PIL import Image

from astrbot_plugin_visual_media.media_behavior import MediaBehavior
from astrbot_plugin_visual_media.character_check import check_character
from astrbot_plugin_visual_media.core.tools.joint_reply import JointMemePlan
from test_http_and_generation import app_for
from test_media_library import Event


def event(text="", sender="1", umo="qq:GroupMessage:1"):
    item = Event(umo)
    item.message_str = text
    item.get_sender_id = lambda: sender
    return item


def candidate(tmp_path, name="image.png", color="blue"):
    path = tmp_path / name
    Image.new("RGB", (24, 24), color).save(path)
    return {"path": str(path), "overlay_text": "测试"}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("以后少发表情", "less"),
        ("安安，你少发点表情包吧", "less"),
        ("以后不要再发表情包了", "off"),
        ("多发点表情吧", "more"),
        ("以后说话少发表情，多用文字", "less"),
    ],
)
def test_explicit_preference_is_member_and_session_scoped(tmp_path, text, expected):
    store = MediaBehavior(tmp_path)
    one = event(text)
    store.remember_explicit(one)
    assert store.preferences(one)["frequency"] == expected
    assert store.preferences(event(sender="2"))["frequency"] == "auto"
    assert store.preferences(event(umo="other:GroupMessage:1"))["frequency"] == "auto"
    assert MediaBehavior(tmp_path).preferences(one)["frequency"] == expected


@pytest.mark.parametrize(
    "text",
    [
        "他说“以后少发表情”",
        "如果我让你少发表情呢",
        "这张别再发了",
        "少发表情是什么意思",
        "我不喜欢水彩画风",
    ],
)
def test_quoted_hypothetical_and_ambiguous_text_not_saved(tmp_path, text):
    store = MediaBehavior(tmp_path)
    one = event(text)
    store.remember_explicit(one)
    assert store.preference_rows() == []


def test_style_block_last_and_reset(tmp_path):
    store = MediaBehavior(tmp_path)
    one = event("我喜欢水彩画风")
    store.remember_explicit(one)
    assert store.preferences(one)["style"] == "水彩"
    store.remember_explicit(event("以后画图用水彩画风"))
    assert store.preferences(one)["style"] == "水彩"
    c = candidate(tmp_path)
    store.delivered(one, c)
    two = event("这张别再发了")
    store.remember_explicit(two)
    assert store.reason(two, c) == "member_blocked_image"
    assert store.preferences(event(sender="2"))["blocked"] == []
    store.remember_explicit(event("恢复默认媒体偏好"))
    assert store.preferences(one) == {"frequency": "auto", "style": "", "blocked": []}


def test_no_member_cannot_change_group_defaults(tmp_path):
    store = MediaBehavior(tmp_path)
    store.remember_explicit(event("少发表情", sender=""))
    assert not store.preference_rows()


def test_receipt_only_repeat_guard_and_explicit_repeat(tmp_path):
    store = MediaBehavior(tmp_path)
    c = candidate(tmp_path)
    one = event()
    store.record(one, "meme", "selected", "test")
    assert store.filter_candidates(event(), [c]) == [c]
    assert store.reserve(one, c)
    assert not store.reserve(event(), c)
    store.release(one, c)
    assert store.reserve(event(), c)
    store.delivered(one, c)
    assert store.filter_candidates(event(), [c]) == []
    assert store.filter_candidates(event("来个开心表情"), [c]) == []
    assert store.filter_candidates(event("再发一次刚才的表情"), [c]) == [c]
    assert store.filter_candidates(event(umo="qq:GroupMessage:2"), [c]) == [c]
    # Persist across plugin reloads.
    assert MediaBehavior(tmp_path).filter_candidates(event(), [c]) == []


@pytest.mark.asyncio
async def test_failed_send_does_not_poison_recent_or_reservation(
    owner, tmp_path, monkeypatch
):
    selector = owner.memes.meme_selector

    async def fail(*args):
        return None

    monkeypatch.setattr(selector._smart_select_service, "send_emoji_message", fail)
    c = candidate(tmp_path)
    assert not await selector.send_emoji_message(event(), c["path"])
    assert owner.behavior.filter_candidates(event(), [c]) == [c]
    assert owner.behavior.reserve(event(), c)
    assert owner.behavior.records()[0]["phase"] == "failed"


@pytest.mark.asyncio
async def test_joint_plan_only_marks_after_confirmed_callback(owner, tmp_path):
    from unittest.mock import AsyncMock

    one = event()
    c = candidate(tmp_path)
    fake = NS(
        media_behavior=owner.behavior,
        plugin_config=NS(),
        is_send_enabled_for_event=lambda e: True,
        meme_selector=NS(
            is_path_allowed_for_event=lambda *a: True, record_emoji_usage=AsyncMock()
        ),
        _emoji_sender_engine=NS(
            emoji_turn_state=lambda e: NS(
                mark_active_sent=lambda: None, set_candidates=lambda x: None
            ),
            mark_auto_emoji_sent=AsyncMock(),
        ),
    )
    plan = JointMemePlan(fake, one, [c])
    _, chains = plan.resolve('{"media":"meme","text":"","meme_id":1}')
    assert owner.behavior.filter_candidates(event(), [c]) == [c]
    await chains[0]._cd_media_success_v1()
    assert owner.behavior.filter_candidates(event(), [c]) == []
    assert any(
        r["phase"] == "delivered" and r["trace"] == one._visual_media_trace
        for r in owner.behavior.records()
    )


@pytest.mark.asyncio
async def test_generic_image_receipt_not_text_only_success(owner):
    one = event()
    tool = NS(name="send_message_to_user")
    result = NS(content=[NS(text="Message sent to session qq:GroupMessage:1")])
    await owner._track_external_image_delivery(
        one, tool, {"messages": [{"type": "plain", "text": "完成"}]}, result
    )
    assert not owner.behavior.records()
    await owner._track_external_image_delivery(
        one,
        tool,
        {"messages": [{"type": "image", "url": "https://example.invalid/a.png"}]},
        result,
    )
    assert owner.behavior.records()[0]["phase"] == "delivered"
    assert owner.behavior.records()[0]["mode"] == "image"


def test_similar_images_demoted_not_removed(tmp_path):
    store = MediaBehavior(tmp_path)
    first = candidate(tmp_path)
    near = candidate(tmp_path, "near.png", "red")
    store.delivered(event(), first)
    assert store.filter_candidates(event(), [near]) == [near]


def test_less_frequency_and_last_style(tmp_path):
    store = MediaBehavior(tmp_path)
    one = event("少发表情")
    store.remember_explicit(one)
    store.delivered(one, candidate(tmp_path))
    assert (
        store.reason(event(), candidate(tmp_path, "new.png", "red"))
        == "member_prefers_fewer_memes"
    )
    store.record(one, "generate", "submitted", "test", {"style": "原作水彩"})
    store.remember_explicit(event("我喜欢这个画风"))
    assert store.preferences(one)["style"] == "原作水彩"


@pytest.mark.asyncio
async def test_decision_api_auth_and_scoped_reset(owner):
    one = event("少发表情")
    owner.behavior.remember_explicit(one)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(owner)), base_url="http://test"
    ) as client:
        prefix = "/astrbot_plugin_visual_media/media/behavior"
        assert (await client.get(prefix)).status_code == 403
        client.headers["x-test-user"] = "admin"
        result = (await client.get(prefix)).json()["data"]
        assert len(result["preferences"]) == 1
        assert result["records"][0]["phase"] == "saved"
        assert "少发表情" not in json.dumps(result)
        assert (
            await client.post(
                prefix + "/reset", json={"scope": "wrong", "member": "wrong"}
            )
        ).status_code == 400
        scope, member = owner.behavior.identity(one)
        assert (
            await client.post(
                prefix + "/reset", json={"scope": scope, "member": member}
            )
        ).status_code == 200
        assert not owner.behavior.preference_rows()


@pytest.mark.asyncio
async def test_character_comparison_uses_references_and_unknown_on_invalid(
    owner, tmp_path, monkeypatch
):
    from astrbot_plugin_visual_media.core.processing.vlm_call_service import (
        VLMCallService,
    )

    async def provider(*args):
        return "test-provider"

    monkeypatch.setattr(VLMCallService, "_resolve_vision_provider", provider)
    reference = candidate(tmp_path)["path"]
    output = candidate(tmp_path, "output.png", "red")["path"]
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return NS(
            completion_text='{"status":"mismatch","issues":["发色不符"],"reason":"参考为蓝色"}'
        )

    owner.context.llm_generate = generate
    profile = {"name": "test", "appearance": "蓝发"}
    result = await check_character(owner, profile, [output], [reference], "微笑")
    assert result["status"] == "mismatch"
    assert calls[0]["image_urls"] == [reference, output]

    async def broken(**kwargs):
        return NS(completion_text="NOT JSON")

    owner.context.llm_generate = broken
    assert (await check_character(owner, profile, [output], [reference], "微笑"))[
        "status"
    ] == "unknown"
    assert (await check_character(owner, profile, [output], [], "微笑"))[
        "status"
    ] == "unknown"
