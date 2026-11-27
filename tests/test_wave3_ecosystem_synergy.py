"""Wave 3 tests: vibe_transition event bus suppression, multi-style series artwork & meme evolution API."""

from __future__ import annotations

import ast
import asyncio
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from PIL import Image as PILImage

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


def _ensure_minimal_astrbot_stubs() -> None:
    api_mod = sys.modules.get("astrbot.api")
    if api_mod is not None:
        if not hasattr(api_mod, "__path__"):
            api_mod.__path__ = []
        if not hasattr(api_mod, "AstrBotConfig"):
            api_mod.AstrBotConfig = dict
    if "astrbot.api.event" not in sys.modules:
        ev_mod = types.ModuleType("astrbot.api.event")
        ev_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
        ev_mod.MessageChain = type("MessageChain", (), {})
        sys.modules["astrbot.api.event"] = ev_mod
    if "astrbot.api.message_components" not in sys.modules:
        comp_mod = types.ModuleType("astrbot.api.message_components")
        comp_mod.Image = type("Image", (), {})
        comp_mod.Plain = type("Plain", (), {})
        sys.modules["astrbot.api.message_components"] = comp_mod
    if "astrbot.api.star" not in sys.modules:
        star_mod = types.ModuleType("astrbot.api.star")
        star_mod.Context = type("Context", (), {})
        star_mod.Star = type("Star", (), {})
        star_mod.StarTools = SimpleNamespace(
            get_data_dir=lambda name: ROOT / ".tmp_test_data" / name
        )
        sys.modules["astrbot.api.star"] = star_mod


_ensure_minimal_astrbot_stubs()

from astrbot_plugin_visual_media.core.events.meme_sender_engine import MemeSenderEngine
from astrbot_plugin_visual_media.core.events.vibe_bus import (
    VibeEventBusBridge,
    VibeStateCache,
    is_vibe_serious_for_event,
)
from astrbot_plugin_visual_media.core.processing.series_artwork_service import (
    SERIES_ARTWORK_STYLES,
    build_series_artwork_prompt,
    render_fallback_artwork_card,
)
from astrbot_plugin_visual_media.core.tools.joint_reply import (
    prepare_joint_reply,
    should_attach_pixel_previews,
)
from astrbot_plugin_visual_media.media_behavior import MediaBehavior


