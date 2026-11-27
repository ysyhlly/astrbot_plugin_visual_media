"""Wave 2 comprehensive tests for astrbot_plugin_visual_media."""

from __future__ import annotations

import ast
import asyncio
import json
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


def _ensure_astrbot_event_stub() -> None:
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
        comp_mod.At = type("At", (), {})
        comp_mod.Image = type("Image", (), {})
        comp_mod.Reply = type("Reply", (), {})
        comp_mod.Plain = type("Plain", (), {})
        comp_mod.Node = type("Node", (), {})
        sys.modules["astrbot.api.message_components"] = comp_mod
    if "astrbot.api.star" not in sys.modules:
        star_mod = types.ModuleType("astrbot.api.star")
        star_mod.Context = type("Context", (), {})
        star_mod.Star = type("Star", (), {})
        star_mod.StarTools = SimpleNamespace(get_data_dir=lambda name: ROOT / ".tmp_test_data" / name)
        sys.modules["astrbot.api.star"] = star_mod


_ensure_astrbot_event_stub()

from astrbot_plugin_visual_media.core.commands.crowd_curation import CrowdCurationService
from astrbot_plugin_visual_media.core.commands.make_meme_service import MakeMemeService
from astrbot_plugin_visual_media.core.processing.character_pack_service import (
    CharacterPackService,
)
from astrbot_plugin_visual_media.core.processing.phash_dedup_service import (
    PHashDedupService,
)
from astrbot_plugin_visual_media.core.search.meme_scope_service import MemeScopeService
from astrbot_plugin_visual_media.core.util.normalization import normalize_scope_mode
from astrbot_plugin_visual_media.core.util.platform_capabilities import (
    get_platform_capabilities,
    list_platform_capability_matrix,
)
from astrbot_plugin_visual_media.media_behavior import MediaBehavior
from astrbot_plugin_visual_media.media_library import MediaLibrary
from astrbot_plugin_visual_media.media_settings import MediaSettings


def _load_visual_media_plugin_class():
    """Extract VisualMediaPlugin methods from main.py without touching host/tl stubs."""
    source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    cls_node = next(
        n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "VisualMediaPlugin"
    )
    keep = {
        "_extract_group_id_from_umo",
        "_origin_matches_scope",
        "_is_excluded_by_core",
        "_is_event_excluded_by_core",
        "_resolve_character_key",
        "get_group_top_memes",
        "series_search_items",
        "character_generate_command",
        "visual_help",
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
        "AstrMessageEvent": object,
        "datetime": datetime,
        "timezone": timezone,
    }
    exec(compile(mod, str(ROOT / "main.py"), "exec"), ns)
    return ns["VisualMediaPlugin"]


VisualMediaPlugin = _load_visual_media_plugin_class()


def _make_png(path: Path, size: tuple[int, int] = (300, 300), color=(240, 200, 180)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    PILImage.new("RGB", size, color).save(path, format="PNG")
    return path


class _FakeEvent:
    def __init__(
        self,
        *,
        umo: str = "aiocqhttp:GroupMessage:10001",
        sender_id: str = "u_1",
        sender_name: str = "Alice",
        message_str: str = "",
        messages: list | None = None,
        platform_name: str = "aiocqhttp",
    ):
        self.unified_msg_origin = umo
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.message_str = message_str
        self._messages = list(messages or [])
        self._platform_name = platform_name
        self._extras: dict = {}

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_platform_name(self):
        return self._platform_name

    def get_messages(self):
        return self._messages

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def plain_result(self, text):
        return SimpleNamespace(type="plain", text=text)

    def chain_result(self, chain):
        return SimpleNamespace(type="chain", chain=list(chain))


# ── 1. Settings & Schema Defaults ────────────────────────────────────────


def test_wave2_media_settings_defaults_and_bounds():
    defaults = MediaSettings()
    assert defaults.make_meme_daily_limit == 10
    assert defaults.character_check_auto_redraw is True
    assert defaults.crowd_curation_threshold == 2

    parsed = MediaSettings.parse(
        {
            "make_meme_daily_limit": 999,
            "character_check_auto_redraw": False,
            "crowd_curation_threshold": 0,
        }
    )
    assert parsed.make_meme_daily_limit == 200
    assert parsed.character_check_auto_redraw is False
    assert parsed.crowd_curation_threshold == 1

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text("utf-8"))
    media_items = schema["media"]["items"]
    assert media_items["make_meme_daily_limit"]["default"] == 10
    assert media_items["character_check_auto_redraw"]["default"] is True
    assert media_items["crowd_curation_threshold"]["default"] == 2


