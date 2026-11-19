"""Regression tests for VM-1..VM-4 prefix-cache and token-cost optimizations."""

from __future__ import annotations

import base64
import json
import os
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from astrbot.core.agent.message import ImageURLPart, TextPart
from astrbot.core.provider.entites import ProviderRequest
from astrbot_plugin_visual_media import character_check
from astrbot_plugin_visual_media.core.events.meme_sender_engine import MemeSenderEngine
from astrbot_plugin_visual_media.core.processing.natural_emotion_analyzer import (
    NaturalEmotionAnalyzer,
)
from astrbot_plugin_visual_media.core.tools import joint_prefetch, joint_reply
from astrbot_plugin_visual_media.core.tools.joint_reply import (
    JOINT_REPLY_KEY,
    JointMemePlan,
    _preview_data_url,
    attach_temporary_candidates,
    clear_preview_cache,
    prepare_joint_reply,
    should_attach_pixel_previews,
)
from astrbot_plugin_visual_media.core.tools.media_policy import (
    selection_prompt,
    turn_media_context,
)
from astrbot_plugin_visual_media.tl import background_notify as notify
from test_media_behavior import event as make_event
from test_media_policy import plugin as make_plugin
from test_media_policy import tools as make_tools


def _save_test_image(path: Path, size=(640, 640), color=(40, 120, 220, 200)) -> str:
    img = Image.new("RGBA", size, color)
    img.save(path, format="PNG")
    return str(path)


def _removable_tools(*names):
    tool_set = make_tools(*names)
    tool_set.remove_tool = lambda name: setattr(
        tool_set, "tools", [t for t in tool_set.tools if t.name != name]
    )
    return tool_set


# ---------------------------------------------------------------------------
# VM-1: Zero system_prompt volatility + temp turn context + static flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reply_modes", [False, True])
@pytest.mark.parametrize("poke_supported", [False, True])
def test_vm1_selection_prompt_is_byte_identical_across_counts_and_tools(
    reply_modes, poke_supported
):
    p0 = selection_prompt(
        0,
        make_tools(),
        reply_modes=reply_modes,
        poke_supported=poke_supported,
    )
    p3 = selection_prompt(
        3,
        make_tools("gemini_image_generation", "generate_character_image"),
        reply_modes=reply_modes,
        poke_supported=poke_supported,
    )
    p5 = selection_prompt(
        5,
        make_tools("gemini_image_batch"),
        reply_modes=reply_modes,
        poke_supported=poke_supported,
    )
    assert p0 == p3 == p5
    assert "本轮有 3 张" not in p3
    assert "本轮有 5 张" not in p5
    assert "本轮可见的生图工具：" not in p3


