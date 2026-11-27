"""Wave 1 regression & contract unit tests for astrbot_plugin_visual_media.

Covers:
1. Generation waiting notice deduplication & pre-check ordering.
2. Dynamic bot name stripping (no hardcoded '安安') & /meme pref self-service command.
3. Readable scope_label / member_name persistence & UMO alignment in MediaBehavior.
4. FaissVecDB disconnected SQLite auto-reconnect retry & cold-start provider race recovery.
5. Series contracts: set_series_scope_enabled (optimistic locking), activate_series_runtime,
   generate_series_artwork, get_group_top_memes, shared provider inheritance, and JS scope hooks.
"""

from __future__ import annotations

import asyncio
import ast
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


def _ensure_minimal_astrbot_stubs() -> None:
    if "astrbot" not in sys.modules:
        sys.modules["astrbot"] = types.ModuleType("astrbot")
    astrbot_mod = sys.modules["astrbot"]
    if not hasattr(astrbot_mod, "__path__"):
        astrbot_mod.__path__ = []
    if "astrbot.api" not in sys.modules:
        api_mod = types.ModuleType("astrbot.api")
        api_mod.__path__ = []
        api_mod.logger = SimpleNamespace(
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        )
        api_mod.AstrBotConfig = dict
        sys.modules["astrbot.api"] = api_mod
        astrbot_mod.api = api_mod
    else:
        api_mod = sys.modules["astrbot.api"]
        if not hasattr(api_mod, "__path__"):
            api_mod.__path__ = []
        if not hasattr(api_mod, "AstrBotConfig"):
            api_mod.AstrBotConfig = dict
    if "astrbot.api.event" not in sys.modules:
        ev_mod = types.ModuleType("astrbot.api.event")
        ev_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
        ev_mod.MessageChain = type("MessageChain", (), {})
        sys.modules["astrbot.api.event"] = ev_mod
    ev_mod = sys.modules["astrbot.api.event"]
    if not hasattr(ev_mod, "filter"):
        class _CmdGroup:
            def __init__(self, fn):
                self._fn = fn

            def __call__(self, *a, **k):
                return self._fn(*a, **k)

            def command(self, *a, **k):
                return lambda f: f

        def _noop_dec(*a, **k):
            return lambda f: f

        ev_mod.filter = SimpleNamespace(
            command=_noop_dec,
            command_group=lambda *a, **k: (lambda fn: _CmdGroup(fn)),
            permission_type=_noop_dec,
            llm_tool=_noop_dec,
            event_message_type=_noop_dec,
            platform_adapter_type=_noop_dec,
            on_llm_tool_respond=_noop_dec,
            on_llm_request=_noop_dec,
            on_llm_response=_noop_dec,
            on_decorating_result=_noop_dec,
            on_astrbot_loaded=_noop_dec,
            on_platform_loaded=_noop_dec,
            EventMessageType=SimpleNamespace(ALL="ALL"),
            PermissionType=SimpleNamespace(ADMIN="ADMIN"),
            PlatformAdapterType=SimpleNamespace(ALL="ALL"),
        )
    if "astrbot.api.event.filter" not in sys.modules:
        f_mod = types.ModuleType("astrbot.api.event.filter")
        f_mod.EventMessageType = ev_mod.filter.EventMessageType
        f_mod.PermissionType = ev_mod.filter.PermissionType
        f_mod.PlatformAdapterType = ev_mod.filter.PlatformAdapterType
        sys.modules["astrbot.api.event.filter"] = f_mod
    if "astrbot.api.star" not in sys.modules:
        star_mod = types.ModuleType("astrbot.api.star")
        star_mod.Context = type("Context", (), {})
        star_mod.Star = type("Star", (), {"__init__": lambda self, ctx=None: setattr(self, "context", ctx)})
        star_mod.StarTools = SimpleNamespace(get_data_dir=lambda name: ROOT / ".tmp_test_data" / name)
        sys.modules["astrbot.api.star"] = star_mod
    if "astrbot.core" not in sys.modules:
        core_mod = types.ModuleType("astrbot.core")
        core_mod.__path__ = []
        sys.modules["astrbot.core"] = core_mod
    if "astrbot.core.star" not in sys.modules:
        cs_mod = types.ModuleType("astrbot.core.star")
        cs_mod.__path__ = []
        sys.modules["astrbot.core.star"] = cs_mod
    if "astrbot.core.star.star" not in sys.modules:
        css_mod = types.ModuleType("astrbot.core.star.star")
        css_mod.star_registry = []
        sys.modules["astrbot.core.star.star"] = css_mod
    if "astrbot.api.web" not in sys.modules:
        web_mod = types.ModuleType("astrbot.api.web")
        web_mod.request = SimpleNamespace(_request=None, query={})
        web_mod.json_response = lambda data, status_code=200: (status_code, data)
        sys.modules["astrbot.api.web"] = web_mod
    if "astrbot.api.provider" not in sys.modules:
        prov_mod = types.ModuleType("astrbot.api.provider")
        prov_mod.ProviderRequest = type("ProviderRequest", (), {})
        sys.modules["astrbot.api.provider"] = prov_mod
    if "astrbot.api.message_components" not in sys.modules:
        comp_mod = types.ModuleType("astrbot.api.message_components")
        comp_mod.Image = type("Image", (), {})
        comp_mod.Plain = type("Plain", (), {})
        comp_mod.Node = type("Node", (), {})
        sys.modules["astrbot.api.message_components"] = comp_mod