# ── 2. One-click Character Sticker Pack Pipeline ─────────────────────────


def test_character_pack_generate_split_fallback_tag_and_preview_commit(tmp_path):
    sheet_path = _make_png(tmp_path / "sheet.png", size=(300, 300))
    persisted: list[dict] = []

    class _FakeGenService:
        async def submit(self, **kwargs):
            return {"task_id": "pack_task_1"}

        async def wait_task(self, task_id, plugin_id=""):
            return {"status": "succeeded", "image_paths": [str(sheet_path)]}

    class _FakePluginAPI:
        async def _persist_image(self, **kwargs):
            persisted.append(kwargs)
            return {
                "hash": kwargs["file_hash"],
                "category": kwargs["category"],
                "character": kwargs.get("character", ""),
            }

    class _FailingVLM:
        async def classify_image(self, **kwargs):
            raise RuntimeError("vlm offline")

    owner = SimpleNamespace(
        name="astrbot_plugin_visual_media",
        library=SimpleNamespace(
            profiles=lambda: [
                {
                    "key": "miku",
                    "name": "初音",
                    "appearance": "双马尾蓝绿发",
                    "style": "Q版贴纸",
                    "references": [],
                }
            ],
            reference_path=lambda h: tmp_path / f"{h}.png",
        ),
        generation=SimpleNamespace(
            _plugin_data_dir=tmp_path,
            get_service=lambda api_version=1: _FakeGenService(),
        ),
        memes=SimpleNamespace(
            base_dir=tmp_path,
            plugin_config=SimpleNamespace(
                categories=["happy", "angry", "sad", "surprised", "confused"],
                closest_category=lambda k: k if k in {"happy", "angry", "sad"} else "happy",
            ),
            image_processor_service=_FailingVLM(),
            plugin_api=_FakePluginAPI(),
        ),
    )

    svc = CharacterPackService(owner)
    # 1) Auto-import mode (VLM raises -> graceful preset fallback, all 9 slices imported)
    res = asyncio.run(svc.generate_pack(character="miku", preview_only=False))
    assert res["character"] == "miku"
    assert res["total_slices"] == 9
    assert res["imported_count"] == 9
    assert res["failed_count"] == 0
    assert len(persisted) == 9
    assert all(p["character"] == "miku" for p in persisted)
    assert all(item["tagged_by"] == "preset_fallback" for item in res["items"])

    # 2) Preview-only mode -> commit selected 3 slices
    persisted.clear()
    preview = asyncio.run(svc.generate_pack(character="miku", preview_only=True))
    assert preview["preview_only"] is True
    assert preview["imported_count"] == 0
    assert len(preview["items"]) == 9
    assert preview["items"][0]["preview_url"].startswith("data:image/png;base64,")

    committed = asyncio.run(svc.commit_pack(pack_id=preview["pack_id"], indices=[0, 2, 4]))
    assert committed["imported_count"] == 3
    assert committed["failed_count"] == 0
    assert len(persisted) == 3


# ── 3. C-side /做表情 Quota, Quote Extraction & Reply Save ────────────────