@pytest.mark.asyncio
async def test_vm1_prepare_joint_reply_keeps_system_prompt_static_and_tools_consistent(
    tmp_path, monkeypatch
):
    img_paths = [
        _save_test_image(tmp_path / f"meme_{i}.png", size=(400, 400)) for i in range(5)
    ]
    fake = make_plugin()
    monkeypatch.setattr(joint_prefetch, "start_joint_prefetch", lambda p, e: None)

    sys_prompts = []
    contexts = []
    for cand_count, gen_tools in [
        (0, ()),
        (3, ("gemini_image_generation",)),
        (5, ("gemini_image_generation", "generate_character_image")),
    ]:
        ev = make_event("哈哈哈哈笑死我了")
        ev._cd_joint_meme_supported_v1 = True
        ev._cd_reply_modes_v1 = True
        ev._cd_reply_poke_supported_v1 = True
        ev._cd_original_user_text_v1 = "哈哈哈哈笑死我了"
        ev._stealer_joint_meme_plan_v1 = JointMemePlan(
            fake,
            ev,
            [
                {"path": img_paths[i], "desc": f"表情{i+1}", "emotion": "happy"}
                for i in range(cand_count)
            ],
        )
        tset = _removable_tools("search_meme", "send_meme", *gen_tools)
        req = NS(system_prompt="base-persona", extra_user_content_parts=[], func_tool=tset)
        assert await prepare_joint_reply(fake, ev, req)
        sys_prompts.append(req.system_prompt)
        contexts.append(req.extra_user_content_parts)

        # Flags set consistently
        assert ev.get_extra(JOINT_REPLY_KEY) is True
        assert ev.get_extra("visual_media_policy_v1") is True
        assert ev.get_extra("_visual_media_static_policy_v1") is True

        # search_meme & send_meme always removed even when candidate_count == 0
        remaining_names = [t.name for t in req.func_tool.tools]
        assert "search_meme" not in remaining_names
        assert "send_meme" not in remaining_names
        assert remaining_names == list(gen_tools)

        # All extra parts marked temporary
        assert req.extra_user_content_parts
        assert all(
            getattr(part, "_no_save", False) is True
            for part in req.extra_user_content_parts
        )

    # Session-level system_prompt is 100% byte-identical across 0, 3, and 5 candidates
    assert sys_prompts[0] == sys_prompts[1] == sys_prompts[2]

    # Dynamic candidate count & generation tool summary moved to temp extra_user_content_parts
    assert "本轮没有可用现成表情候选" in contexts[0][0].text
    assert "本轮未直接暴露生图工具" in contexts[0][0].text
    assert "本轮有 3 张现成表情候选" in contexts[1][0].text
    assert "本轮通过会话筛选的生图工具：gemini_image_generation" in contexts[1][0].text
    assert "本轮有 5 张现成表情候选" in contexts[2][0].text
    assert (
        "本轮通过会话筛选的生图工具：gemini_image_generation、generate_character_image"
        in contexts[2][0].text
    )


def test_vm1_turn_media_context_covers_legacy_plain_text_reminder():
    text = turn_media_context(0, make_tools("gemini_image_generation"), reply_modes=False)
    assert "直接用普通文本" in text
    assert "本轮通过会话筛选的生图工具：gemini_image_generation" in text


@pytest.mark.asyncio
async def test_gpt_cost1_preserved_visual_reply_policy_handoff_and_toctou_validation(
    tmp_path, monkeypatch
):
    from astrbot_plugin_visual_media.core.tools.joint_preview import (
        JointPreviewCache,
        is_preview_source_current,
    )
    from astrbot_plugin_visual_media.core.tools.media_policy import MEDIA_POLICY

    img1 = Path(_save_test_image(tmp_path / "c1.png", size=(400, 400)))
    img2 = Path(_save_test_image(tmp_path / "c2.png", size=(400, 400)))
    fake = make_plugin()
    monkeypatch.setattr(joint_prefetch, "start_joint_prefetch", lambda p, e: None)

    ev = make_event("哈哈哈哈笑死我了")
    ev._cd_joint_meme_supported_v1 = True
    ev._cd_reply_modes_v1 = True
    ev._cd_visual_reply_policy_supported_v1 = True
    ev._cd_original_user_text_v1 = "哈哈哈哈笑死我了"
    ev._stealer_joint_meme_plan_v1 = JointMemePlan(
        fake,
        ev,
        [
            {"path": str(img1), "desc": "猫1"},
            {"path": str(img2), "desc": "猫2"},
        ],
    )
    req = NS(
        system_prompt="host-system",
        extra_user_content_parts=[],
        func_tool=_removable_tools("search_meme", "send_meme", "gemini_image_generation"),
    )
    assert await prepare_joint_reply(fake, ev, req)

    # Under _cd_visual_reply_policy_supported_v1, visual_media hands off static MEDIA_POLICY
    # + render_state callback without mutating req.system_prompt or duplicating turn context
    assert req.system_prompt == "host-system"
    assert ev.get_extra("_visual_media_static_policy_v1") is True
    policy_plan, media_policy, render_state = req._cd_visual_reply_policy_v1
    assert policy_plan is ev._stealer_joint_meme_plan_v1
    assert media_policy == MEDIA_POLICY
    assert isinstance(fake._joint_meme_preview_cache, JointPreviewCache)
    assert req._stealer_joint_meme_ids_v1 == (1, 2)
    rendered = render_state(req._stealer_joint_meme_ids_v1, req.func_tool)
    assert "编号为 1、2" in rendered
    assert "gemini_image_generation" in rendered

    # Previews rendered via JointPreviewCache are shrunk WebP/JPEG (<=320px)
    img_parts = [p for p in req.extra_user_content_parts if isinstance(p, ImageURLPart)]
    assert len(img_parts) == 2
    assert img_parts[0].image_url.url.startswith(
        ("data:image/webp;base64,", "data:image/jpeg;base64,")
    )

    # Replacing img2 invalidates its source_version; current_ids() strips candidate 2
    # and JointMemePlan.resolve() rejects unoffered candidate 2.
    ver2 = ev._stealer_joint_meme_plan_v1.candidates[1]["_joint_preview_source_version"]
    assert is_preview_source_current(str(img2), ver2)
    _save_test_image(img2, size=(120, 120), color=(255, 0, 0, 255))
    assert not is_preview_source_current(str(img2), ver2)
    remaining_ids = req._stealer_joint_meme_current_ids_v1()
    assert remaining_ids == (1,)
    with pytest.raises(ValueError, match="invalid_joint_meme_candidate"):
        ev._stealer_joint_meme_plan_v1.resolve(
            '{"reply_mode":"meme_only","text":"","meme_id":2}'
        )