_ensure_minimal_astrbot_stubs()


def _load_visual_media_plugin_class():
    """Extract VisualMediaPlugin methods from main.py cleanly without requiring cv2/host runtime."""
    from astrbot_plugin_visual_media.core.util.series_config import resolve_shared_provider
    from astrbot_plugin_visual_media.media_runtime import MediaRuntime

    source_path = ROOT / "main.py"
    source = ast.parse(source_path.read_text(encoding="utf-8"))
    cls_node = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "VisualMediaPlugin")
    keep = {
        "_parse_series_ts",
        "_extract_group_id_from_umo",
        "_origin_matches_scope",
        "_derive_meme_scope_target",
        "_safe_tracker_rows",
        "_safe_meme_index",
        "_safe_character_profiles",
        "_series_panels_mod",
        "_series_tz",
        "_is_excluded_by_core",
        "get_series_scope_status",
        "set_series_scope_enabled",
        "activate_series_runtime",
        "generate_series_artwork",
        "get_group_top_memes",
        "meme_pref",
    }
    cls_node.bases = []
    cls_node.body = [
        n
        for n in cls_node.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in keep
    ]
    for method in cls_node.body:
        method.decorator_list = [
            d for d in method.decorator_list if isinstance(d, ast.Name) and d.id == "staticmethod"
        ]
    source.body = [
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        cls_node,
    ]
    ast.fix_missing_locations(source)
    ns = {
        "__package__": "astrbot_plugin_visual_media",
        "asyncio": asyncio,
        "datetime": __import__("datetime").datetime,
        "timezone": __import__("datetime").timezone,
        "SimpleNamespace": SimpleNamespace,
        "Any": object,
        "AstrMessageEvent": object,
        "MediaRuntime": MediaRuntime,
        "resolve_shared_provider": resolve_shared_provider,
    }
    exec(compile(source, str(source_path), "exec"), ns)
    return ns["VisualMediaPlugin"]


class _DummyEvent:
    def __init__(
        self,
        *,
        umo: str = "aiocqhttp:GroupMessage:123456",
        sender_id: str = "10001",
        sender_name: str = "小明",
        group_id: str = "123456",
        group_name: str = "星尘小群",
        message_str: str = "",
    ):
        self.unified_msg_origin = umo
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.group_id = group_id
        self._group_name = group_name
        self.message_str = message_str
        self.message_obj = SimpleNamespace(message_id="msg-1", self_id="99999")
        self._extras: dict = {}

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return self.group_id

    def get_group_name(self):
        return self._group_name

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def plain_result(self, text: str):
        return text


# ---------------------------------------------------------------------------
# 1. Generation waiting notice deduplication & pre-check ordering
# ---------------------------------------------------------------------------