def test_make_meme_quota_quote_and_reply_save_local(tmp_path):
    out_img = _make_png(tmp_path / "meme_out.png", size=(128, 128))
    persisted: list[dict] = []
    submitted_prompts: list[str] = []
    fail_next = {"flag": False}

    class _FakeGenService:
        async def submit(self, **kwargs):
            submitted_prompts.append(kwargs["prompt"])
            return {"task_id": "mm_1"}

        async def wait_task(self, task_id, plugin_id=""):
            if fail_next["flag"]:
                return {"status": "failed", "error": "upstream timeout"}
            return {"status": "succeeded", "image_paths": [str(out_img)]}

    class _FakePluginAPI:
        async def _persist_image(self, **kwargs):
            persisted.append(kwargs)
            return {"hash": kwargs["file_hash"], "category": kwargs["category"]}

    owner = SimpleNamespace(
        name="astrbot_plugin_visual_media",
        behavior=SimpleNamespace(settings=MediaSettings.parse({"make_meme_daily_limit": 2})),
        generation=SimpleNamespace(
            _plugin_data_dir=tmp_path,
            rate_limiter=SimpleNamespace(allows_group=lambda ev: True),
            avatar_handler=None,
            get_service=lambda api_version=1: _FakeGenService(),
        ),
        memes=SimpleNamespace(
            base_dir=tmp_path,
            plugin_config=SimpleNamespace(categories=["happy", "awkward"]),
            image_processor_service=None,
            plugin_api=_FakePluginAPI(),
        ),
    )

    svc = MakeMemeService(owner)
    reply_comp = SimpleNamespace(
        message_str="今天一定早睡",
        sender_id="999",
        sender_nickname="夜猫子",
        chain=[],
    )
    # Give reply_comp class name 'Reply'
    ReplyCls = type("Reply", (), {})
    reply_obj = ReplyCls()
    reply_obj.message_str = reply_comp.message_str
    reply_obj.sender_id = reply_comp.sender_id
    reply_obj.sender_nickname = reply_comp.sender_nickname
    reply_obj.chain = []

    ev1 = _FakeEvent(
        umo="aiocqhttp:GroupMessage:777",
        sender_id="u_alice",
        message_str="/做表情 凌晨三点再说",
        messages=[reply_obj],
    )
    res1 = asyncio.run(svc.create_meme(ev1, "凌晨三点再说"))
    assert res1["ok"] is True
    assert res1["used"] == 1 and res1["remaining"] == 1
    assert "今天一定早睡" in submitted_prompts[-1]
    assert "凌晨三点再说" in submitted_prompts[-1]

    # Reply 收藏 saves to local group library
    save_ev = _FakeEvent(umo="aiocqhttp:GroupMessage:777", sender_id="u_bob", message_str="收藏")
    save_msg = asyncio.run(svc.try_handle_save_reply(save_ev))
    assert save_msg and "本群专属表情库" in save_msg
    assert len(persisted) == 1
    assert persisted[0]["scope_mode"] == "local"
    assert persisted[0]["origin_target"] == "umo:aiocqhttp:GroupMessage:777"

    # Duplicate 收藏 is ignored
    assert asyncio.run(svc.try_handle_save_reply(save_ev)) is None

    # Failed generation refunds quota
    fail_next["flag"] = True
    res_fail = asyncio.run(svc.create_meme(ev1, "失败退款测试"))
    assert res_fail["ok"] is False
    assert "已退还" in res_fail["error"]
    assert svc.get_usage_sync("aiocqhttp:GroupMessage:777", "u_alice") == (1, 2)

    # Second success exhausts limit=2; third is rejected
    fail_next["flag"] = False
    assert asyncio.run(svc.create_meme(ev1, "第二次"))["ok"] is True
    res3 = asyncio.run(svc.create_meme(ev1, "第三次超限"))
    assert res3["ok"] is False
    assert "额度已用完" in res3["error"]


# ── 4. Reference Image Isolation ─────────────────────────────────────────


