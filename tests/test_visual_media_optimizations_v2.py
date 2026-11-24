"""Regression tests for visual_media v2 lifecycle, telemetry, openai_compat, batch upload, retention, and health."""

from __future__ import annotations

import asyncio
import base64
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _GroupDeco:
    def __call__(self, fn):
        fn.command = lambda *a, **kw: (lambda inner: inner)
        return fn

    def command(self, *args, **kwargs):
        return lambda fn: fn


class _FilterStub:
    def __getattr__(self, _name):
        return lambda *a, **kw: (lambda fn: fn)

    def command_group(self, *args, **kwargs):
        return _GroupDeco()


def _ensure_astrbot_stubs() -> None:
    """Fill missing astrbot stub submodules when running under tests/conftest.py."""
    if getattr(sys.modules.get("astrbot"), "__spec__", None) is not None:
        return
    api_mod = sys.modules.get("astrbot.api")
    if api_mod is not None and not hasattr(api_mod, "__path__"):
        api_mod.__path__ = []
    if api_mod is not None and not hasattr(api_mod, "AstrBotConfig"):
        api_mod.AstrBotConfig = dict

    for name in (
        "astrbot.api.web",
        "astrbot.api.star",
        "astrbot.api.event",
        "astrbot.api.event.filter",
        "astrbot.api.message_components",
        "astrbot.api.provider",
        "astrbot.core",
        "astrbot.core.star",
        "astrbot.core.star.star",
        "astrbot.core.utils",
        "astrbot.core.utils.astrbot_path",
        "astrbot.core.agent",
        "astrbot.core.agent.run_context",
        "astrbot.core.agent.tool",
        "astrbot.core.astr_agent_context",
        "astrbot.core.platform",
        "astrbot.core.platform.message_session",
    ):
        mod = sys.modules.setdefault(name, types.ModuleType(name))
        if not hasattr(mod, "__path__"):
            mod.__path__ = []

    web_mod = sys.modules["astrbot.api.web"]
    if not hasattr(web_mod, "json_response"):
        web_mod.json_response = lambda payload, status_code=200: (payload, status_code)
    if not hasattr(web_mod, "request"):
        web_mod.request = SimpleNamespace()

    star_mod = sys.modules["astrbot.api.star"]
    if not hasattr(star_mod, "Context"):
        star_mod.Context = type("Context", (), {})
    if not hasattr(star_mod, "Star"):
        class _Star:
            def __init__(self, context=None):
                self.context = context

        star_mod.Star = _Star
    if not hasattr(star_mod.Star, "html_render"):
        async def _html_render(self, *args, **kwargs):
            return ""

        star_mod.Star.html_render = _html_render
    if not hasattr(star_mod.Star, "text_to_image"):
        async def _text_to_image(self, *args, **kwargs):
            return ""

        star_mod.Star.text_to_image = _text_to_image
    if not hasattr(star_mod, "StarTools"):
        star_mod.StarTools = SimpleNamespace(get_data_dir=lambda name: Path("/tmp") / name)

    filter_mod = sys.modules["astrbot.api.event.filter"]
    if not hasattr(filter_mod, "EventMessageType") or not hasattr(
        filter_mod.EventMessageType, "ALL"
    ):
        filter_mod.EventMessageType = SimpleNamespace(ALL="all")
    if not hasattr(filter_mod, "PermissionType") or not hasattr(
        filter_mod.PermissionType, "ADMIN"
    ):
        filter_mod.PermissionType = SimpleNamespace(ADMIN="admin")
    if not hasattr(filter_mod, "PlatformAdapterType") or not hasattr(
        filter_mod.PlatformAdapterType, "ALL"
    ):
        filter_mod.PlatformAdapterType = SimpleNamespace(ALL="all")

    event_mod = sys.modules["astrbot.api.event"]
    if not hasattr(event_mod, "AstrMessageEvent"):
        event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
    if not hasattr(event_mod, "MessageChain"):
        event_mod.MessageChain = type("MessageChain", (), {})
    if not hasattr(event_mod, "filter") or not hasattr(event_mod.filter, "llm_tool"):
        event_mod.filter = _FilterStub()

    prov_mod = sys.modules["astrbot.api.provider"]
    if not hasattr(prov_mod, "ProviderRequest"):
        prov_mod.ProviderRequest = type("ProviderRequest", (), {})

    comp_mod = sys.modules["astrbot.api.message_components"]
    for cname in ("Image", "Node", "Plain", "At", "Reply", "Face", "File", "Video"):
        if not hasattr(comp_mod, cname):
            setattr(comp_mod, cname, type(cname, (), {}))

    core_star = sys.modules["astrbot.core.star.star"]
    if not hasattr(core_star, "star_registry"):
        core_star.star_registry = []

    path_mod = sys.modules["astrbot.core.utils.astrbot_path"]
    if not hasattr(path_mod, "get_astrbot_plugin_data_path"):
        path_mod.get_astrbot_plugin_data_path = lambda: "/tmp/astrbot_plugin_data"

    class _FunctionTool:
        @classmethod
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, *args, **kwargs):
            return None

    run_ctx = sys.modules["astrbot.core.agent.run_context"]
    if not hasattr(run_ctx, "ContextWrapper"):
        run_ctx.ContextWrapper = type("ContextWrapper", (), {})
    tool_mod = sys.modules["astrbot.core.agent.tool"]
    if not hasattr(tool_mod, "FunctionTool"):
        tool_mod.FunctionTool = _FunctionTool
    if not hasattr(tool_mod, "ToolExecResult"):
        tool_mod.ToolExecResult = type("ToolExecResult", (), {})
    agent_ctx = sys.modules["astrbot.core.astr_agent_context"]
    if not hasattr(agent_ctx, "AstrAgentContext"):
        agent_ctx.AstrAgentContext = type("AstrAgentContext", (), {})
    msg_sess = sys.modules["astrbot.core.platform.message_session"]
    if not hasattr(msg_sess, "MessageSession"):
        msg_sess.MessageSession = type("MessageSession", (), {})

    if "mcp" not in sys.modules:
        mcp_mod = types.ModuleType("mcp")
        mcp_types = types.ModuleType("mcp.types")
        mcp_mod.types = mcp_types
        sys.modules["mcp"] = mcp_mod
        sys.modules["mcp.types"] = mcp_types


