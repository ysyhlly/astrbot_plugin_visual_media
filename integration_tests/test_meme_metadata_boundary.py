"""Internal image notes never become prose or implicit delivery commands."""

import json

import pytest
from astrbot_plugin_visual_media.core.tools.joint_reply import (
    MEME_HISTORY_LABEL,
    JointMemePlan,
)
from test_media_library import Event
from test_media_policy import plugin

DETAILS = {
    "id": 2,
    "desc": "白发少女举牌温柔道晚安",
    "overlay_text": "",
    "emotion": "love",
    "character": "",
    "scenes": "睡前道晚安, 表达关心, 亲密聊天结尾",
}
NOTE = "[我发送的表情包资料：" + json.dumps(DETAILS, ensure_ascii=False) + "]"
CAPTION = "晚安喽，被窝盖好。"


@pytest.mark.parametrize("count", [0, 3])
@pytest.mark.parametrize("separator", ["\n", "\\\n", " ", ""])
@pytest.mark.parametrize("structured", [False, True])
def test_incident_text_keeps_only_prose(tmp_path, count, separator, structured):
    path = tmp_path / "available.png"
    path.write_bytes(b"offline")
    event = Event()
    plan = JointMemePlan(plugin(), event, [{"path": str(path)}] * count)
    raw = CAPTION + separator + NOTE
    if structured:
        raw = json.dumps({"reply_mode": "text", "text": raw, "meme_id": None})
    assert plan.resolve(raw) == (CAPTION, [])
    assert event.get_extra("visual_media_choice_v1")["mode"] == "text"
    assert not event.get_extra("visual_media_choice_v1")["delivered"]


def test_multiple_notes_nested_brackets_and_following_prose():
    details = {**DETAILS, "desc": '举牌说 "晚安 ] }"', "extra": ["[", {"x": "]"}]}
    note = "[我发送的表情包资料：" + json.dumps(details) + "]"
    modern = "[" + MEME_HISTORY_LABEL + "：" + json.dumps(details) + "]"
    plan = JointMemePlan(plugin(), Event(), [])
    assert plan.resolve(CAPTION + "\n" + note + "\n" + modern + "\n明天见。") == (
        CAPTION + "\n明天见。",
        [],
    )


@pytest.mark.parametrize(
    "raw", [NOTE, CAPTION + "\n" + NOTE[:-1], CAPTION + "\n" + NOTE[:40]]
)
@pytest.mark.parametrize("count", [0, 3])
def test_empty_or_incomplete_note_is_never_sent(raw, count):
    plan = JointMemePlan(plugin(), Event(), [{"path": "/offline"}] * count)
    with pytest.raises(ValueError):
        plan.resolve(raw)


@pytest.mark.parametrize("mode", ["text", "text_meme", "meme_only", "silent"])
def test_note_only_is_not_a_valid_explicit_reply(mode, tmp_path):
    path = tmp_path / "available.png"
    path.write_bytes(b"offline")
    plan = JointMemePlan(plugin(), Event(), [{"path": str(path)}])
    with pytest.raises(ValueError):
        plan.resolve(
            json.dumps(
                {
                    "reply_mode": mode,
                    "text": NOTE,
                    "meme_id": 1 if "meme" in mode else None,
                }
            )
        )


@pytest.mark.parametrize(
    "text",
    [
        "普通晚安。",
        "这里是示例：\n```text\n" + NOTE + "\n```",
        "示例 `" + NOTE + "` 是内部记录。",
        "运行示例：\n~~~text\n" + NOTE + "\n~~~",
        '{"description":"请保留普通 JSON", "ids":[1,2]}',
        json.dumps({"example": NOTE}, ensure_ascii=False),
    ],
)
def test_requested_content_is_preserved(text):
    plan = JointMemePlan(plugin(), Event(), [])
    assert plan.resolve(
        json.dumps({"reply_mode": "text", "text": text, "meme_id": None})
    ) == (text, [])


@pytest.mark.parametrize("caption", [CAPTION, CAPTION + "\n" + NOTE, ""])
def test_real_meme_keeps_image_and_separate_history_context(tmp_path, caption):
    path = tmp_path / "available.png"
    path.write_bytes(b"offline")
    plan = JointMemePlan(plugin(), Event(), [{**DETAILS, "path": str(path)}])
    mode = "text_meme" if caption else "meme_only"
    text, chains = plan.resolve(
        json.dumps({"reply_mode": mode, "text": caption, "meme_id": 1})
    )
    assert text == "" and len(chains) == 1
    chain = chains[0]
    assert chain.get_plain_text() == (CAPTION if caption else "")
    assert type(chain.chain[-1]).__name__ == "Image"
    assert chain._cd_media_validate_v1()
    assert DETAILS["desc"] in chain._cd_delivery_text_v1
    assert MEME_HISTORY_LABEL in chain._cd_delivery_text_v1
    assert '"id"' not in chain._cd_delivery_text_v1
    assert "我发送的表情包资料" not in chain._cd_delivery_text_v1