def _load_visual_media_plugin_class():
    source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    cls_node = next(
        n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "VisualMediaPlugin"
    )
    keep = {
        "_extract_group_id_from_umo",
        "_origin_matches_scope",
        "_is_excluded_by_core",
        "_safe_meme_index",
        "_safe_character_profiles",
        "_artwork_fallback_dir",
        "generate_series_artwork",
        "get_group_top_memes",
    }
    cls_node.bases = []
    cls_node.body = [
        n
        for n in cls_node.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in keep
    ]
    for method in cls_node.body:
        method.decorator_list = [
            dec
            for dec in method.decorator_list
            if isinstance(dec, ast.Name) and dec.id in {"staticmethod", "classmethod"}
        ]
    mod = ast.Module(body=[cls_node], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {
        "__name__": "astrbot_plugin_visual_media.main",
        "__package__": "astrbot_plugin_visual_media",
        "Any": object,
        "Path": Path,
        "SimpleNamespace": SimpleNamespace,
        "asyncio": asyncio,
        "datetime": datetime,
        "timezone": timezone,
    }
    exec(compile(mod, str(ROOT / "main.py"), "exec"), ns)
    return ns["VisualMediaPlugin"]


VisualMediaPlugin = _load_visual_media_plugin_class()


class _FakeEvent:
    def __init__(self, *, umo="aiocqhttp:GroupMessage:1001", sender_id="u1", text="大家先认真讨论下方案"):
        self.unified_msg_origin = umo
        self._sender_id = sender_id
        self.message_str = text
        self._cd_original_user_text_v1 = text
        self._cd_joint_meme_supported_v1 = True
        self._extras: dict = {}

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        parts = self.unified_msg_origin.split(":", 2)
        return parts[2] if len(parts) == 3 else ""

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


# ── 1. Vibe Event Bus Subscription, TTL Expiry & Safe Unsubscribe ────────


def test_vibe_bus_subscribe_transition_ttl_and_unsubscribe():
    subs: dict[str, tuple[str, object]] = {}

    class _FakeChatDynamics:
        def subscribe_dynamics_event(self, event_type, callback, *, owner=""):
            token = f"tok_{len(subs) + 1}"
            subs[token] = (event_type, callback)
            return token

        def unsubscribe_dynamics_event(self, token):
            return subs.pop(token, None) is not None

        def emit(self, event_type, payload):
            for et, cb in list(subs.values()):
                if et == event_type:
                    cb(payload)

    cd = _FakeChatDynamics()
    owner = SimpleNamespace(
        context=SimpleNamespace(
            get_registered_star=lambda name: SimpleNamespace(star_cls=cd)
            if name == "astrbot_plugin_chat_dynamics"
            else None
        )
    )

    bridge = VibeEventBusBridge(owner, default_ttl=120.0)
    assert bridge.subscribe() is True
    assert len(subs) == 1
    # Idempotent second subscribe
    assert bridge.subscribe() is True
    assert len(subs) == 1

    umo = "aiocqhttp:GroupMessage:9001"
    cd.emit("vibe_transition", {"umo": umo, "to_vibe": "serious_inquiry"})
    serious, vibe = bridge.cache.is_serious(umo)
    assert serious is True and vibe == "serious_inquiry"

    # Also recognizes deciding
    cd.emit("vibe_transition", {"umo": umo, "vibe": "deciding", "ttl_seconds": 30})
    serious, vibe = bridge.cache.is_serious(umo)
    assert serious is True and vibe == "deciding"

    # TTL expiry automatically lifts suppression so groups never get stuck
    now_future = bridge.cache.snapshot()[umo]["expires_at"] + 1.0
    serious_after_ttl, _ = bridge.cache.is_serious(umo, now=now_future)
    assert serious_after_ttl is False

    # Relaxed vibe immediately clears serious mode
    cd.emit("vibe_transition", {"umo": umo, "vibe": "serious_inquiry"})
    assert bridge.cache.is_serious(umo)[0] is True
    cd.emit("vibe_transition", {"umo": umo, "vibe": "playful"})
    assert bridge.cache.is_serious(umo)[0] is False

    # Unsubscribe cleans up cleanly
    assert bridge.unsubscribe() is True
    assert len(subs) == 0


# ── 2. Serious Vibe Suppresses Memes in joint_reply & MemeSenderEngine ───


def test_vibe_serious_suppresses_joint_reply_and_logs_behavior(tmp_path, monkeypatch):
    # Ensure TextPart/ImageURLPart exist even when astrbot.core is stubbed
    if "astrbot.core.agent.message" not in sys.modules:
        msg_mod = types.ModuleType("astrbot.core.agent.message")

        class _Part:
            def __init__(self, **kw):
                self.__dict__.update(kw)

            def mark_as_temp(self):
                return self

        class _ImgPart(_Part):
            class ImageURL:
                def __init__(self, url=""):
                    self.url = url

        msg_mod.TextPart = _Part
        msg_mod.ImageURLPart = _ImgPart
        monkeypatch.setitem(sys.modules, "astrbot.core.agent.message", msg_mod)

    vibe_cache = VibeStateCache(default_ttl=300.0)
    umo = "aiocqhttp:GroupMessage:3003"
    vibe_cache.update_from_payload({"umo": umo, "to_vibe": "serious_inquiry"})

    behavior = MediaBehavior(tmp_path)
    plugin = SimpleNamespace(
        plugin_config=SimpleNamespace(llm_joint_reply=True, auto_send_meme=True),
        auto_send_meme=True,
        meme_chance=1.0,
        vibe_state=vibe_cache,
        media_behavior=behavior,
        is_send_enabled_for_event=lambda ev: True,
        _emoji_sender_engine=None,
    )
    engine = MemeSenderEngine(plugin)
    plugin._emoji_sender_engine = engine

    ev = _FakeEvent(umo=umo, sender_id="u_42", text="哈哈这个方案怎么定")
    # 1) should_attach_pixel_previews gates out under vibe_serious
    plan_stub = SimpleNamespace(plugin=plugin, event=ev)
    attach, gate_reason = should_attach_pixel_previews(plan_stub, ev)
    assert attach is False and gate_reason == "vibe_serious"

    # 2) prepare_joint_reply suppresses candidates and logs suppressed_by=vibe_serious
    req = SimpleNamespace(extra_user_content_parts=[], system_prompt="", func_tool=None)
    ok = asyncio.run(prepare_joint_reply(plugin, ev, req))
    assert ok is True
    assert ev._stealer_joint_meme_plan_v1.candidates == []

    recs = behavior.records(limit=20)
    suppressed_recs = [
        r for r in recs if r["reason"] == "vibe_serious" and r["details"].get("suppressed_by") == "vibe_serious"
    ]
    assert len(suppressed_recs) >= 1

    # 3) Passive MemeSenderEngine also blocks auto meme under vibe_serious
    ev2 = _FakeEvent(umo=umo, sender_id="u_42", text="同意先讨论风险")
    allowed = asyncio.run(engine.resolve_auto_emoji_turn_permission(ev2))
    assert allowed is False
    assert ev2.get_extra("stealer_auto_emoji_turn_reason") == "vibe_serious"

    # 4) Once group transitions back to casual/playful, suppression lifts immediately
    vibe_cache.update_from_payload({"umo": umo, "to_vibe": "casual"})
    assert is_vibe_serious_for_event(plugin, ev2) == (False, "")
    ev3 = _FakeEvent(umo=umo, sender_id="u_42", text="哈哈太好玩了")
    assert asyncio.run(engine.resolve_auto_emoji_turn_permission(ev3)) is True


# ── 3. Multi-Style generate_series_artwork & Pillow Fallback Cards ───────


def test_generate_series_artwork_four_styles_and_pillow_fallback(tmp_path):
    assert {"polaroid", "comic_strip", "pictionary", "meme_remix"}.issubset(
        SERIES_ARTWORK_STYLES.keys()
    )

    # Prompt templates contain style-specific constraints (e.g. no-text rule for pictionary)
    p_pic, key_pic = build_series_artwork_prompt("长颈鹿喝奶茶", style="pictionary")
    assert key_pic == "pictionary"
    assert "严格禁止" in p_pic and "文字" in p_pic

    p_comic, key_comic = build_series_artwork_prompt("猫咪接龙大冒险", style="comic_strip")
    assert key_comic == "comic_strip" and "连环画" in p_comic

    p_remix, key_remix = build_series_artwork_prompt("加班摸鱼王", style="meme_remix")
    assert key_remix == "meme_remix" and "热梗" in p_remix

    # When no image provider is configured OR rate-limit raises -> Pillow commemorative card fallback (ok=True, fallback=True)
    plugin = VisualMediaPlugin.__new__(VisualMediaPlugin)
    plugin._closed = False
    plugin.context = None
    plugin.library = SimpleNamespace(root=tmp_path, profiles=lambda: [])
    plugin.memes = None
    plugin._is_excluded_by_core = lambda **kw: False

    # Case A: generation is None (no provider configured)
    plugin.generation = None
    for style_name in ("polaroid", "comic_strip", "pictionary", "meme_remix"):
        res = asyncio.run(
            plugin.generate_series_artwork(
                "群友一起看流星雨纪念",
                style=style_name,
                persona_id="anan",
                source_plugin="astrbot_plugin_dynamics_games",
            )
        )
        assert res["ok"] is True
        assert res["fallback"] is True
        assert res["style"] == style_name
        card_path = Path(res["image_path"])
        assert card_path.is_file()
        with PILImage.open(card_path) as im:
            assert im.width >= 700 and im.height >= 600

    # Case B: rate limit exception triggers Pillow fallback without throwing
    class _RateLimitedService:
        async def submit(self, **kwargs):
            raise RuntimeError("rate limit exceeded")

    plugin.generation = SimpleNamespace(
        _plugin_data_dir=tmp_path,
        api_client=object(),
        rate_limiter=SimpleNamespace(allows_group=lambda ev: True),
        get_service=lambda api_version=1: _RateLimitedService(),
    )
    res_rl = asyncio.run(
        plugin.generate_series_artwork(
            "年度金句回顾",
            umo="aiocqhttp:GroupMessage:123",
            style="polaroid",
            source_plugin="astrbot_plugin_group_chronicle",
        )
    )
    assert res_rl["ok"] is True
    assert res_rl["fallback"] is True
    assert "rate limit" in res_rl["fallback_reason"]
    assert Path(res_rl["image_path"]).is_file()


# ── 4. Enhanced get_group_top_memes (Semantic/Tag Matching + Thumbnails) ─


def test_get_group_top_memes_semantic_frequency_and_thumbnails(tmp_path):
    img1 = tmp_path / "m1.png"
    img2 = tmp_path / "m2.png"
    PILImage.new("RGB", (400, 400), (255, 220, 180)).save(img1, format="PNG")
    PILImage.new("RGB", (320, 320), (180, 220, 255)).save(img2, format="PNG")

    index = {
        str(img1): {
            "hash": "hm1",
            "category": "tired",
            "desc": "深夜写代码摸鱼的猫咪",
            "overlay_text": "今天也在带薪摸鱼",
            "tags": ["摸鱼", "打工人", "加班"],
            "scenes": ["上班摸鱼", "不想加班"],
            "use_count": 30,
            "scope_mode": "local",
            "origin_target": "umo:aiocqhttp:GroupMessage:500",
        },
        str(img2): {
            "hash": "hm2",
            "category": "happy",
            "desc": "周末快乐撸猫喝咖啡",
            "overlay_text": "周末快乐",
            "tags": ["周末", "放松"],
            "scenes": ["周末休息"],
            "use_count": 10,
            "scope_mode": "public",
            "origin_target": "",
        },
    }

    plugin = VisualMediaPlugin.__new__(VisualMediaPlugin)
    plugin._closed = False
    plugin.context = None
    plugin._is_excluded_by_core = lambda **kw: False
    plugin._safe_meme_index = lambda: index
    plugin.memes = SimpleNamespace(base_dir=tmp_path)

    # Keyword / semantic match finds the摸鱼/打工人 meme with frequency & generated thumbnail
    results = plugin.get_group_top_memes("aiocqhttp:GroupMessage:500", keyword="打工人摸鱼", limit=5)
    assert len(results) >= 1
    top = results[0]
    assert top["hash"] == "hm1"
    assert top["use_count"] == 30
    assert top["frequency"] == 0.75  # 30 / (30 + 10)
    assert top["thumbnail_path"] and Path(top["thumbnail_path"]).is_file()
    with PILImage.open(top["thumbnail_path"]) as thumb:
        assert max(thumb.size) <= 240