_ensure_astrbot_stubs()


class _SafeStubLogger:
    def debug(self, *args, **kwargs):
        return None

    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def exception(self, *args, **kwargs):
        return None


@pytest.fixture(autouse=True)
def _ensure_safe_stub_logger(monkeypatch):
    """Guard against earlier standalone tests replacing astrbot.api.logger or tl package with minimal dummies."""
    if getattr(sys.modules.get("astrbot"), "__spec__", None) is not None:
        return
    _ensure_astrbot_stubs()
    qualified_tl = sys.modules.get("astrbot_plugin_visual_media.tl")
    if (
        qualified_tl is not None
        and getattr(qualified_tl, "__spec__", None) is None
        and not hasattr(qualified_tl, "AvatarHandler")
    ):
        monkeypatch.delitem(
            sys.modules, "astrbot_plugin_visual_media.tl", raising=False
        )
    safe = _SafeStubLogger()
    api_mod = sys.modules.get("astrbot.api")
    if api_mod is not None:
        monkeypatch.setattr(api_mod, "logger", safe, raising=False)
    for mod_name in (
        "core.search.embedding_service",
        "astrbot_plugin_visual_media.core.search.embedding_service",
        "task_scheduler",
        "astrbot_plugin_visual_media.task_scheduler",
        "astrbot_plugin_visual_media.api.image_mutations",
        "astrbot_plugin_visual_media.generation_engine",
        "astrbot_plugin_visual_media.main",
    ):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "logger"):
            monkeypatch.setattr(mod, "logger", safe, raising=False)