def _build_lightweight_gen_plugin():
    """Extract command methods from generation_engine.py without importing cv2/host deps."""
    from tl import enhanced_prompts
    from tl.core.errors import CoreError, QueueFullError, QuotaExceededError, ServiceClosedError, redact_text
    from tl.core.states import JobState
    from tl.format_error import format_error_message
    from tl.generation_errors import api_failure_message
    from tl.generation_tracker import requester_from_event
    from tl.prompt_templates import builtin_template, render_template
    from tl.provider_capabilities import parse_model_selector, select_candidates
    from tl.tl_api import ApiRequestConfig
    from tl.v2_bridge import (
        apply_result_to_config as v2_apply_result_to_config,
        command_request as v2_command_request,
        split_artifacts as v2_split_artifacts,
        v2_failure_message,
        v2_failure_text,
    )

    source_path = ROOT / "generation_engine.py"
    source = ast.parse(source_path.read_text(encoding="utf-8"))
    plugin_node = next(n for n in source.body if isinstance(n, ast.ClassDef))
    keep = {
        "_check_command_generation_limit",
        "_quick_generate_image",
        "generate_image",
        "modify_image",
        "_handle_quick_mode",
        "_quick_template",
        "_quick_dimensions",
        "quick_avatar",
        "quick_sticker",
        "_parse_generation_route",
        "_extract_prompt_from_message",
        "_resolve_quick_mode_params",
        "_resolve_quick_mode_custom_size_overrides",
    }
    plugin_node.bases = []
    plugin_node.body = [
        n
        for n in plugin_node.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in keep
    ]
    for method in plugin_node.body:
        method.decorator_list = [
            d for d in method.decorator_list if isinstance(d, ast.Name) and d.id == "staticmethod"
        ]
    helper_nodes = [
        n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "_build_no_ref_msg"
    ]
    source.body = [
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *helper_nodes,
        plugin_node,
    ]
    ast.fix_missing_locations(source)
    ns = {
        "asyncio": asyncio,
        "shlex": __import__("shlex"),
        "time": __import__("time"),
        "Path": Path,
        "logger": SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None),
        "redact_text": redact_text,
        "ApiRequestConfig": ApiRequestConfig,
        "CoreError": CoreError,
        "QueueFullError": QueueFullError,
        "QuotaExceededError": QuotaExceededError,
        "ServiceClosedError": ServiceClosedError,
        "JobState": JobState,
        "format_error_message": format_error_message,
        "api_failure_message": api_failure_message,
        "requester_from_event": requester_from_event,
        "builtin_template": builtin_template,
        "render_template": render_template,
        "parse_model_selector": parse_model_selector,
        "select_candidates": select_candidates,
        "v2_apply_result_to_config": v2_apply_result_to_config,
        "v2_command_request": v2_command_request,
        "v2_split_artifacts": v2_split_artifacts,
        "v2_failure_message": v2_failure_message,
        "v2_failure_text": v2_failure_text,
        **vars(enhanced_prompts),
    }
    exec(compile(source, str(source_path), "exec"), ns)
    cls = ns["GeminiImageGenerationPlugin"]
    plugin = cls()
    candidate = SimpleNamespace(
        id="cand-1",
        api_type="gemini",
        model="gemini-2.5-flash-image",
        api_keys=["k1"],
        settings={},
        supports_image_edit=True,
        model_alias=None,
    )
    plugin.cfg = SimpleNamespace(
        provider_candidates=[candidate],
        total_timeout=60,
        enable_smart_retry=True,
        max_attempts_per_key=2,
        preserve_reference_image_size=False,
        enable_sticker_split=False,
        sticker_grid_rows=4,
        sticker_grid_cols=4,
        quick_mode_overrides={},
        vision_provider_id="",
        enable_llm_crop=False,
        enable_sticker_zip=False,
    )
    plugin.rate_limiter = SimpleNamespace(allows_group=lambda ev: True)
    plugin.api_client = object()
    plugin._ensure_api_client = lambda: plugin.api_client is not None
    plugin.avatar_handler = SimpleNamespace(
        should_use_avatar=AsyncMock(return_value=False),
        should_use_avatar_for_prompt=AsyncMock(return_value=False),
    )
    plugin.image_handler = SimpleNamespace(
        fetch_images_from_event=AsyncMock(return_value=([], [])),
        filter_valid_reference_images=lambda imgs, **k: list(imgs or []),
    )

    async def _no_duration(*a, **k):
        if False:
            yield None

    plugin.message_sender = SimpleNamespace(
        send_results_with_stream_retry=AsyncMock(),
        send_api_duration=_no_duration,
    )
    return plugin


