import json
from types import SimpleNamespace as NS

import pytest
from astrbot_plugin_visual_media.core.tools import joint_prefetch, joint_reply
from astrbot_plugin_visual_media.core.tools.media_policy import selection_prompt
from test_media_library import Event


def plugin():
    return NS(
        plugin_config=NS(llm_joint_reply=True, auto_send_meme=True),
        is_send_enabled_for_event=lambda event: True,
        meme_selector=NS(is_path_allowed_for_event=lambda path, event: True),
        _emoji_sender_engine=NS(is_command_event=lambda event: False),
    )


def tools(*names):
    return NS(
        tools=[NS(name=name, active=True) for name in names],
        remove_tool=lambda name: None,
    )


def test_no_candidates_still_exposes_generation_without_json_streaming():
    prompt = selection_prompt(0, tools("gemini_image_generation"))
    assert "仍可按语境选择文字" in prompt
    assert "gemini_image_generation" in prompt
    assert "直接用普通文本" in prompt
    assert "本轮仅文字回复" not in prompt
    assert "仅因图库没有匹配，不能推断需要生图" in prompt


def test_candidates_do_not_remove_generation_choice():
    prompt = selection_prompt(
        3, tools("gemini_image_generation", "generate_character_image")
    )
    assert "text|meme|generate" in prompt
    assert "现成表情不能表达" in prompt
    assert "send_message_to_user" in prompt
    assert "不要再附带本轮候选表情" in prompt


def test_unavailable_tools_are_not_advertised_as_callable():
    prompt = selection_prompt(1, tools("search_meme"))
    assert "未直接暴露生图工具" in prompt
    assert "本轮可见的生图工具：" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "meme_enabled,send_allowed", [(True, True), (False, True), (True, False)]
)
async def test_request_policy_keeps_generation_tools_and_permission_boundaries(
    monkeypatch, meme_enabled, send_allowed
):
    fake = plugin()
    fake.plugin_config.auto_send_meme = meme_enabled
    fake.is_send_enabled_for_event = lambda event: send_allowed
    event = Event()
    event._cd_joint_meme_supported_v1 = True
    event._cd_original_user_text_v1 = "想象一个戴围巾的蓝鲸"
    monkeypatch.setattr(
        joint_prefetch, "start_joint_prefetch", lambda plugin, event: None
    )
    tool_set = tools("search_meme", "send_meme", "gemini_image_generation")
    tool_set.remove_tool = lambda name: setattr(
        tool_set, "tools", [tool for tool in tool_set.tools if tool.name != name]
    )
    request = NS(
        system_prompt="persona", extra_user_content_parts=[], func_tool=tool_set
    )
    assert await joint_reply.prepare_joint_reply(fake, event, request)
    assert [tool.name for tool in request.func_tool.tools] == [
        "gemini_image_generation"
    ]
    assert event.get_extra("visual_media_policy_v1") is True
    assert "仅使用文字回复" not in str(request.extra_user_content_parts)
    assert "直接用普通文本" in request.system_prompt


@pytest.mark.asyncio
async def test_non_owned_and_command_requests_are_not_taken_over():
    fake = plugin()
    event = Event()
    request = NS(system_prompt="unchanged")
    assert not await joint_reply.prepare_joint_reply(fake, event, request)
    event._cd_joint_meme_supported_v1 = True
    fake._emoji_sender_engine.is_command_event = lambda event: True
    assert not await joint_reply.prepare_joint_reply(fake, event, request)
    assert request.system_prompt == "unchanged"


@pytest.mark.parametrize("count", [0, 1])
def test_json_generation_without_tool_never_executes_or_claims_success(count, tmp_path):
    event = Event()
    candidates = [{"path": str(tmp_path / "candidate.png")}] if count else []
    plan = joint_reply.JointMemePlan(plugin(), event, candidates)
    text, chains = plan.resolve(
        json.dumps({"media": "generate", "text": "已经画好了", "meme_id": None})
    )
    assert "还没有生成" in text
    assert chains == []
    assert (
        event.get_extra("visual_media_choice_v1")["reason"]
        == "generation_without_tool_rejected"
    )


@pytest.mark.parametrize(
    "completion",
    [
        "正在画，等我一下。",
        '{"media":"generate","text":"正在画","meme_id":null}',
        '{"media":"meme","text":"正在画","meme_id":1}',
    ],
)
def test_native_generation_owns_visual_turn_and_suppresses_prefetched_meme(
    completion, tmp_path
):
    event = Event()
    event.set_extra("visual_media_generation_requested", True)
    plan = joint_reply.JointMemePlan(
        plugin(), event, [{"path": str(tmp_path / "candidate.png")}]
    )
    text, chains = plan.resolve(completion)
    assert text and chains == []
    assert event.get_extra("visual_media_choice_v1")["mode"] == "generate"
    assert not event.get_extra("visual_media_choice_v1")["delivered"]


def test_explicit_text_selection_ignores_stray_candidate_id(tmp_path):
    event = Event()
    plan = joint_reply.JointMemePlan(
        plugin(), event, [{"path": str(tmp_path / "candidate.png")}]
    )
    assert plan.resolve('{"media":"text","text":"你好","meme_id":1}') == ("你好", [])
    assert event.get_extra("visual_media_choice_v1")["mode"] == "text"


def test_meme_selection_revalidates_scope_and_generation(tmp_path):
    file = tmp_path / "candidate.png"
    file.write_bytes(b"fixture")
    event = Event()
    fake = plugin()
    plan = joint_reply.JointMemePlan(fake, event, [{"path": str(file)}])
    _, chains = plan.resolve('{"media":"meme","text":"","meme_id":1}')
    assert chains[0]._cd_media_validate_v1()
    assert event.get_extra("visual_media_choice_v1")["mode"] == "meme"
    fake.is_send_enabled_for_event = lambda event: False
    assert not chains[0]._cd_media_validate_v1()
    with pytest.raises(ValueError, match="unavailable"):
        plan.resolve('{"media":"meme","text":"","meme_id":1}')


@pytest.mark.asyncio
async def test_owner_capability_tracks_lifecycle(owner):
    manifest = owner.get_dynamics_capabilities()
    assert manifest["capabilities"] == {"media": 1}
    assert manifest["enabled"] and manifest["ready"]
    await owner.terminate()
    assert owner.get_dynamics_capabilities()["enabled"] is False