# ---------------------------------------------------------------------------
# P0: Embedding cold-start silent lookup & post-load initialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedding_find_provider_silently_avoids_get_provider_by_id_warn(tmp_path):
    from core.search.embedding_service import EmbeddingService

    warn_calls: list[str] = []

    class _ColdContext:
        provider_manager = SimpleNamespace(
            inst_map={},
            embedding_provider_inst_map={},
            embedding_provider_insts=[],
        )

        def get_all_providers(self):
            return []

        def get_all_embedding_providers(self):
            return []

        def get_provider_by_id(self, pid: str):
            warn_calls.append(pid)
            return None

    plugin = SimpleNamespace(
        context=_ColdContext(),
        enable_embedding_search=True,
        embedding_provider_id="emb-cold",
        plugin_config=SimpleNamespace(data_dir=str(tmp_path)),
        db_service=None,
    )
    svc = EmbeddingService(plugin)
    await svc.initialize()

    # Must NEVER call context.get_provider_by_id during cold start when provider_manager exists.
    assert warn_calls == []
    assert svc._initialized is False

    # Once provider appears in provider_manager, silent lookup finds it without calling get_provider_by_id.
    fake_provider = SimpleNamespace(
        provider_config={"id": "emb-cold"},
        get_dim=lambda: 4,
        get_model=lambda: "text-embedding-3-small",
        get_embedding=AsyncMock(return_value=[1.0, 0.0, 0.0, 0.0]),
        get_embeddings=AsyncMock(return_value=[[1.0, 0.0, 0.0, 0.0]]),
    )
    plugin.context.provider_manager.embedding_provider_inst_map["emb-cold"] = fake_provider
    assert svc._find_provider_by_id("emb-cold") is fake_provider
    assert warn_calls == []


@pytest.mark.asyncio
async def test_main_on_astrbot_loaded_completes_embedding_init():
    from astrbot_plugin_visual_media.main import VisualMediaPlugin
    from astrbot_plugin_visual_media.task_scheduler import TaskScheduler

    plugin = VisualMediaPlugin.__new__(VisualMediaPlugin)
    plugin.generation = SimpleNamespace(on_astrbot_loaded=AsyncMock(return_value="gen-ok"))

    init_calls = 0

    class _FakeEmbedding:
        def __init__(self):
            self._initialized = False

        async def initialize(self):
            nonlocal init_calls
            init_calls += 1
            self._initialized = True

    emb = _FakeEmbedding()
    scheduler = TaskScheduler()
    plugin.memes = SimpleNamespace(
        embedding_service=emb,
        plugin_config=SimpleNamespace(enable_embedding_search=True),
        task_scheduler=scheduler,
    )

    res = await plugin.on_astrbot_loaded()
    assert res == "gen-ok"
    # Wait for scheduled embedding_initialize task to finish.
    pending = list(scheduler._tasks.values())
    if pending:
        await asyncio.gather(*pending)
    assert init_calls == 1
    assert emb._initialized is True

    # Calling on_astrbot_loaded again once already initialized must NOT re-trigger initialization.
    await plugin.on_astrbot_loaded()
    assert init_calls == 1
    await scheduler.cleanup()


# ---------------------------------------------------------------------------
# P0 & P2: GenerationEngine duplicate init elimination, task tracking & clear_api_client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generation_on_astrbot_loaded_skips_duplicate_init_and_terminate_clears_singleton(
    monkeypatch,
):
    from astrbot_plugin_visual_media.generation_engine import GeminiImageGenerationPlugin
    from astrbot_plugin_visual_media.tl import tl_api

    tl_api.clear_api_client()
    singleton = tl_api.get_api_client(["k1"])
    assert tl_api._api_client is singleton

    plugin = GeminiImageGenerationPlugin.__new__(GeminiImageGenerationPlugin)
    plugin.api_client = singleton
    plugin.cfg = SimpleNamespace(help_render_mode="local", proxy=None)
    plugin.llm_image_tool = SimpleNamespace(refresh_from_plugin=lambda: None)
    plugin.llm_batch_tool = SimpleNamespace(refresh_from_plugin=lambda: None)
    plugin._background_tasks = set()

    load_calls: list[bool] = []
    plugin._load_api_client_from_config = lambda *, quiet=False: load_calls.append(quiet)
    invalidate_calls: list[bool] = []
    monkeypatch.setattr(singleton, "invalidate_session", lambda: invalidate_calls.append(True))

    font_started = asyncio.Event()
    font_release = asyncio.Event()

    async def _slow_font():
        font_started.set()
        await font_release.wait()

    plugin._ensure_font_for_local_mode = _slow_font

    await plugin.on_astrbot_loaded()
    assert load_calls == []
    assert invalidate_calls == []
    await asyncio.wait_for(font_started.wait(), timeout=1.0)
    assert len(plugin._background_tasks) == 1

    # Wire minimal collaborators for terminate()
    plugin._web_closed = False
    plugin.provider_runtime = SimpleNamespace(closed=False)
    plugin._public_service = SimpleNamespace(close=AsyncMock())
    plugin.generation_scheduler = SimpleNamespace(close=AsyncMock())
    plugin.studio_providers = None
    plugin.studio_templates = None
    plugin.configuration_lock = asyncio.Lock()
    plugin.web_studio_service = SimpleNamespace(close=AsyncMock())
    plugin._web_routes = []
    plugin._web_api = None
    plugin.background_task_manager = SimpleNamespace(close=AsyncMock())
    plugin._v2_service = None
    plugin.generation_tracker = SimpleNamespace(close=AsyncMock())
    plugin.rate_limiter = SimpleNamespace(close=AsyncMock())

    font_task = next(iter(plugin._background_tasks))
    await plugin.terminate()

    assert font_task.cancelled()
    assert not plugin._background_tasks
    assert plugin.api_client is None
    assert tl_api._api_client is None