def test_generation_single_waiting_notice_and_precheck_order():
    from tl.core.errors import QuotaExceededError
    from tl.core.results import GenerationResult
    from tl.core.states import JobState

    async def _run():
        plugin = _build_lightweight_gen_plugin()

        class _Svc:
            def __init__(self, submit_err=None):
                self.submit_err = submit_err

            def result_wait_timeout(self, d):
                return d + 10

            async def prepare_references(self, refs, *, requester):
                return tuple(refs)

            async def submit(self, req, *, admission=None):
                if self.submit_err is not None:
                    raise self.submit_err
                return SimpleNamespace(job_id="j1")

            async def wait(self, job_id, timeout=None):
                return SimpleNamespace(
                    state=JobState.SUCCEEDED,
                    result=GenerationResult(
                        artifacts=("https://img.test/1.png",), text="ok", provider="p", model="m"
                    ),
                )

            async def cancel(self, job_id):
                return None

        plugin._ensure_v2_service = lambda: _Svc()
        ev = _DummyEvent()

        # 1. /生图 emits exactly 1 waiting notice (no duplicate "开始生成图像...")
        out_gen = [x async for x in plugin.generate_image(ev, "一只猫")]
        assert out_gen == ["🎨 生成中..."]

        # 2. /快速 头像 emits exactly 1 waiting notice
        out_avatar = [x async for x in plugin.quick_avatar(ev, "蓝色头发")]
        assert out_avatar == ["🎨 头像模式生成中..."]

        # 3. /改图 without ref image fails BEFORE any waiting notice
        out_mod_noref = [x async for x in plugin.modify_image(ev, "换成夜景")]
        assert len(out_mod_noref) == 1
        assert "需要参考图" in out_mod_noref[0]
        assert not any("生成中" in x or "开始修改" in x for x in out_mod_noref)

        # 4. /改图 with ref image emits exactly 1 waiting notice
        plugin.image_handler.fetch_images_from_event = AsyncMock(return_value=(["ref.png"], []))
        out_mod_ok = [x async for x in plugin.modify_image(ev, "换成夜景")]
        assert out_mod_ok == ["🎨 修改生成中..."]

        # 5. /快速 表情包 without ref image fails BEFORE any waiting notice
        plugin.image_handler.fetch_images_from_event = AsyncMock(return_value=([], []))
        out_sticker_noref = [x async for x in plugin.quick_sticker(ev, "开心")]
        assert len(out_sticker_noref) == 1
        assert "需要参考图" in out_sticker_noref[0]
        assert not any("生成中" in x or "表情包模式生成图像" in x for x in out_sticker_noref)

        # 6. Quota denial emits 0 waiting notices
        plugin._ensure_v2_service = lambda: _Svc(
            submit_err=QuotaExceededError("额度已满", details={"silent": False})
        )
        out_quota = [x async for x in plugin.generate_image(ev, "一只猫")]
        assert out_quota == ["额度已满"]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 2 & 3A. Dynamic bot names, readable scope_label/member_name, /meme pref
# ---------------------------------------------------------------------------


def test_media_behavior_dynamic_bot_names_and_no_hardcoded_anan(tmp_path):
    src = (ROOT / "media_behavior.py").read_text(encoding="utf-8")
    assert "安安" not in src

    from astrbot_plugin_visual_media.media_behavior import MediaBehavior

    class _ChatOwner:
        def get_series_shared_config(self):
            return {"bot_names": ["小晴", "SunnyBot"]}

    ctx = SimpleNamespace(
        get_all_stars=lambda: {
            "cd": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics", activated=True, star_cls=_ChatOwner()
            )
        }
    )
    behavior = MediaBehavior(tmp_path, context=ctx)

    ev1 = _DummyEvent(message_str="小晴，以后少发表情包吧", sender_id="u1", sender_name="阿哲")
    behavior.remember_explicit(ev1)
    assert behavior.preferences(ev1)["frequency"] == "less"

    ev2 = _DummyEvent(message_str="@小晴 以后不要再发表情了", sender_id="u2", sender_name="小美")
    behavior.remember_explicit(ev2)
    assert behavior.preferences(ev2)["frequency"] == "off"

    ev3 = _DummyEvent(
        message_str="[CQ:at,qq=12345] SunnyBot 以后多发表情吧",
        sender_id="u3",
        sender_name="老王",
    )
    behavior.remember_explicit(ev3)
    assert behavior.preferences(ev3)["frequency"] == "more"

    # Readable scope_label & member_name stored & joined into records & preference_rows
    prefs = behavior.preference_rows()
    by_name = {p["member_name"]: p for p in prefs}
    assert "阿哲" in by_name
    assert "小美" in by_name
    assert by_name["阿哲"]["scope_label"] == "星尘小群 (123456)"
    assert by_name["阿哲"]["scope_umo"] == "aiocqhttp:GroupMessage:123456"

    # Querying records by raw UMO resolves to the hashed scope seamlessly
    recs = behavior.records(limit=20, scope="aiocqhttp:GroupMessage:123456")
    assert len(recs) >= 3
    assert all(r["scope_label"] == "星尘小群 (123456)" for r in recs)
    assert {r["member_name"] for r in recs} >= {"阿哲", "小美", "老王"}

    # Backward compatibility: legacy hash-only row with no label table entry still loads cleanly
    with behavior.connect() as db:
        db.execute(
            "INSERT INTO preferences(scope, member, data, updated) VALUES (?,?,?,?)",
            ("0123456789abcdef01234567", "fedcba9876543210fedcba98", '{"frequency":"less","style":"","blocked":[]}', 1.0),
        )
    all_prefs = behavior.preference_rows()
    legacy = next(p for p in all_prefs if p["scope"] == "0123456789abcdef01234567")
    assert legacy["scope_label"] == ""
    assert legacy["member_name"] == ""
    assert legacy["data"]["frequency"] == "less"


