"""Four-mode plans preserve permissions, content shape and legacy callers."""

import json

import pytest
from astrbot_plugin_visual_media.core.tools.joint_reply import JointMemePlan
from astrbot_plugin_visual_media.core.tools.media_policy import selection_prompt
from test_media_library import Event
from test_media_policy import plugin, tools


@pytest.mark.parametrize("mode", ["text", "meme_only", "text_meme", "poke", "silent"])
def test_reply_action_shapes(tmp_path, mode):
    path = tmp_path / "meme.png"
    path.write_bytes(b"offline")
    event = Event()
    if mode == "poke":
        event._cd_reply_poke_supported_v1 = True
    plan = JointMemePlan(plugin(), event, [{"path": str(path)}])
    text = "哈哈" if mode in ("text", "text_meme") else ""
    number = 1 if mode in ("meme_only", "text_meme") else None
    payload = {"reply_mode": mode, "text": text, "meme_id": number}
    if mode == "silent":
        payload["narration"] = "安安正在熟睡中"
    result, chains = plan.resolve(
        json.dumps(payload)
    )
    assert plan.reply_mode == ("narration" if mode == "silent" else mode)
    assert plan.requested_reply_mode == mode
    if mode == "silent":
        assert plan.narration == "（安安正在熟睡中）"
        assert result == "" and not chains
        return
    if number:
        assert result == "" and len(chains) == 1
        assert [type(c).__name__ for c in chains[0].chain] == (
            ["Plain", "Image"] if text else ["Image"]
        )
    else:
        assert result == text and not chains


@pytest.mark.parametrize("count", [0, 1])
def test_narrated_silence_is_explicit_even_without_candidates(tmp_path, count):
    event = Event()
    plan = JointMemePlan(plugin(), event, [{"path": str(tmp_path / "x")}] * count)
    assert plan.resolve(
        '{"reply_mode":"silent","text":"","meme_id":null,"narration":"（安安看了看手机，决定暂时不回应）"}'
    ) == ("", [])
    assert plan.reply_mode == "narration" and plan.requested_reply_mode == "silent"
    assert plan.narration == "（安安看了看手机，决定暂时不回应）"
    assert plan.reply_reason == "model_silent_narration"
    assert event.get_extra("visual_media_choice_v1")["mode"] == "narration"
    prompt = selection_prompt(count, tools(), reply_modes=True, poke_supported=True)
    assert "silent" in prompt and "私聊" in prompt and "poke" in prompt
    assert "不得臆造睡眠、忙碌或正在做的事" in prompt


def test_poke_requires_chat_dynamics_capability(tmp_path):
    event = Event()
    event._cd_reply_poke_supported_v1 = True
    plan = JointMemePlan(plugin(), event, [])
    assert plan.resolve('{"reply_mode":"poke","text":"","meme_id":null}') == ("", [])
    assert plan.reply_mode == plan.requested_reply_mode == "poke"
    assert plan.reply_reason == "model_selected_poke"

    unsupported = JointMemePlan(plugin(), Event(), [])
    with pytest.raises(ValueError, match="invalid_reply_mode_plan"):
        unsupported.resolve('{"reply_mode":"poke","text":"","meme_id":null}')


@pytest.mark.parametrize("mode", ["meme_only", "text_meme"])
def test_unavailable_meme_never_invents_words(tmp_path, mode):
    plan = JointMemePlan(plugin(), Event(), [{"path": str(tmp_path / "missing")}])
    text = "哈" if mode == "text_meme" else ""
    assert plan.resolve(
        json.dumps({"reply_mode": mode, "text": text, "meme_id": 1})
    ) == (text, [])
    assert plan.reply_mode == ("text" if text else "silent")
    assert plan.reply_reason == "meme_unavailable"


@pytest.mark.parametrize(
    "payload",
    [
        {"reply_mode": "silent", "text": "我不回复了"},
        {"reply_mode": "silent", "text": "", "meme_id": None},
        {"reply_mode": "silent", "text": "", "meme_id": None, "narration": 42},
        {"reply_mode": "silent", "text": "", "meme_id": None, "narration": "   "},
        {
            "reply_mode": "silent",
            "text": "",
            "meme_id": None,
            "narration": "第一行\n第二行",
        },
        {"reply_mode": "silent", "text": "", "meme_id": None, "narration": "安" * 119},
        {"reply_mode": "meme_only", "text": "哈", "meme_id": 1},
        {"reply_mode": "text_meme", "text": "", "meme_id": 1},
        {"reply_mode": "meme_only", "text": "", "meme_id": True},
        {"reply_mode": "meme_only", "text": "", "meme_id": 99},
        {"reply_mode": "text", "text": ""},
        {"reply_mode": "text", "text": "哈", "meme_id": 1},
        {"reply_mode": "text", "text": "好", "narration": "角色旁白"},
        {"reply_mode": "poke", "text": "戳一下", "meme_id": None},
    ],
)
def test_invalid_modes_are_errors(payload):
    with pytest.raises(ValueError):
        JointMemePlan(plugin(), Event(), []).resolve(json.dumps(payload))


def test_generation_cannot_be_reported_as_silent():
    event = Event()
    event.set_extra("visual_media_generation_requested", True)
    with pytest.raises(ValueError, match="silent_after_generation"):
        JointMemePlan(plugin(), event, []).resolve(
            '{"reply_mode":"silent","text":"","narration":"暂时没有回应"}'
        )


def test_poke_cannot_follow_generation():
    event = Event()
    event._cd_reply_poke_supported_v1 = True
    event.set_extra("visual_media_generation_requested", True)
    with pytest.raises(ValueError, match="poke_after_generation"):
        JointMemePlan(plugin(), event, []).resolve(
            '{"reply_mode":"poke","text":"","meme_id":null}'
        )


def test_malformed_silence_is_not_plain_text():
    with pytest.raises(ValueError):
        JointMemePlan(plugin(), Event(), []).resolve('{"reply_mode":"silent",')