@pytest.mark.asyncio
async def test_generation_on_astrbot_loaded_loads_when_api_client_is_none():
    from astrbot_plugin_visual_media.generation_engine import GeminiImageGenerationPlugin

    plugin = GeminiImageGenerationPlugin.__new__(GeminiImageGenerationPlugin)
    plugin.api_client = None
    plugin.cfg = SimpleNamespace(help_render_mode="remote", proxy=None)
    refresh_hits = 0

    def _refresh():
        nonlocal refresh_hits
        refresh_hits += 1

    plugin.llm_image_tool = SimpleNamespace(refresh_from_plugin=_refresh)
    plugin.llm_batch_tool = SimpleNamespace(refresh_from_plugin=_refresh)
    plugin._background_tasks = set()

    load_calls: list[bool] = []

    def _fake_load(*, quiet: bool = False):
        load_calls.append(quiet)
        plugin.api_client = object()

    plugin._load_api_client_from_config = _fake_load

    await plugin.on_astrbot_loaded()
    assert load_calls == [True]
    assert refresh_hits == 2


# ---------------------------------------------------------------------------
# P1: Image generation / edit telemetry reporting via series_panels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tl_api_reports_series_telemetry_for_generate_edit_and_error(monkeypatch):
    from tl.api_types import APIError, ApiRequestConfig
    from tl.tl_api import GeminiAPIClient

    recorded: list[dict] = []

    class _FakeTelemetry:
        def record(self, **kwargs):
            recorded.append(kwargs)

    client = GeminiAPIClient(["key-1"])
    client.context = SimpleNamespace(_chat_dynamics_series_telemetry=_FakeTelemetry())

    monkeypatch.setattr(
        client,
        "_get_api_url",
        AsyncMock(return_value=("https://api.example.com/v1/chat/completions", {}, {})),
    )

    async def _ok_make_request(**kwargs):
        cfg = kwargs["config"]
        cfg.token_usage = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
        return (["https://cdn.example.com/1.png", "https://cdn.example.com/2.png"], [], None, None)

    monkeypatch.setattr(client, "_make_request", _ok_make_request)

    gen_cfg = ApiRequestConfig(
        model="gemini-3-pro-image",
        prompt="a cat",
        api_type="openai",
        candidate_id="cand-gen",
    )
    await client._generate_image_single_inner(gen_cfg)

    assert len(recorded) == 1
    first = recorded[0]
    assert first["plugin_id"] == "astrbot_plugin_visual_media"
    assert first["provider_id"] == "cand-gen"
    assert first["purpose"] == "image_generate"
    assert first["model"] == "gemini-3-pro-image"
    assert first["ok"] is True
    assert first["error_type"] == ""
    assert first["image_count"] == 2
    assert first["generation_seconds"] >= 0.0
    assert first["response"] == {
        "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    }

    # Image edit path
    edit_cfg = ApiRequestConfig(
        model="gemini-3-pro-image",
        prompt="make it blue",
        api_type="openai",
        candidate_id="cand-edit",
        reference_images=["/tmp/ref.png"],
    )
    await client._generate_image_single_inner(edit_cfg)
    assert recorded[-1]["purpose"] == "image_edit"
    assert recorded[-1]["provider_id"] == "cand-edit"

    # Error path
    monkeypatch.setattr(
        client,
        "_make_request",
        AsyncMock(side_effect=APIError("quota exceeded", 429, "quota")),
    )
    err_cfg = ApiRequestConfig(
        model="gemini-3-pro-image",
        prompt="fail me",
        api_type="openai",
        candidate_id="cand-err",
    )
    with pytest.raises(APIError):
        await client._generate_image_single_inner(err_cfg)

    last = recorded[-1]
    assert last["provider_id"] == "cand-err"
    assert last["ok"] is False
    assert last["error_type"] == "quota"
    assert last["image_count"] == 0


# ---------------------------------------------------------------------------
# P1: Dead code removal in tl_api.py + 4 fixes in openai_compat.py
# ---------------------------------------------------------------------------


def test_tl_api_dead_parse_openai_response_removed():
    from tl.tl_api import GeminiAPIClient

    assert not hasattr(GeminiAPIClient, "_parse_openai_response")


@pytest.mark.asyncio
async def test_openai_compat_four_fixes(monkeypatch):
    from tl.api import openai_compat
    from tl.api.openai_compat import OpenAICompatProvider
    from tl.api_types import APIError, ApiRequestConfig
    from tl.tl_api import GeminiAPIClient

    provider = OpenAICompatProvider()

    # Fix 1: Redact secret query params in URL logs and secrets in error responses.
    captured_logs: list[str] = []

    class _CaptureLogger:
        def debug(self, msg, *args):
            captured_logs.append(msg % args if args else str(msg))

        def warning(self, msg, *args):
            captured_logs.append(msg % args if args else str(msg))

        def info(self, msg, *args):
            captured_logs.append(msg % args if args else str(msg))

        def error(self, msg, *args):
            captured_logs.append(msg % args if args else str(msg))

    monkeypatch.setattr(openai_compat, "logger", _CaptureLogger())

    client = GeminiAPIClient(["k"])
    client.proxy = None
    secret_url = "https://img.example.com/a.png?key=sk-secret-token-1234567890"
    urls, paths, _, _ = await provider.parse_response(
        client=client,
        response_data={
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "image_url", "image_url": {"url": secret_url}}
                        ]
                    }
                }
            ]
        },
        session=None,
        request_config=ApiRequestConfig(model="m", prompt="p"),
    )
    assert urls == [secret_url]
    assert paths == []
    assert any("img.example.com" in line for line in captured_logs)
    assert all("sk-secret-token-1234567890" not in line for line in captured_logs)

    # Error response text redaction
    captured_logs.clear()
    with pytest.raises(APIError) as exc_info:
        await provider.parse_response(
            client=client,
            response_data={
                "choices": [
                    {
                        "message": {
                            "content": "Denied with Authorization: Bearer sk-topsecret99999999999999 and api_key=leak123"
                        }
                    }
                ]
            },
            session=None,
            request_config=ApiRequestConfig(model="m", prompt="p"),
        )
    assert "sk-topsecret99999999999999" not in exc_info.value.message
    assert "leak123" not in exc_info.value.message
    assert all("sk-topsecret99999999999999" not in line for line in captured_logs)
    assert all("leak123" not in line for line in captured_logs)

    # Fix 2: Guard prevents calling _extract_from_content when image_candidates already extracted images.
    extract_mock = AsyncMock(return_value=(["https://dup.example.com/dup.png"], []))
    monkeypatch.setattr(client, "_extract_from_content", extract_mock)
    urls2, _, _, _ = await provider.parse_response(
        client=client,
        response_data={
            "choices": [
                {
                    "message": {
                        "content": "Here is your generated image.",
                        "images": [
                            {"image_url": {"url": "https://img.example.com/one.png"}}
                        ],
                    }
                }
            ]
        },
        session=None,
        request_config=ApiRequestConfig(model="m", prompt="p"),
    )
    assert extract_mock.await_count == 0
    assert urls2 == ["https://img.example.com/one.png"]

    # Fix 3: Global client.proxy forces _download_image even when request_config.proxy is None.
    client.proxy = "http://127.0.0.1:7890"
    dl_calls: list[tuple[str, str | None]] = []

    async def _fake_download(url, session, *, use_cache=False, proxy=None):
        dl_calls.append((url, proxy))
        return ("/tmp/downloaded.png", "/tmp/downloaded.png")

    monkeypatch.setattr(client, "_download_image", _fake_download)
    urls3, paths3, _, _ = await provider.parse_response(
        client=client,
        response_data={
            "choices": [
                {
                    "message": {
                        "images": [
                            {"image_url": {"url": "https://remote.example.com/pic.png"}}
                        ]
                    }
                }
            ]
        },
        session=None,
        request_config=ApiRequestConfig(model="m", prompt="p", proxy=None),
    )
    assert dl_calls == [("https://remote.example.com/pic.png", "http://127.0.0.1:7890")]
    assert paths3 == ["/tmp/downloaded.png"]
    assert urls3 == ["/tmp/downloaded.png"]

    # Fix 4: Raw string r"\s+" matches and strips actual whitespace/newlines in loose data:image base64.
    client.proxy = None
    monkeypatch.setattr(client, "_extract_from_content", AsyncMock(return_value=([], [])))
    saved_b64: list[tuple[str, str]] = []

    async def _fake_save_b64(b64_data: str, fmt: str):
        saved_b64.append((b64_data, fmt))
        return "/tmp/loose.png"

    monkeypatch.setattr(openai_compat, "save_base64_image", _fake_save_b64)
    multiline_b64 = "iVBORw0K\n  GgoAAAANS\tUhEUg=="
    urls4, paths4, _, _ = await provider.parse_response(
        client=client,
        response_data={
            "choices": [
                {
                    "message": {
                        "content": f"Generated:\ndata:image/png;base64,{multiline_b64}"
                    }
                }
            ]
        },
        session=None,
        request_config=ApiRequestConfig(model="m", prompt="p"),
    )
    assert saved_b64 == [("iVBORw0KGgoAAAANSUhEUg==", "png")]
    assert paths4 == ["/tmp/loose.png"]
    assert urls4 == ["/tmp/loose.png"]