def test_meme_pref_command_show_set_and_reset(tmp_path):
    from astrbot_plugin_visual_media.media_runtime import MediaRuntime

    async def _run():
        VisualMediaPlugin = _load_visual_media_plugin_class()

        plugin = VisualMediaPlugin.__new__(VisualMediaPlugin)
        plugin.behavior = MediaRuntime(tmp_path)
        ev = _DummyEvent(sender_id="u42", sender_name="团子")

        show_default = [x async for x in plugin.meme_pref(ev, "show")]
        assert "自然选择" in show_default[0]

        set_less = [x async for x in plugin.meme_pref(ev, "less")]
        assert "少用表情" in set_less[0]
        assert plugin.behavior.preferences(ev)["frequency"] == "less"

        set_off = [x async for x in plugin.meme_pref(ev, "off")]
        assert "不主动发表情" in set_off[0]
        assert plugin.behavior.preferences(ev)["frequency"] == "off"

        set_more = [x async for x in plugin.meme_pref(ev, "more")]
        assert "更常用表情" in set_more[0]
        assert plugin.behavior.preferences(ev)["frequency"] == "more"

        reset_out = [x async for x in plugin.meme_pref(ev, "reset")]
        assert "已重置" in reset_out[0]
        assert plugin.behavior.preferences(ev)["frequency"] == "auto"

        bad_out = [x async for x in plugin.meme_pref(ev, "unknown_opt")]
        assert "/meme pref [show|auto|less|off|more|reset]" in bad_out[0]

        await plugin.behavior.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 3B. FaissVecDB reconnect retry & cold-start provider race recovery
# ---------------------------------------------------------------------------