def test_reference_image_isolation_from_random_and_top_memes(tmp_path):
    assert normalize_scope_mode("reference") == "reference"
    assert normalize_scope_mode("character_ref") == "reference"

    ref_file = _make_png(tmp_path / "ref.png")
    pub_file = _make_png(tmp_path / "pub.png")
    index = {
        str(ref_file): {
            "hash": "hash_ref",
            "scope_mode": "public",
            "category": "happy",
            "desc": "角色标准三视图",
            "use_count": 99,
        },
        str(pub_file): {
            "hash": "hash_pub",
            "scope_mode": "public",
            "category": "happy",
            "desc": "日常开心表情",
            "use_count": 5,
        },
    }

    async def _update_index_path(path, updates):
        index[path].update(updates)
        return True

    cfg = SimpleNamespace(
        characters=[],
        character_info={},
        save_characters=lambda: None,
        save_character_info=lambda: None,
    )
    owner = SimpleNamespace(
        generation=SimpleNamespace(_plugin_data_dir=tmp_path),
        memes=SimpleNamespace(
            base_dir=tmp_path,
            plugin_config=cfg,
            plugin_api=SimpleNamespace(
                _get_index=lambda: index,
                _update_index_path=_update_index_path,
            ),
        ),
    )

    lib = MediaLibrary(owner)
    asyncio.run(
        lib.save_profile(
            {
                "key": "lily",
                "name": "莉莉",
                "appearance": "白发红瞳",
                "references": ["hash_ref"],
                "isolate_references": True,
            }
        )
    )
    # Reference image is now marked 'reference'
    assert index[str(ref_file)]["scope_mode"] == "reference"
    # Still readable by MediaLibrary.reference_path()
    assert lib.reference_path("hash_ref") == ref_file.resolve()

    # Isolated from MemeScopeService (both with event and without event)
    scope_svc = MemeScopeService(owner.memes)
    ev = _FakeEvent()
    assert scope_svc._is_entry_allowed_for_event(index[str(ref_file)], ev) is False
    assert scope_svc._is_entry_allowed_for_event(index[str(ref_file)], None) is False
    assert scope_svc._is_entry_allowed_for_event(index[str(pub_file)], ev) is True

    # Isolated from VisualMediaPlugin.get_group_top_memes & series_search_items
    plugin = object.__new__(VisualMediaPlugin)
    plugin._closed = False
    plugin.memes = owner.memes
    plugin.library = lib
    plugin.generation = None
    plugin.context = None
    plugin._safe_meme_index = lambda: index
    plugin._safe_tracker_rows = lambda: []
    plugin._safe_character_profiles = lib.profiles
    plugin._is_excluded_by_core = lambda **kw: False

    top = plugin.get_group_top_memes("aiocqhttp:GroupMessage:10001", limit=10)
    assert [item["hash"] for item in top] == ["hash_pub"]
    search = plugin.series_search_items("标准三视图", limit=10)
    assert not any(item["kind"] == "meme" for item in search)


# ── 5. /角色生图 & Tiered /视觉帮助 ───────────────────────────────────────


def test_character_generate_command_and_tiered_visual_help():
    called: list[tuple[str, str]] = []

    plugin = object.__new__(VisualMediaPlugin)
    plugin._closed = False
    plugin._is_event_excluded_by_core = lambda ev: False
    plugin._safe_character_profiles = lambda: [
        {"key": "anan", "name": "夏目安安", "appearance": "黑长直", "style": ""}
    ]

    async def _gen_char(key, prompt, ev):
        called.append((key, prompt))
        return {"task_id": "t1", "status": "queued"}

    plugin.library = SimpleNamespace(generate_character=_gen_char)

    # Match by display name with multi-word prompt
    ev = _FakeEvent(message_str="/角色生图 夏目安安 在海边 举着奶茶微笑")
    outputs = []

    async def _run():
        async for out in plugin.character_generate_command(ev, "夏目安安", "在海边"):
            outputs.append(out.text)

    asyncio.run(_run())
    assert called == [("anan", "在海边 举着奶茶微笑")]
    assert any("已提交角色" in t for t in outputs)

    # Unknown character lists available characters
    ev_bad = _FakeEvent(message_str="/角色生图 未知角色 画一张")
    bad_out = []

    async def _run_bad():
        async for out in plugin.character_generate_command(ev_bad, "未知角色", "画一张"):
            bad_out.append(out.text)

    asyncio.run(_run_bad())
    assert any("夏目安安(anan)" in t for t in bad_out)

    # Tiered visual_help covers all Wave 2 commands
    help_out = []

    async def _run_help():
        async for out in plugin.visual_help(_FakeEvent()):
            help_out.append(out.text)

    asyncio.run(_run_help())
    help_text = "\n".join(help_out)
    for token in ("/换风格", "/快速", "@provider:model", "/角色生图", "/做表情", "/meme pref", "/meme 审图", "管理员"):
        assert token in help_text


# ── 6. character_check Mismatch Auto-Redraw (max 1x) ─────────────────────