# ---------------------------------------------------------------------------
# P2: Managed background task sets & cancellation on close()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracked_background_tasks_cancelled_on_close(tmp_path):
    from core.search.embedding_service import EmbeddingService
    from tl.prompt_templates import PromptTemplateStore
    from tl.studio_templates import StudioTemplateService
    from tl.tl_api import GeminiAPIClient

    # 1. GeminiAPIClient.invalidate_session + close
    client = GeminiAPIClient(["k"])
    close_started = asyncio.Event()
    close_gate = asyncio.Event()

    class _SlowSession:
        closed = False

        async def close(self):
            close_started.set()
            await close_gate.wait()

    client._session = _SlowSession()
    client.invalidate_session()
    await asyncio.wait_for(close_started.wait(), timeout=1.0)
    assert len(client._bg_tasks) == 1
    task_api = next(iter(client._bg_tasks))
    await client.close()
    assert task_api.cancelled()
    assert not client._bg_tasks

    # 2. StudioTemplateService.reconfigure_hub + close
    source_close_started = asyncio.Event()
    source_close_gate = asyncio.Event()

    class _SlowSource:
        def __init__(self, url: str, key: str | None):
            self.base_url = url
            self.api_key = key

        async def close(self):
            source_close_started.set()
            await source_close_gate.wait()

    store = PromptTemplateStore(tmp_path)
    svc = StudioTemplateService(
        store,
        tmp_path,
        hub_mode="cache",
        hub_url="https://hub-a.example",
        hub_key="",
        source_factory=_SlowSource,
    )
    svc.reconfigure_hub(url="https://hub-b.example")
    await asyncio.wait_for(source_close_started.wait(), timeout=1.0)
    assert len(svc._bg_tasks) == 1
    task_tpl = next(iter(svc._bg_tasks))
    # Replace active hub_source with instant close stub so close() finishes immediately
    svc.hub_source = SimpleNamespace(close=AsyncMock())
    await svc.close()
    assert task_tpl.cancelled()
    assert not svc._bg_tasks

    # 3. EmbeddingService._schedule_db_refresh + close
    emb = EmbeddingService(SimpleNamespace(context=None, enable_embedding_search=False))
    never = asyncio.Event()
    pending_task = asyncio.create_task(never.wait())
    emb._bg_tasks.add(pending_task)
    pending_task.add_done_callback(emb._bg_tasks.discard)
    await emb.close()
    assert pending_task.cancelled()
    assert not emb._bg_tasks