def test_faiss_reconnect_retry_and_cold_start_provider_race(tmp_path, monkeypatch):
    from core.search.embedding_service import EmbeddingService

    try:
        from astrbot.core.provider.provider import EmbeddingProvider as _EmbBase
    except Exception:
        _EmbBase = object

    class _FakeProvider(_EmbBase):
        def __init__(self):
            self.provider_config = {"id": "emb-shared"}

        def get_dim(self):
            return 3

        def get_model(self):
            return "bge-test"

        async def get_embedding(self, text):
            return [1.0, 0.0, 0.0]

    # Inject fake faiss & FaissVecDB modules that fail with disconnected sqlite on 1st call
    created_clients: list = []

    class _FlakyFaissVecDB:
        def __init__(self, db_path, index_path, adapter):
            self.db_path = db_path
            self.index_path = index_path
            self.adapter = adapter
            self.init_calls = 0
            self.closed = False
            self.document_storage = SimpleNamespace(conn=object())
            created_clients.append(self)

        async def initialize(self):
            self.init_calls += 1
            self.document_storage.conn = object()

        async def retrieve(self, *, query, k, fetch_k, rerank):
            if len(created_clients) == 1:
                raise sqlite3.OperationalError("no active connection")
            return [SimpleNamespace(data={"metadata": {"path": "/memes/cat.png"}}, similarity=0.92)]

        async def insert_batch(self, *, contents, metadatas, **kwargs):
            if len(created_clients) == 2 and not getattr(self, "_retried_insert", False):
                self._retried_insert = True
                raise RuntimeError("Connection closed")
            return [1 for _ in contents]

        async def delete_documents(self, *, metadata_filters):
            return None

        async def close(self):
            self.closed = True
            self.document_storage.conn = None

    fake_faiss = types.ModuleType("faiss")
    fake_vec_mod = types.ModuleType("astrbot.core.db.vec_db.faiss_impl.vec_db")
    fake_vec_mod.FaissVecDB = _FlakyFaissVecDB
    monkeypatch.setitem(sys.modules, "faiss", fake_faiss)
    monkeypatch.setitem(sys.modules, "astrbot.core.db.vec_db.faiss_impl.vec_db", fake_vec_mod)

    providers_ready = {"list": []}

    class _ChatOwner:
        def get_series_shared_config(self):
            return {"embedding_provider": "emb-shared"}

    provider_inst = _FakeProvider()
    ctx = SimpleNamespace(
        get_all_stars=lambda: {
            "cd": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics", activated=True, star_cls=_ChatOwner()
            )
        },
        provider_manager=SimpleNamespace(inst_map={}),
        get_all_embedding_providers=lambda: list(providers_ready["list"]),
    )
    plugin = SimpleNamespace(
        enable_embedding_search=True,
        embedding_provider_id="",
        context=ctx,
        plugin_config=SimpleNamespace(data_dir=tmp_path, model_request_interval=0.0),
        db_service=None,
    )

    async def _run():
        svc = EmbeddingService(plugin)
        # Cold-start race: no provider yet during initialize()
        await svc.initialize()
        assert svc._initialized is False
        assert svc._faiss_available is None  # Must NOT be poisoned to False!

        # Provider becomes ready later via series shared embedding_provider
        ctx.provider_manager.inst_map["emb-shared"] = provider_inst
        await svc.initialize()
        assert svc._provider is provider_inst
        assert svc._faiss_available is True
        assert len(created_clients) == 1

        # First retrieve raises sqlite3.OperationalError("no active connection") -> auto-rebuilds & succeeds
        hits = await svc.search("开心", k=5)
        assert hits == [("/memes/cat.png", 0.92)]
        assert len(created_clients) == 2
        assert created_clients[0].closed is True
        assert created_clients[1].init_calls == 1

        # Insert raising "Connection closed" -> auto-rebuilds & succeeds
        ok = await svc.insert_emoji("/memes/dog.png", {"desc": "小狗", "category": "happy"})
        assert ok is True
        assert len(created_clients) == 3

        await svc.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 4. Series contracts: scope toggle, activate_series_runtime, artwork, top memes, shared models
# ---------------------------------------------------------------------------