def test_gpt_cost1_preserved_empty_catalogue_cache_and_library_version(tmp_path):
    from astrbot_plugin_visual_media.core.db.database_service import DatabaseService
    from astrbot_plugin_visual_media.core.tools.joint_prefetch import EmptyCatalogueCache

    db = DatabaseService(tmp_path / "emoji.db")
    snap, version = db.get_library_snapshot()
    assert snap == {}
    assert len(version) == 3
    assert db.get_library_version() == version

    cache = EmptyCatalogueCache(ttl_seconds=30, max_entries=4)
    fake_plugin = NS(
        plugin_config=NS(get_event_target=lambda e: ("group", "100")),
    )
    ev = NS(
        get_platform_id=lambda: "aiocqhttp",
        get_self_id=lambda: "999",
        get_sender_id=lambda: "123",
        unified_msg_origin="aiocqhttp:GroupMessage:100",
    )
    key = cache.key(fake_plugin, ev, "hello")
    assert key is not None
    assert cache.check(key, version) == "cold"
    cache.remember(key, version)
    assert cache.check(key, version) == "hit"
    assert cache.check(key, ("other", version[1], version[2])) == "version_changed"


# ---------------------------------------------------------------------------
# VM-2: Speaker visual preferences moved out of req.system_prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vm2_speaker_preferences_move_to_temp_user_parts_and_keep_system_prompt_static(
    owner,
):
    owner.memes._prepare_joint_meme_reply = AsyncMock()

    sys_prompts = []
    pref_texts = []
    for sender, text in [
        ("user_1", "你好呀"),
        ("user_2", "以后少发表情"),
        ("user_3", "我喜欢水彩画风"),
        ("user_4", "以后不要再发表情包了"),
    ]:
        ev = make_event(text, sender=sender)
        req = NS(system_prompt="base-system", extra_user_content_parts=[])
        await owner._prepare_joint_meme_reply(ev, req)
        sys_prompts.append(req.system_prompt)
        assert len(req.extra_user_content_parts) == 1
        part = req.extra_user_content_parts[0]
        assert isinstance(part, TextPart)
        assert getattr(part, "_no_save", False) is True
        pref_texts.append(part.text)

    # system_prompt never mutates across speaker switches or preference saves
    assert all(p == "base-system" for p in sys_prompts)
    assert "自然选择" in pref_texts[0]
    assert "少用表情" in pref_texts[1]
    assert "本轮明确偏好已保存" in pref_texts[1]
    assert "水彩" in pref_texts[2]
    assert "不主动发表情" in pref_texts[3]


@pytest.mark.asyncio
async def test_vm2_fallback_to_system_prompt_when_extra_user_content_parts_missing(
    owner,
):
    owner.memes._prepare_joint_meme_reply = AsyncMock()
    ev = make_event("你好呀", sender="legacy_user")
    req = NS(system_prompt="legacy-system")  # no extra_user_content_parts attribute
    await owner._prepare_joint_meme_reply(ev, req)
    assert "当前成员的视觉表达偏好" in req.system_prompt