# ---------------------------------------------------------------------------
# P2: Batch upload file count & total bytes limits, disk spooling & scheduler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_upload_limits_disk_spooling_and_scheduler(tmp_path, monkeypatch):
    from astrbot_plugin_visual_media.api import image_mutations
    from astrbot_plugin_visual_media.task_scheduler import TaskScheduler

    class _FakeAPI(image_mutations.ImageMutationRoutes):
        BATCH_UPLOAD_MAX_FILES = 2
        BATCH_UPLOAD_MAX_TOTAL_BYTES = 16

        def __init__(self, data_dir: Path):
            self._data_dir = data_dir
            self._db = object()
            self._cfg = SimpleNamespace(categories=["happy"])
            self.batch_upload_tasks = {}
            self.persisted: list[bytes] = []
            self.plugin = SimpleNamespace(
                task_scheduler=TaskScheduler(),
                image_processor_service=None,
            )

        def _prune_batch_upload_tasks(self):
            return 0

        @staticmethod
        def _decode_base64_payload(b64: str) -> bytes:
            return base64.b64decode(b64)

        @staticmethod
        def _is_allowed_ext(ext: str) -> bool:
            return ext in {".png", ".jpg"}

        @staticmethod
        def _normalize_character_key(val: str) -> str:
            return val.strip()

        def _get_category_keys(self) -> list[str]:
            return ["happy"]

        @staticmethod
        def _task_now() -> float:
            return 1.0

        async def _persist_image(self, *, file_content: bytes, category: str, file_hash: str, **kwargs):
            self.persisted.append(file_content)
            return {"hash": file_hash, "category": category}

    monkeypatch.setattr(image_mutations, "jsonify", lambda payload: payload)

    api = _FakeAPI(tmp_path)

    # 1. File count limit exceeded -> 400 and no leftover spool files
    three_files = [
        {"name": f"f{i}.png", "base64": base64.b64encode(b"1234").decode()}
        for i in range(3)
    ]
    monkeypatch.setattr(
        image_mutations,
        "request",
        SimpleNamespace(get_json=AsyncMock(return_value={"_files": three_files, "category": "happy"})),
    )
    resp = await api.handle_batch_upload()
    assert isinstance(resp, tuple) and resp[1] == 400
    assert resp[0]["success"] is False
    temp_root = tmp_path / "temp"
    assert not temp_root.exists() or not any(temp_root.iterdir())

    # 2. Total byte limit exceeded -> 413 and first spooled file cleaned up
    two_big = [
        {"name": "a.png", "base64": base64.b64encode(b"0123456789").decode()},
        {"name": "b.png", "base64": base64.b64encode(b"0123456789").decode()},
    ]
    monkeypatch.setattr(
        image_mutations,
        "request",
        SimpleNamespace(get_json=AsyncMock(return_value={"_files": two_big, "category": "happy"})),
    )
    resp_big = await api.handle_batch_upload()
    assert isinstance(resp_big, tuple) and resp_big[1] == 413
    assert resp_big[0]["success"] is False
    assert not temp_root.exists() or not any(temp_root.iterdir())

    # 3. Valid upload -> spooled to disk, scheduled via task_scheduler, processed & spool dir removed
    captured_names: list[str] = []
    real_create_task = api.plugin.task_scheduler.create_task

    def _spy_create_task(name, coro, replace_existing=True):
        captured_names.append(name)
        return real_create_task(name, coro, replace_existing=replace_existing)

    api.plugin.task_scheduler.create_task = _spy_create_task

    valid_files = [
        {"name": "ok1.png", "base64": base64.b64encode(b"abcd").decode()},
        {"name": "ok2.png", "base64": base64.b64encode(b"efgh").decode()},
    ]
    monkeypatch.setattr(
        image_mutations,
        "request",
        SimpleNamespace(get_json=AsyncMock(return_value={"_files": valid_files, "category": "happy"})),
    )
    resp_ok = await api.handle_batch_upload()
    assert resp_ok["success"] is True
    task_id = resp_ok["task_id"]
    assert captured_names == [f"batch_upload:{task_id}"]

    running = list(api.plugin.task_scheduler._tasks.values())
    if running:
        await asyncio.gather(*running)
    assert api.batch_upload_tasks[task_id]["status"] == "completed"
    assert api.batch_upload_tasks[task_id]["success"] == 2
    assert api.persisted == [b"abcd", b"efgh"]
    assert not (temp_root / f"batch_{task_id}").exists()
    await api.plugin.task_scheduler.cleanup()