def test_series_scope_toggle_optimistic_lock_and_activate_runtime():
    from astrbot_plugin_visual_media.core.config.config import PluginConfig
    from tl.rate_limiter import RateLimiter

    VisualMediaPlugin = _load_visual_media_plugin_class()

    class _SaveableConfig(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.saves = 0

        def save_config(self, updates=None):
            if isinstance(updates, dict):
                self.update(updates)
            self.saves += 1

    raw_cfg = _SaveableConfig({"generation": {"limit_settings": {}}, "memes": {}})
    plugin = VisualMediaPlugin.__new__(VisualMediaPlugin)
    plugin._closed = False
    plugin._scope_version_seq = 0
    plugin._scope_mutation_lock = asyncio.Lock()
    plugin._config_save_lock = __import__("threading").RLock()
    plugin.raw_config = raw_cfg
    plugin.context = SimpleNamespace(get_all_stars=lambda: {})

    gen_cfg = SimpleNamespace(group_limit_mode="none", group_limit_list=set())
    plugin.generation = SimpleNamespace(
        cfg=gen_cfg,
        rate_limiter=RateLimiter(gen_cfg),
        api_client=object(),
        generation_tracker=SimpleNamespace(records_snapshot=lambda: []),
        _ensure_api_client=lambda quiet=False: True,
    )

    meme_cfg = PluginConfig.__new__(PluginConfig)
    object.__setattr__(meme_cfg, "__dict__", {})
    object.__setattr__(meme_cfg, "__pydantic_fields_set__", set())
    object.__setattr__(meme_cfg, "__pydantic_extra__", None)
    object.__setattr__(meme_cfg, "__pydantic_private__", None)
    for k, v in {
        "auto_send_meme": False,
        "send_target_whitelist": [],
        "send_target_blacklist": [],
        "send_target_filter_mode": "whitelist_first",
        "steal_target_whitelist": [],
        "steal_target_blacklist": [],
        "steal_target_filter_mode": "whitelist_first",
    }.items():
        object.__setattr__(meme_cfg, k, v)
    meme_cfg._refresh_target_policy_cache()

    def _update_memes(updates: dict):
        for k, v in updates.items():
            object.__setattr__(meme_cfg, k, v)
        meme_cfg._refresh_target_policy_cache()
        raw_cfg["memes"].update(updates)
        return True

    plugin.memes = SimpleNamespace(
        plugin_config=meme_cfg,
        plugin_api=SimpleNamespace(_get_index=lambda: {}),
        update_config=_update_memes,
    )

    umo = "aiocqhttp:GroupMessage:88888"

    async def _run():
        st0 = plugin.get_series_scope_status(umo)
        assert st0["umo"] == umo
        assert st0["enabled"] is True
        assert st0["writable"] is True
        assert st0["mode"] == "both"
        assert st0["detail"] == st0["summary"]

        # Version conflict rejected
        conflict = await plugin.set_series_scope_enabled(umo, False, expected_version="stale-ver")
        assert conflict["ok"] is False
        assert conflict["conflict"] is True
        assert conflict["reason"] == "version_conflict"
        assert conflict["version"] == st0["version"]

        # Disable with matching version
        res_off = await plugin.set_series_scope_enabled(umo, False, expected_version=st0["version"])
        assert res_off["ok"] is True
        assert res_off["enabled"] is False
        assert res_off["version"] != st0["version"]
        assert "88888" in gen_cfg.group_limit_list
        assert gen_cfg.group_limit_mode == "blacklist"
        assert "group:88888" in meme_cfg.send_target_blacklist
        assert raw_cfg.saves >= 1

        st1 = plugin.get_series_scope_status(umo)
        assert st1["enabled"] is False
        assert st1["mode"] == "disabled"

        # Re-enable with updated version
        res_on = await plugin.set_series_scope_enabled(umo, True, expected_version=st1["version"])
        assert res_on["ok"] is True
        assert res_on["enabled"] is True
        assert "88888" not in gen_cfg.group_limit_list
        assert "group:88888" not in meme_cfg.send_target_blacklist

        # activate_series_runtime enables auto_send_meme
        act = plugin.activate_series_runtime()
        assert act["ok"] is True
        assert act["enabled"] is True
        assert act["auto_send_meme"] is True
        assert meme_cfg.auto_send_meme is True

    asyncio.run(_run())


def test_cross_plugin_artwork_and_top_memes_and_shared_provider_inheritance(tmp_path, monkeypatch):
    from core.processing.natural_emotion_analyzer import NaturalEmotionAnalyzer
    from core.processing.vlm_call_service import VLMCallService

    try:
        from tl.vision_handler import VisionHandler
    except ImportError:
        fake_splitter = types.ModuleType("tl.image_splitter")
        fake_splitter.split_image = lambda *a, **k: []
        monkeypatch.setitem(sys.modules, "tl.image_splitter", fake_splitter)
        from tl.vision_handler import VisionHandler

    VisualMediaPlugin = _load_visual_media_plugin_class()

    ref_file = tmp_path / "ref.png"
    ref_file.write_bytes(b"\x89PNG\r\n\x1a\n")
    out_file = tmp_path / "art.png"
    out_file.write_bytes(b"\x89PNG\r\n\x1a\n")

    class _ChatOwner:
        def get_series_shared_config(self):
            return {
                "reply_provider": "shared-reply",
                "light_analysis_provider": "shared-light",
                "vision_provider": "shared-vision",
                "embedding_provider": "shared-embed",
            }

    ctx = SimpleNamespace(
        get_all_stars=lambda: {
            "cd": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics", activated=True, star_cls=_ChatOwner()
            )
        },
        get_config=lambda: {},
        get_current_chat_provider_id=AsyncMock(return_value="session-fallback"),
    )

    # 1. Shared provider inheritance
    analyzer = NaturalEmotionAnalyzer.__new__(NaturalEmotionAnalyzer)
    analyzer.plugin = SimpleNamespace(
        plugin_config=SimpleNamespace(emotion_analysis_provider_id=""),
        context=ctx,
    )
    assert asyncio.run(analyzer._get_text_provider(_DummyEvent())) == "shared-light"

    vlm = VLMCallService(SimpleNamespace(context=ctx, plugin_config=SimpleNamespace(vision_provider_id="")))
    assert asyncio.run(vlm._resolve_vision_provider()) == "shared-vision"

    vh = VisionHandler(context=ctx, vision_provider_id="")
    assert vh.effective_vision_provider_id() == "shared-vision"

    # 2. Cross-plugin generate_series_artwork & get_group_top_memes
    submitted_kwargs: dict = {}

    class _FakeGenService:
        async def submit(self, **kwargs):
            submitted_kwargs.update(kwargs)
            return {"task_id": "t-art-1", "status": "queued"}

        async def wait_task(self, task_id, *, plugin_id):
            return {"status": "succeeded", "image_paths": [str(out_file)], "image_urls": []}

    plugin = VisualMediaPlugin.__new__(VisualMediaPlugin)
    plugin._closed = False
    plugin.context = ctx
    plugin.library = SimpleNamespace(
        profiles=lambda: [
            {
                "key": "xiaoqing",
                "name": "小晴",
                "appearance": "短发蓝瞳",
                "style": "清新水彩",
                "references": ["hash-ref-1"],
            }
        ],
        reference_path=lambda h: ref_file,
    )
    plugin.generation = SimpleNamespace(
        rate_limiter=SimpleNamespace(allows_group=lambda ev: True),
        get_service=lambda api_version=1: _FakeGenService(),
        generation_tracker=SimpleNamespace(get=lambda jid: None),
    )
    plugin.memes = SimpleNamespace(
        plugin_api=SimpleNamespace(
            _get_index=lambda: {
                "/m/g1_top.png": {
                    "hash": "h1",
                    "category": "happy",
                    "desc": "加班摸鱼猫",
                    "overlay_text": "摸鱼",
                    "tags": ["摸鱼"],
                    "use_count": 15,
                    "scope_mode": "local",
                    "origin_target": "umo:aiocqhttp:GroupMessage:111",
                },
                "/m/g2_secret.png": {
                    "hash": "h2",
                    "category": "happy",
                    "desc": "摸鱼私有图",
                    "use_count": 99,
                    "scope_mode": "local",
                    "origin_target": "umo:aiocqhttp:GroupMessage:222",
                },
                "/m/public_mid.png": {
                    "hash": "h3",
                    "category": "troll",
                    "desc": "公共摸鱼梗",
                    "use_count": 8,
                    "scope_mode": "public",
                    "origin_target": "",
                },
            }
        )
    )

    art = asyncio.run(
        plugin.generate_series_artwork(
            "海边落日散步",
            persona_id="xiaoqing",
            umo="aiocqhttp:GroupMessage:111",
            style="polaroid",
            source_plugin="astrbot_plugin_dynamics_world",
        )
    )
    assert art["ok"] is True
    assert art["image_path"] == str(out_file)
    assert submitted_kwargs["use_rate_limit"] is True
    assert submitted_kwargs["plugin_id"] == "astrbot_plugin_dynamics_world"
    assert submitted_kwargs["reference_images"] == [str(ref_file)]
    assert "拍立得" in submitted_kwargs["prompt"]
    assert "短发蓝瞳" in submitted_kwargs["prompt"]

    # Empty prompt safely returns ok=False
    bad_art = asyncio.run(plugin.generate_series_artwork(""))
    assert bad_art["ok"] is False and bad_art["error"]

    # get_group_top_memes respects group isolation & ranks properly
    top = plugin.get_group_top_memes("aiocqhttp:GroupMessage:111", keyword="摸鱼", limit=5)
    hashes = [item["hash"] for item in top]
    assert hashes == ["h1", "h3"]
    assert "h2" not in hashes


def test_workshop_frontend_shared_scope_wiring():
    for rel in (
        "views/workshop/workshop.js",
        "views/workshop/memes/app.js",
        "views/workshop/behavior/behavior.js",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "getSharedScope" in text, rel
        assert "setSharedScope" in text, rel
        assert "dynamics-scopechange" in text, rel

    behavior_js = (ROOT / "views/workshop/behavior/behavior.js").read_text(encoding="utf-8")
    assert "scope_label" in behavior_js
    assert "member_name" in behavior_js