def test_character_check_mismatch_auto_redraw_capped_at_once(tmp_path, monkeypatch):
    import astrbot_plugin_visual_media.character_check as cc_mod

    submitted: list[dict] = []
    sent_notices: list[str] = []

    async def _fake_dispatch(*args, **kwargs):
        return True

    async def _fake_check(*args, **kwargs):
        return {"status": "mismatch", "issues": ["发色偏红"], "reason": "发色不一致"}

    if "astrbot_plugin_visual_media.tl" not in sys.modules:
        tl_pkg = types.ModuleType("astrbot_plugin_visual_media.tl")
        tl_pkg.__path__ = [str(ROOT / "tl")]
        monkeypatch.setitem(sys.modules, "astrbot_plugin_visual_media.tl", tl_pkg)

    bd_mod = types.ModuleType("astrbot_plugin_visual_media.tl.background_dispatch")
    bd_mod.dispatch_generation_result = _fake_dispatch
    monkeypatch.setitem(sys.modules, "astrbot_plugin_visual_media.tl.background_dispatch", bd_mod)

    gt_mod = types.ModuleType("astrbot_plugin_visual_media.tl.generation_tracker")
    gt_mod.requester_from_event = lambda ev: {"umo": getattr(ev, "unified_msg_origin", "")}
    monkeypatch.setitem(sys.modules, "astrbot_plugin_visual_media.tl.generation_tracker", gt_mod)

    monkeypatch.setattr(cc_mod, "check_character", _fake_check)

    class _FakeGenService:
        async def submit(self, **kwargs):
            idx = len(submitted) + 1
            submitted.append(kwargs)
            return {"task_id": f"char_t_{idx}"}

    async def _fake_send(session, chain):
        for comp in getattr(chain, "chain", []):
            txt = getattr(comp, "text", "")
            if txt:
                sent_notices.append(txt)

    # Ensure MessageChain and MessageSession are available even when conftest.py stubs astrbot.
    class _StubChain:
        def __init__(self):
            self.chain = []

        def message(self, text):
            self.chain.append(SimpleNamespace(text=text))
            return self

    ev_mod = types.ModuleType("astrbot.api.event")
    ev_mod.MessageChain = _StubChain
    monkeypatch.setitem(sys.modules, "astrbot.api.event", ev_mod)

    sess_mod = types.ModuleType("astrbot.core.platform.message_session")
    sess_mod.MessageSession = SimpleNamespace(from_str=lambda s: s)
    monkeypatch.setitem(sys.modules, "astrbot.core.platform.message_session", sess_mod)

    from astrbot_plugin_visual_media.media_runtime import MediaRuntime

    behavior = MediaRuntime(tmp_path, lambda: {"character_check_auto_redraw": True})
    owner = SimpleNamespace(
        name="astrbot_plugin_visual_media",
        _closed=False,
        behavior=behavior,
        tasks=None,
        context=SimpleNamespace(send_message=_fake_send),
        generation=SimpleNamespace(
            _plugin_data_dir=tmp_path,
            rate_limiter=SimpleNamespace(allows_group=lambda ev: True),
            get_service=lambda: _FakeGenService(),
        ),
        memes=SimpleNamespace(
            base_dir=tmp_path,
            plugin_config=SimpleNamespace(
                characters=["anan"],
                character_info={},
                save_characters=lambda: None,
                save_character_info=lambda: None,
            ),
            plugin_api=SimpleNamespace(_get_index=lambda: {}),
        ),
    )
    lib = MediaLibrary(owner)
    asyncio.run(
        lib.save_profile({"key": "anan", "name": "安安", "appearance": "黑发", "references": []})
    )

    async def _scenario():
        ev = _FakeEvent(umo="aiocqhttp:GroupMessage:888")
        await lib.generate_character("anan", "喝咖啡", ev)
        assert len(submitted) == 1
        # Simulate first generation completing -> triggers check -> mismatch -> auto redraw 1 time
        await submitted[0]["on_complete"](
            {"status": "succeeded", "task_id": "char_t_1", "image_paths": [str(tmp_path / "a.png")]}
        )
        # Wait for post-delivery check task
        for t in list(getattr(lib, "_check_tasks", ())):
            await t
        assert len(submitted) == 2
        assert "发色偏红" in submitted[1]["prompt"]

        # Simulate redrawn generation also mismatching -> must NOT trigger a 3rd generation!
        await submitted[1]["on_complete"](
            {"status": "succeeded", "task_id": "char_t_2", "image_paths": [str(tmp_path / "b.png")]}
        )
        for t in list(getattr(lib, "_check_tasks", ())):
            await t
        assert len(submitted) == 2
        assert any("/角色生图" in n for n in sent_notices)

    asyncio.run(_scenario())