# ---------------------------------------------------------------------------
# VM-3: Shrunk WebP/JPEG preview, LRU cache, max count (default 3), gating
# ---------------------------------------------------------------------------


def test_vm3_preview_shrink_format_fallback_and_lru_cache(tmp_path, monkeypatch):
    import random

    clear_preview_cache()
    img_path = Path(tmp_path / "big.png")
    # Photo-like non-repetitive texture so size comparison reflects real meme PNGs
    rng = random.Random(42)
    raw_pixels = bytes(rng.getrandbits(8) for _ in range(1024 * 1024 * 4))
    big = Image.frombytes("RGBA", (1024, 1024), raw_pixels)
    big.save(img_path, format="PNG")

    open_calls = 0
    real_open = Image.open

    def counting_open(*args, **kwargs):
        nonlocal open_calls
        open_calls += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(Image, "open", counting_open)

    url1 = _preview_data_url(str(img_path))
    assert url1.startswith(("data:image/webp;base64,", "data:image/jpeg;base64,"))
    assert open_calls == 1

    # Decoded dimensions bounded to 320x320
    raw_bytes = base64.b64decode(url1.split(",", 1)[1])
    with real_open(BytesIO(raw_bytes)) as decoded:
        assert max(decoded.size) <= 320

    # Significantly smaller than 768x768 PNG
    png768_url = _preview_data_url(str(img_path), max_size=768, fmt="png")
    assert len(url1) < len(png768_url) * 0.4
    assert open_calls == 2

    # Second call with same args hits LRU cache (no PIL.Image.open)
    url2 = _preview_data_url(str(img_path))
    assert url2 == url1
    assert open_calls == 2

    # Changing mtime invalidates cache
    st = img_path.stat()
    os.utime(img_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    url3 = _preview_data_url(str(img_path))
    assert url3 == url1
    assert open_calls == 3

    # WebP unavailable automatically falls back to JPEG
    monkeypatch.setattr(joint_reply, "_webp_supported", lambda: False)
    jpeg_fallback = _preview_data_url(str(img_path), fmt="webp")
    assert jpeg_fallback.startswith("data:image/jpeg;base64,")
    raw_jpeg = base64.b64decode(jpeg_fallback.split(",", 1)[1])
    with real_open(BytesIO(raw_jpeg)) as decoded_jpeg:
        assert decoded_jpeg.format == "JPEG"
        assert max(decoded_jpeg.size) <= 320


def test_vm3_preview_lru_cache_is_bounded(tmp_path, monkeypatch):
    clear_preview_cache()
    monkeypatch.setattr(joint_reply, "_PREVIEW_CACHE_MAX_ITEMS", 3)
    paths = [_save_test_image(tmp_path / f"img_{i}.png", size=(64, 64)) for i in range(5)]
    for p in paths:
        _preview_data_url(p)
    assert len(joint_reply._PREVIEW_CACHE) == 3
    clear_preview_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_max,expected_images", [(None, 3), (2, 2), (5, 5)])
async def test_vm3_max_attached_previews_and_metadata_retention(
    tmp_path, configured_max, expected_images
):
    clear_preview_cache()
    paths = [
        _save_test_image(tmp_path / f"cand_{i}.png", size=(400, 400)) for i in range(5)
    ]
    fake = make_plugin()
    if configured_max is not None:
        fake.plugin_config.joint_reply_max_preview_images = configured_max

    ev = make_event("哈哈哈哈笑死我了")
    ev._cd_original_user_text_v1 = "哈哈哈哈笑死我了"
    candidates = [
        {"path": paths[i], "desc": f"搞笑猫猫{i+1}", "emotion": "happy"}
        for i in range(5)
    ]
    plan = JointMemePlan(fake, ev, candidates)
    req = NS(extra_user_content_parts=[], func_tool=make_tools())
    await attach_temporary_candidates(plan, req)

    img_parts = [p for p in req.extra_user_content_parts if isinstance(p, ImageURLPart)]
    meta_parts = [
        p
        for p in req.extra_user_content_parts
        if isinstance(p, TextPart) and p.text.startswith("[本轮表情候选资料]")
    ]
    assert len(img_parts) == expected_images
    assert len(meta_parts) == 5
    # Candidate #5 is still selectable even when pixel preview cap is 2 or 3
    text, chains = plan.resolve('{"media":"meme","text":"笑死","meme_id":5}')
    assert text == "" and len(chains) == 1


@pytest.mark.asyncio
async def test_vm3_context_gating_skips_pixels_on_technical_long_and_off_pref_turns(
    tmp_path,
):
    clear_preview_cache()
    paths = [_save_test_image(tmp_path / f"g_{i}.png", size=(300, 300)) for i in range(3)]
    candidates = [{"path": p, "desc": "猫猫点头", "emotion": "happy"} for p in paths]

    # Technical / code / long turns -> 0 pixel previews, text metadata preserved
    for query in [
        "帮我写一个 Python 异步重试装饰器并处理超时异常",
        "看看这段报错:\n```python\nTraceback (most recent call last):\nValueError: bad\n```",
        "请详细解释一下操作系统中虚拟内存页表转换与多级页表设计原理，以及 TLB 命中率对性能的影响。" * 2,
        "线上数据库故障宕机了怎么办",
    ]:
        fake = make_plugin()
        ev = make_event(query)
        ev._cd_original_user_text_v1 = query
        plan = JointMemePlan(fake, ev, candidates)
        req = NS(extra_user_content_parts=[], func_tool=make_tools())
        await attach_temporary_candidates(plan, req)
        img_parts = [p for p in req.extra_user_content_parts if isinstance(p, ImageURLPart)]
        meta_parts = [
            p
            for p in req.extra_user_content_parts
            if isinstance(p, TextPart) and p.text.startswith("[本轮表情候选资料]")
        ]
        assert len(img_parts) == 0, f"expected 0 pixels for: {query}"
        assert len(meta_parts) == 3
        assert "仅提供文字元数据" in req.extra_user_content_parts[0].text

    # Playful / explicit-meme / chat_dynamics playful hint -> attaches pixels
    for query, extras in [
        ("哈哈哈哈太离谱了", {}),
        ("给我来张表情包", {}),
        ("早安呀～", {}),
        ("这是一段普通中性文本用于测试外部提示是否放行预览上传的逻辑", {"_cd_playful_turn_v1": True}),
    ]:
        fake = make_plugin()
        ev = make_event(query)
        ev._cd_original_user_text_v1 = query
        for k, v in extras.items():
            ev.set_extra(k, v)
        plan = JointMemePlan(fake, ev, candidates)
        req = NS(extra_user_content_parts=[], func_tool=make_tools())
        await attach_temporary_candidates(plan, req)
        img_parts = [p for p in req.extra_user_content_parts if isinstance(p, ImageURLPart)]
        assert len(img_parts) == 3, f"expected 3 pixels for: {query}"

    # Member preference "off" skips pixels even on playful chat (unless explicit meme request)
    fake = make_plugin()
    ev_off = make_event("哈哈哈哈")
    ev_off._cd_original_user_text_v1 = "哈哈哈哈"
    ev_off._visual_media_member_prefs_v1 = {"frequency": "off", "style": "", "blocked": []}
    plan_off = JointMemePlan(fake, ev_off, candidates)
    allowed, reason = should_attach_pixel_previews(plan_off, ev_off)
    assert allowed is False and reason == "member_prefers_text"

    # Toggleable: disabling context gate attaches pixels even on technical turns
    fake_un = make_plugin()
    fake_un.plugin_config.joint_reply_preview_context_gate = False
    ev_tech = make_event("帮我写一个 Python 装饰器")
    ev_tech._cd_original_user_text_v1 = "帮我写一个 Python 装饰器"
    plan_tech = JointMemePlan(fake_un, ev_tech, candidates)
    req_tech = NS(extra_user_content_parts=[], func_tool=make_tools())
    await attach_temporary_candidates(plan_tech, req_tech)
    assert sum(1 for p in req_tech.extra_user_content_parts if isinstance(p, ImageURLPart)) == 3


def test_vm3_conf_schema_defines_preview_options():
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    meme_items = schema["memes"]["items"]
    assert meme_items["joint_reply_max_preview_images"]["default"] == 3
    assert meme_items["joint_reply_preview_max_size"]["default"] == 320
    assert meme_items["joint_reply_preview_format"]["default"] == "webp"
    assert meme_items["joint_reply_preview_quality"]["default"] == 75
    assert meme_items["joint_reply_preview_context_gate"]["default"] is True


# ---------------------------------------------------------------------------
# VM-4: Emotion dedup short-circuit + static system_prompt splits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vm4_joint_reply_short_circuits_passive_emotion_analysis(owner):
    ev = make_event("哈哈")
    ev.set_extra(JOINT_REPLY_KEY, True)

    owner.memes._prepare_emoji_after_llm = AsyncMock()
    owner.memes._prepare_emoji_response = AsyncMock()
    await owner._prepare_emoji_after_llm(ev, NS(completion_text="哈哈真好玩"))
    await owner._prepare_emoji_response(ev)
    assert not owner.memes._prepare_emoji_after_llm.called
    assert not owner.memes._prepare_emoji_response.called

    # Direct analyzer & sender engine entrypoints also short-circuit with 0 LLM calls
    llm_mock = AsyncMock()
    owner.context.llm_generate = llm_mock
    res = await owner.memes.emotion_analyzer.analyze_for_reply(
        ev, "这个回复没有任何本地关键词触发", user_message="普通消息"
    )
    assert res is None
    assert llm_mock.await_count == 0

    engine = MemeSenderEngine(owner.memes)
    owner.memes.emotion_analyzer.analyze_for_reply = AsyncMock()
    await engine.async_analyze_and_send_emoji(ev, "测试回复", [], user_message="测试")
    assert not owner.memes.emotion_analyzer.analyze_for_reply.called


@pytest.mark.asyncio
async def test_vm4_natural_emotion_analyzer_splits_static_system_prompt():
    calls = []

    async def fake_llm_generate(**kwargs):
        calls.append(kwargs)
        return NS(completion_text='{"query":"放松聊天","emotions":["happy"]}')

    cfg = NS(
        get_categories=lambda: ["happy", "sad", "tired"],
        category_info={
            "happy": {"name": "开心", "desc": "快乐愉悦"},
            "sad": {"name": "难过", "desc": "低落失落"},
            "tired": {"name": "困倦", "desc": "疲惫想躺"},
        },
        emotion_analysis_prompt="",
        emotion_analysis_provider_id="fast-model",
        enable_natural_emotion_analysis=True,
        get_keyword_map=lambda: {},
        normalize_category_strict=lambda x: x if x in {"happy", "sad", "tired"} else None,
    )
    plugin = NS(plugin_config=cfg, context=NS(llm_generate=fake_llm_generate))
    analyzer = NaturalEmotionAnalyzer(plugin)

    ev1 = make_event("今晚加班到十点", sender="u1")
    ev2 = make_event("明天放假出去玩", sender="u2")
    r1 = await analyzer.analyze_for_reply(ev1, "注意休息别太累了", user_message="今晚加班到十点")
    r2 = await analyzer.analyze_for_reply(ev2, "记得多拍几张照片", user_message="明天放假出去玩")

    assert r1 is not None and r2 is not None
    assert len(calls) == 2
    assert calls[0]["system_prompt"] == calls[1]["system_prompt"]
    assert "happy(快乐愉悦)" in calls[0]["system_prompt"]
    assert "今晚加班" not in calls[0]["system_prompt"]
    assert "明天放假" not in calls[1]["system_prompt"]
    assert "今晚加班到十点" in calls[0]["prompt"]
    assert "注意休息别太累了" in calls[0]["prompt"]
    assert "明天放假出去玩" in calls[1]["prompt"]


@pytest.mark.asyncio
async def test_vm4_character_check_splits_static_system_prompt(owner, tmp_path, monkeypatch):
    from astrbot_plugin_visual_media.core.processing.vlm_call_service import (
        VLMCallService,
    )

    async def provider(*args):
        return "vision-test"

    monkeypatch.setattr(VLMCallService, "_resolve_vision_provider", provider)
    ref1 = _save_test_image(tmp_path / "ref1.png", size=(32, 32))
    ref2 = _save_test_image(tmp_path / "ref2.png", size=(32, 32))
    out1 = _save_test_image(tmp_path / "out1.png", size=(32, 32))
    out2 = _save_test_image(tmp_path / "out2.png", size=(32, 32))

    calls = []

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        return NS(completion_text='{"status":"pass","issues":[],"reason":"一致"}')

    owner.context.llm_generate = fake_generate
    res1 = await character_check.check_character(
        owner, {"name": "安安", "appearance": "蓝发"}, [out1], [ref1], "微笑"
    )
    res2 = await character_check.check_character(
        owner, {"name": "小雅", "appearance": "黑长直"}, [out1, out2], [ref1, ref2], "挥手"
    )
    assert res1["status"] == "pass" and res2["status"] == "pass"
    assert len(calls) == 2
    assert (
        calls[0]["system_prompt"]
        == calls[1]["system_prompt"]
        == character_check.CHARACTER_CHECK_SYSTEM_PROMPT
    )
    assert "前 1 张" in calls[0]["prompt"] and "其余 1 张" in calls[0]["prompt"]
    assert "前 2 张" in calls[1]["prompt"] and "其余 2 张" in calls[1]["prompt"]
    assert "安安" not in calls[0]["system_prompt"]
    assert "小雅" not in calls[1]["system_prompt"]


@pytest.mark.asyncio
async def test_vm4_background_notify_keeps_system_prompt_static_and_uses_temp_user_part(
    monkeypatch,
):
    captured_reqs = []

    class CronEvent:
        def __init__(self, *, context, **kwargs):
            self._has_send_oper = False

    async def fake_conv(*, event, plugin_context):
        return NS(history="[]")

    async def fake_build(*, event, plugin_context, req, **kwargs):
        captured_reqs.append(req)

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
    monkeypatch.setattr(
        notify,
        "MessageSession",
        NS(from_str=lambda umo: NS(message_type="group")),
    )
    monkeypatch.setattr(notify, "CronMessageEvent", CronEvent)
    monkeypatch.setattr(notify, "MainAgentBuildConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(notify, "get_session_tool_timeout", lambda *args: 60)
    monkeypatch.setattr(notify, "_get_session_conv", fake_conv)
    monkeypatch.setattr(notify, "build_main_agent", fake_build)
    monkeypatch.setattr(notify, "ProviderRequest", ProviderRequest)
    monkeypatch.setattr(notify, "ToolSet", lambda: NS(add_tool=lambda tool: None))
    monkeypatch.setattr(notify, "persist_agent_history", AsyncMock())
    monkeypatch.setattr(notify, "_bump_delivery_count", AsyncMock())

    plugin = NS(context=host)
    ev = NS(unified_msg_origin="qq:GroupMessage:1", role="member")

    assert await notify.notify_llm_background_result(
        plugin,
        ev,
        {"task_id": "job-alpha-001", "image_urls": ["https://example.invalid/a.png"]},
        notice="done A",
        scene="test",
    )
    assert await notify.notify_llm_background_result(
        plugin,
        ev,
        {"task_id": "job-beta-002", "image_urls": ["https://example.invalid/b.png"]},
        notice="done B",
        scene="test",
    )

    assert len(captured_reqs) == 2
    req_a, req_b = captured_reqs
    # system_prompt stays 100% static across distinct background jobs
    assert req_a.system_prompt == req_b.system_prompt
    assert "job-alpha-001" not in req_a.system_prompt
    assert "job-beta-002" not in req_b.system_prompt

    # Unique per-job payload lives in temporary extra_user_content_parts
    assert len(req_a.extra_user_content_parts) == 1
    assert len(req_b.extra_user_content_parts) == 1
    part_a = req_a.extra_user_content_parts[0]
    part_b = req_b.extra_user_content_parts[0]
    assert isinstance(part_a, TextPart) and getattr(part_a, "_no_save", False) is True
    assert isinstance(part_b, TextPart) and getattr(part_b, "_no_save", False) is True
    assert "job-alpha-001" in part_a.text
    assert "job-beta-002" in part_b.text