# ---------------------------------------------------------------------------
# P2: Retention disk byte limit
# ---------------------------------------------------------------------------


def test_retention_eviction_enforces_max_disk_bytes(tmp_path):
    from core.maintenance.retention import automatic_disk_bytes, eviction_candidates

    p1 = tmp_path / "old_low.png"
    p2 = tmp_path / "mid.png"
    p3 = tmp_path / "hot.png"
    p_fav = tmp_path / "fav.png"
    p1.write_bytes(b"a" * 400)
    p2.write_bytes(b"b" * 400)
    p3.write_bytes(b"c" * 400)
    p_fav.write_bytes(b"d" * 1000)

    index = {
        str(p1): {"use_count": 0, "created_at": 100},
        str(p2): {"use_count": 2, "created_at": 200},
        str(p3): {"use_count": 10, "created_at": 300},
        str(p_fav): {"use_count": 0, "created_at": 1, "is_favorite": True},
    }
    assert automatic_disk_bytes(index) == 1200

    # Count (3) is well under limit (100), but disk bytes (1200) exceed cap (700) -> must evict 2 lowest-priority entries.
    evicted = eviction_candidates(index, limit=100, usage_weight=0.7, max_disk_bytes=700)
    evicted_paths = [p for p, _ in evicted]
    assert evicted_paths == [str(p1), str(p2)]
    assert str(p_fav) not in evicted_paths
    assert str(p3) not in evicted_paths