# ── 7. pHash Clustering & Pending Batch Pre-Tag ──────────────────────────


def test_phash_clustering_groups_similar_and_picks_best_keeper():
    # Two near-identical 64-hex-char phashes (Hamming distance 1) + one distant hash
    base = "0" * 64
    near = "0" * 63 + "1"
    far = "f" * 64
    entries = [
        {"hash": "h1", "phash": base, "use_count": 2, "is_favorite": 0, "width": 200, "height": 200},
        {"hash": "h2", "phash": near, "use_count": 10, "is_favorite": 1, "width": 400, "height": 400},
        {"hash": "h3", "phash": far, "use_count": 1, "is_favorite": 0, "width": 200, "height": 200},
    ]
    clusters = PHashDedupService.cluster_similar_images(entries, threshold=20)
    assert len(clusters) == 1
    assert clusters[0]["size"] == 2
    assert clusters[0]["recommended_keep"] == "h2"
    assert clusters[0]["max_distance"] == 1


# ── 8. Group Crowd Curation (/meme 审图) ─────────────────────────────────


def test_crowd_curation_voting_approve_and_reject(tmp_path):
    p1 = _make_png(tmp_path / "p1.png")
    p2 = _make_png(tmp_path / "p2.png")
    pending_rows = [
        {"id": 101, "path": str(p1), "hash": "h101", "category": "happy", "desc": "搞笑图1", "review_status": "pending", "origin_target": "umo:g1"},
        {"id": 102, "path": str(p2), "hash": "h102", "category": "angry", "desc": "烂图2", "review_status": "pending", "origin_target": "umo:g1"},
    ]
    approved_ids: list[int] = []
    updated_calls: list[tuple[int, dict]] = []

    class _FakeDB:
        def get_pending_paginated(self, **kwargs):
            return list(pending_rows), len(pending_rows), {}

        async def update_pending(self, pid, fields, **kwargs):
            updated_calls.append((pid, fields))
            return {"id": pid, **fields}

        def delete_pending_batch(self, ids):
            removed = [r for r in pending_rows if r["id"] in ids]
            pending_rows[:] = [r for r in pending_rows if r["id"] not in ids]
            return removed

    class _FakeAPI:
        async def _approve_one(self, db, pid):
            approved_ids.append(pid)
            pending_rows[:] = [r for r in pending_rows if r["id"] != pid]
            return True, ""

    owner = SimpleNamespace(
        behavior=SimpleNamespace(settings=MediaSettings(crowd_curation_threshold=2)),
        memes=SimpleNamespace(db_service=_FakeDB(), plugin_api=_FakeAPI()),
    )
    svc = CrowdCurationService(owner)
    u1 = _FakeEvent(umo="g1", sender_id="u1", message_str="通过")
    u2 = _FakeEvent(umo="g1", sender_id="u2", message_str="+1")

    sess, err = asyncio.run(svc.start_session(u1))
    assert err == "" and sess["pending_id"] == 101
    # First vote
    r1 = asyncio.run(svc.try_handle_vote_message(u1))
    assert "通过 1/2" in r1
    # Duplicate vote from same user does not count twice
    r1_dup = asyncio.run(svc.try_handle_vote_message(u1))
    assert "已经投过" in r1_dup and not approved_ids
    # Second user reaches threshold -> promotes into local group library
    r2 = asyncio.run(svc.try_handle_vote_message(u2))
    assert "审图通过" in r2
    assert approved_ids == [101]
    assert updated_calls[-1] == (101, {"scope_mode": "local", "origin_target": "umo:g1"})

    # Next session pulls 102 -> two reject votes delete it
    sess2, err2 = asyncio.run(svc.start_session(u1))
    assert err2 == "" and sess2["pending_id"] == 102
    asyncio.run(svc.cast_vote(u1, "reject"))
    res_rej = asyncio.run(svc.cast_vote(u2, "reject"))
    assert "审图未通过" in res_rej
    assert not p2.exists()


# ── 9. Cross-Platform Capability Matrix & Degradation ────────────────────


def test_platform_capability_matrix_and_avatar_fallback(monkeypatch):
    onebot = get_platform_capabilities(_FakeEvent(platform_name="aiocqhttp"))
    assert onebot.platform_family == "onebot"
    assert onebot.supports_qq_sticker is True
    assert onebot.supports_forward_node is True
    assert onebot.supports_avatar_lookup is True

    discord = get_platform_capabilities(_FakeEvent(platform_name="discord"))
    assert discord.platform_family == "discord"
    assert discord.supports_qq_sticker is False
    assert discord.supports_forward_node is False
    assert discord.supports_interactive_buttons is True
    assert "Discord" in discord.degradation_hint("forward_node")

    tg = get_platform_capabilities(_FakeEvent(platform_name="telegram"))
    assert tg.supports_telegram_sticker is True
    assert tg.supports_qq_sticker is False

    wx = get_platform_capabilities(_FakeEvent(platform_name="gewechat"))
    assert wx.platform_family == "wechat"
    assert wx.supports_avatar_lookup is False
    assert "微信" in wx.degradation_hint("avatar_lookup")

    matrix = list_platform_capability_matrix()
    assert {"onebot", "qq_official", "discord", "telegram", "wechat"}.issubset(matrix.keys())

    tl_utils_stub = types.ModuleType("tl.tl_utils")
    tl_utils_stub.AvatarManager = lambda: SimpleNamespace()
    tl_utils_stub.download_qq_avatar = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "tl.tl_utils", tl_utils_stub)
    import importlib

    ah_mod = importlib.import_module("tl.avatar_handler")
    ah_mod = importlib.reload(ah_mod)
    handler = ah_mod.AvatarHandler(auto_avatar_reference=True)
    # Non-supporting platform degrades cleanly to None without calling download_qq_avatar
    assert asyncio.run(handler.download_user_avatar("wxid_abc", _FakeEvent(platform_name="gewechat"))) is None


# ── 10. joint_reply Prefetch Timeout/Degradation Metrics ─────────────────


def test_joint_prefetch_metrics_tracking(tmp_path):
    behavior = MediaBehavior(tmp_path)
    ev = _FakeEvent(umo="aiocqhttp:GroupMessage:555", sender_id="u1")

    behavior.record_joint_prefetch(ev, outcome="candidates", elapsed_ms=100.0, candidates=3)
    behavior.record_joint_prefetch(ev, outcome="timeout", elapsed_ms=400.0, candidates=0)
    behavior.record_joint_prefetch(ev, outcome="local_fallback", elapsed_ms=250.0, candidates=2)
    behavior.record_joint_prefetch(ev, outcome="error", elapsed_ms=50.0, candidates=0)

    summary = behavior.joint_prefetch_summary()
    assert summary["total"] == 4
    assert summary["ok"] == 1
    assert summary["timeout"] == 1
    assert summary["local_fallback"] == 1
    assert summary["error"] == 1
    assert summary["degraded"] == 3
    assert summary["timeout_rate"] == 25.0
    assert summary["degraded_rate"] == 75.0
    assert summary["avg_elapsed_ms"] == 200.0

    # Degraded turns also emit audit decision records
    recs = behavior.records(limit=20)
    jp_modes = [r for r in recs if r["mode"] == "joint_prefetch"]
    assert len(jp_modes) == 3


# ── 11. Workshop JS Structural Contracts ─────────────────────────────────


def test_wave2_workshop_frontend_hooks_present():
    root = Path(__file__).resolve().parents[1] / "views" / "workshop"
    workshop_js = (root / "workshop.js").read_text("utf-8")
    behavior_js = (root / "behavior" / "behavior.js").read_text("utf-8")
    memes_tpl = (root / "memes" / "template.js").read_text("utf-8")
    memes_app = (root / "memes" / "app.js").read_text("utf-8")

    assert "media/character-pack" in workshop_js
    assert "media/character-pack/commit" in workshop_js
    assert "empty-library-onboarding" in workshop_js
    assert "media/quick-steal" in workshop_js
    assert "isolate_references" in workshop_js

    assert "joint-prefetch-metrics" in behavior_js
    assert "joint_prefetch" in behavior_js

    assert "openDedupModal" in memes_tpl and "openDedupModal" in memes_app
    assert "api/dedup/clusters" in memes_app and "api/dedup/merge" in memes_app
    assert "openPendingBatchTagModal" in memes_tpl and "api/pending/batch-update" in memes_app
    assert "advancePendingFocus" in memes_app