# ---------------------------------------------------------------------------
# P2: Synchronous read-only get_series_health()
# ---------------------------------------------------------------------------


def test_get_series_health_synchronous_readonly_and_silent():
    from astrbot_plugin_visual_media import series_panels
    from astrbot_plugin_visual_media.main import VisualMediaPlugin

    warn_calls: list[str] = []

    class _Ctx:
        provider_manager = SimpleNamespace(inst_map={}, embedding_provider_inst_map={})

        def get_all_embedding_providers(self):
            return []

        def get_provider_by_id(self, pid: str):
            warn_calls.append(pid)
            return None

    plugin = VisualMediaPlugin.__new__(VisualMediaPlugin)
    plugin._closed = False
    plugin.context = _Ctx()
    plugin.generation = SimpleNamespace(
        api_client=object(),
        cfg=SimpleNamespace(
            provider_candidates=[SimpleNamespace(id="gemini-main", api_type="google")]
        ),
    )
    plugin.memes = SimpleNamespace(
        plugin_config=SimpleNamespace(
            enable_embedding_search=True,
            embedding_provider_id="emb-missing",
        ),
        embedding_service=SimpleNamespace(
            _provider=None,
            _initialized=False,
            _faiss_available=None,
            _fallback_loaded=False,
        ),
        plugin_api=SimpleNamespace(_get_index=lambda: {"a.png": {}}),
    )

    health = series_panels.read_plugin_health("astrbot_plugin_visual_media", plugin)
    assert health is not None
    assert health["plugin_id"] == "astrbot_plugin_visual_media"
    assert health["ready"] is True
    assert health["degraded"] is True
    assert "embedding_provider_unavailable" in health["issues"]
    assert warn_calls == []

    roles = {item["role"]: item for item in health["providers"]}
    assert roles["image_generation"]["status"] == "ok"
    assert roles["image_generation"]["provider_id"] == "gemini-main"
    assert roles["embedding"]["status"] == "missing"
    assert health["metrics"]["meme_count"] == 1
    assert health["metrics"]["generation_client_ready"] is True
