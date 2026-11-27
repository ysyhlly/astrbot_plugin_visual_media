"""Focused unit tests for 2GB RAM server performance optimizations in astrbot_plugin_visual_media."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
REPOS_PARENT = REPO_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPOS_PARENT) not in sys.path:
    sys.path.insert(0, str(REPOS_PARENT))


def _ensure_astrbot_stubs() -> None:
    """Ensure package-compatible AstrBot stubs exist even when running this file standalone."""
    astrbot = sys.modules.get("astrbot")
    if astrbot is None:
        astrbot = types.ModuleType("astrbot")
        sys.modules["astrbot"] = astrbot
    if not hasattr(astrbot, "__path__"):
        astrbot.__path__ = []  # type: ignore[attr-defined]

    api = sys.modules.get("astrbot.api")
    if api is None:
        api = types.ModuleType("astrbot.api")
        api.logger = SimpleNamespace(
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None,
        )
        sys.modules["astrbot.api"] = api
    if not hasattr(api, "__path__"):
        api.__path__ = []  # type: ignore[attr-defined]
    astrbot.api = api

    if "astrbot.api.event" not in sys.modules:
        ev = types.ModuleType("astrbot.api.event")
        ev.AstrMessageEvent = type("AstrMessageEvent", (), {})
        ev.MessageChain = type("MessageChain", (), {})
        sys.modules["astrbot.api.event"] = ev
    if "astrbot.api.message_components" not in sys.modules:
        mc = types.ModuleType("astrbot.api.message_components")
        mc.At = type("At", (), {})
        mc.Image = type("Image", (), {})
        mc.Plain = type("Plain", (), {})
        mc.Reply = type("Reply", (), {})
        sys.modules["astrbot.api.message_components"] = mc
    if "astrbot.api.provider" not in sys.modules:
        prov = types.ModuleType("astrbot.api.provider")
        prov.ProviderRequest = type("ProviderRequest", (), {})
        sys.modules["astrbot.api.provider"] = prov
    if "astrbot.api.star" not in sys.modules:
        star = types.ModuleType("astrbot.api.star")
        star.Context = type("Context", (), {})
        star.Star = type("Star", (), {})
        star.StarTools = SimpleNamespace(get_data_dir=lambda _n: REPO_ROOT)
        sys.modules["astrbot.api.star"] = star


def _make_png_bytes(width: int = 16, height: int = 16, mode: str = "RGBA") -> bytes:
    color = (200, 100, 50, 255) if mode == "RGBA" else (200, 100, 50)
    buf = io.BytesIO()
    Image.new(mode, (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def test_no_eager_cv2_or_image_splitter_import_on_startup() -> None:
    """Importing core tl modules and image_processor_service must NOT pull in cv2 or image_splitter."""
    code = """
import sys, types
astrbot = types.ModuleType("astrbot")
astrbot.__path__ = []
api = types.ModuleType("astrbot.api")
api.__path__ = []
api.logger = types.SimpleNamespace(
    debug=lambda *a, **k: None,
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
)
ev = types.ModuleType("astrbot.api.event")
ev.AstrMessageEvent = type("AstrMessageEvent", (), {})
ev.MessageChain = type("MessageChain", (), {})
mc = types.ModuleType("astrbot.api.message_components")
mc.At = type("At", (), {})
mc.Image = type("Image", (), {})
mc.Plain = type("Plain", (), {})
mc.Reply = type("Reply", (), {})
mc.Node = type("Node", (), {})
mc.Nodes = type("Nodes", (), {})
mc.File = type("File", (), {})
prov = types.ModuleType("astrbot.api.provider")
prov.ProviderRequest = type("ProviderRequest", (), {})
star = types.ModuleType("astrbot.api.star")
star.Context = type("Context", (), {})
star.Star = type("Star", (), {})
star.StarTools = types.SimpleNamespace(get_data_dir=lambda _n: ".")
core_mod = types.ModuleType("astrbot.core")
core_mod.__path__ = []
core_agent = types.ModuleType("astrbot.core.agent")
core_agent.__path__ = []
core_tool = types.ModuleType("astrbot.core.agent.tool")
from typing import Generic, TypeVar
_T = TypeVar("_T")
class _FT(Generic[_T]):
    pass
core_tool.FunctionTool = _FT
core_tool.ToolExecResult = object
core_run = types.ModuleType("astrbot.core.agent.run_context")
class _CW(Generic[_T]):
    pass
core_run.ContextWrapper = _CW
core_star = types.ModuleType("astrbot.core.star")
core_star.__path__ = []
core_star_ctx = types.ModuleType("astrbot.core.star.context")
core_star_ctx.AstrAgentContext = type("AstrAgentContext", (), {})
core_agent_ctx = types.ModuleType("astrbot.core.astr_agent_context")
core_agent_ctx.AstrAgentContext = type("AstrAgentContext", (), {})
core_utils = types.ModuleType("astrbot.core.utils")
core_utils.__path__ = []
core_path = types.ModuleType("astrbot.core.utils.astrbot_path")
core_path.get_astrbot_temp_path = lambda: "."
astrbot.api = api
sys.modules.update({
    "astrbot": astrbot,
    "astrbot.api": api,
    "astrbot.api.event": ev,
    "astrbot.api.message_components": mc,
    "astrbot.api.provider": prov,
    "astrbot.api.star": star,
    "astrbot.core": core_mod,
    "astrbot.core.agent": core_agent,
    "astrbot.core.agent.tool": core_tool,
    "astrbot.core.agent.run_context": core_run,
    "astrbot.core.star": core_star,
    "astrbot.core.star.context": core_star_ctx,
    "astrbot.core.astr_agent_context": core_agent_ctx,
    "astrbot.core.utils": core_utils,
    "astrbot.core.utils.astrbot_path": core_path,
})
import tl
import tl.tl_utils
import tl.web_studio_service
import tl.vision_handler
import core.processing.image_processor_service
assert "cv2" not in sys.modules, "cv2 eagerly imported!"
assert "tl.image_splitter" not in sys.modules, "image_splitter eagerly imported!"
assert "numpy" not in sys.modules, "numpy eagerly imported by tl/image_processor_service!"
print("OK")
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res.returncode == 0, f"stderr:\n{res.stderr}\nstdout:\n{res.stdout}"
    assert "OK" in res.stdout


def test_tl_utils_decode_base64_uses_pillow_without_cv2(
    tmp_path: Path, monkeypatch
) -> None:
    _ensure_astrbot_stubs()
    # Load real tl/tl_utils.py (tests/conftest.py installs a minimal stub in sys.modules)
    spec = importlib.util.spec_from_file_location(
        "tl._real_tl_utils_test",
        REPO_ROOT / "tl" / "tl_utils.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "tl"
    spec.loader.exec_module(mod)

    monkeypatch.setattr(mod, "get_temp_dir", lambda: tmp_path)
    valid_b64 = base64.b64encode(_make_png_bytes(12, 12, "RGB")).decode("ascii")
    out_path = mod._decode_base64_to_temp_file(valid_b64, verify_image=True)
    assert out_path is not None and Path(out_path).is_file()
    Path(out_path).unlink(missing_ok=True)

    invalid_b64 = base64.b64encode(b"not-a-valid-image").decode("ascii")
    bad_path = mod._decode_base64_to_temp_file(invalid_b64, verify_image=True)
    assert bad_path is None
    assert list(tmp_path.glob("cut_*.png")) == []


@pytest.mark.asyncio
async def test_image_render_service_gif_cache_eviction_and_thumb_offload(
    tmp_path: Path, monkeypatch
) -> None:
    _ensure_astrbot_stubs()
    from core.processing.image_render_service import ImageRenderService

    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory")
    svc = ImageRenderService(plugin_instance=SimpleNamespace(send_meme_as_gif=True))

    # Single payload larger than min(2MB, max_bytes // 2) must NOT be pinned in cache
    huge_payload = "A" * (2 * 1024 * 1024 + 10)
    svc._store_gif_base64_cache("huge.gif:1", huge_payload)
    assert "huge.gif:1" not in svc._gif_base64_cache

    # Small payloads respect low_memory max_size (10)
    for idx in range(15):
        svc._store_gif_base64_cache(f"small_{idx}.gif:1", "B" * 1024)
    assert len(svc._gif_base64_cache) <= 10

    # generate_thumb_uri works cleanly on RGBA PNG
    img_path = tmp_path / "sample.png"
    img_path.write_bytes(_make_png_bytes(240, 120, "RGBA"))
    uri = svc.generate_thumb_uri(str(img_path), size=64)
    assert uri.startswith("data:image/png;base64,")

    # render_emoji_list_page_file offloads _prepare_emoji_list_render_items via asyncio.to_thread
    offloaded = {"called": False}
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "_prepare_emoji_list_render_items":
            offloaded["called"] = True
        return await real_to_thread(fn, *args, **kwargs)

    async def fake_html_render(*args, **kwargs):
        return str(tmp_path / "page.png")

    svc.plugin = SimpleNamespace(html_render=fake_html_render)
    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)
    rendered = await svc.render_emoji_list_page_file(
        items=[{"index": 1, "path": str(img_path), "desc": "hi"}],
        page=1,
        total_pages=1,
        total_filtered=1,
        total_all=1,
        category="",
        per_page=10,
    )
    assert rendered == str(tmp_path / "page.png")
    assert offloaded["called"] is True


def test_text_similarity_and_search_feature_lru_bounds_and_clear(monkeypatch) -> None:
    _ensure_astrbot_stubs()
    from core.search import search_features, text_similarity

    for fn in (
        text_similarity.tokenize_for_bm25,
        text_similarity._normalize_text,
        text_similarity._calculate_hybrid_similarity_cached,
        text_similarity._extract_words,
        text_similarity._extract_chinese_chars,
        search_features.collect_phrase_words,
        search_features.prepare_entry_text_features,
    ):
        assert fn.cache_info().maxsize <= 2048

    text_similarity.tokenize_for_bm25("开心测试表情")
    search_features.collect_phrase_words(("开心", "测试"))
    assert text_similarity.tokenize_for_bm25.cache_info().currsize >= 1
    assert search_features.collect_phrase_words.cache_info().currsize >= 1

    text_similarity.clear_text_similarity_caches()
    assert text_similarity.tokenize_for_bm25.cache_info().currsize == 0
    assert search_features.collect_phrase_words.cache_info().currsize == 0

    # Hard memory watermark triggers automatic cache clear
    text_similarity.tokenize_for_bm25("内存压力测试")
    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory")
    monkeypatch.setenv("ASTRBOT_TEST_RSS_MB", "950")
    assert text_similarity.maybe_clear_caches_on_memory_pressure() is True
    assert text_similarity.tokenize_for_bm25.cache_info().currsize == 0


def test_embedding_service_compact_float32_query_cache_and_inplace_matrix(
    monkeypatch,
) -> None:
    _ensure_astrbot_stubs()
    from core.search.embedding_service import EmbeddingService

    class FakeProvider:
        def get_model(self):
            return "text-embedding-test"

        def get_dim(self):
            return 4

    vec1 = np.array([3.0, 4.0, 0.0, 0.0], dtype=np.float32)
    vec2 = np.array([0.0, 0.0, 0.0, 5.0], dtype=np.float32)

    class FakeDB:
        def load_embeddings_by_sig(self, _sig: str):
            return [
                {"path": "/a.png", "dim": 4, "vector": vec1.tobytes()},
                {"path": "/b.png", "dim": 4, "vector": vec2.tobytes()},
            ]

    plugin = SimpleNamespace(
        enable_embedding_search=True,
        embedding_provider_id="",
        context=None,
        db_service=FakeDB(),
    )
    svc = EmbeddingService(plugin)
    assert svc.QUERY_CACHE_MAX_ENTRIES == 64

    provider = FakeProvider()
    svc._provider = provider
    svc._provider_dim = 4
    svc._last_enable_embedding_search = True
    svc._last_embedding_provider_id = ""

    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory_aggressive")
    assert svc._effective_query_cache_max_entries() == 32

    svc.set_cached_query_embedding(provider, "hello world", [0.1, 0.2, 0.3, 0.4])
    cached_raw = next(iter(svc._query_embedding_cache.values()))[1]
    assert isinstance(cached_raw, np.ndarray)
    assert cached_raw.dtype == np.float32
    cached_list = svc.get_cached_query_embedding(provider, "hello   world")
    assert isinstance(cached_list, list)
    assert len(cached_list) == 4

    svc._load_fallback_matrix()
    assert svc._fallback_matrix is not None
    assert svc._fallback_matrix.dtype == np.float32
    assert np.allclose(svc._fallback_matrix[0], [0.6, 0.8, 0.0, 0.0], atol=1e-5)
    assert np.allclose(svc._fallback_matrix[1], [0.0, 0.0, 0.0, 1.0], atol=1e-5)


@pytest.mark.asyncio
async def test_database_pragmas_scoped_lookup_and_phash_cache(
    tmp_path: Path, monkeypatch
) -> None:
    _ensure_astrbot_stubs()
    from core.db.database_service import DatabaseService
    from core.processing.image_processor_service import ImageProcessorService

    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory")
    db = DatabaseService(tmp_path / "emoji.db")
    with db._get_connection() as conn:
        assert int(conn.execute("PRAGMA temp_store").fetchone()[0]) == 1
        assert int(conn.execute("PRAGMA mmap_size").fetchone()[0]) == 0
        assert int(conn.execute("PRAGMA cache_size").fetchone()[0]) == -1024

    await db.insert_batch(
        [
            {
                "path": str(tmp_path / "a.png"),
                "category": "happy",
                "hash": "hash_a",
                "phash": "abcdef0123456789",
                "scope_mode": "public",
                "origin_target": "",
                "desc": "smile",
            },
            {
                "path": str(tmp_path / "b.png"),
                "category": "happy",
                "hash": "hash_b",
                "phash": "1234567890abcdef",
                "scope_mode": "local",
                "origin_target": "umo:g1",
                "desc": "private",
            },
        ]
    )

    hit_pub = db.get_emoji_by_hash_and_scope("hash_a", scope_modes=("public",))
    assert hit_pub is not None and hit_pub[1]["hash"] == "hash_a"
    assert db.get_emoji_by_hash_and_scope("hash_b", scope_modes=("public",)) is None
    hit_priv = db.get_emoji_by_hash_and_scope(
        "hash_b", scope_modes=("local",), origin_target="umo:g1"
    )
    assert hit_priv is not None and hit_priv[1]["hash"] == "hash_b"
    batch = db.get_emojis_by_hashes(["hash_a", "hash_b"])
    assert len(batch) == 2

    # ImageProcessorService caches phash_map by library_revision
    proc = ImageProcessorService.__new__(ImageProcessorService)
    proc._phash_map_cache = {}
    proc._phash_map_revision = None
    calls = {"n": 0}
    real_with_rev = db.get_phash_map_with_revision

    def spy_with_rev():
        calls["n"] += 1
        return real_with_rev()

    monkeypatch.setattr(db, "get_phash_map_with_revision", spy_with_rev)
    m1 = proc._get_cached_phash_map(db)
    m2 = proc._get_cached_phash_map(db)
    assert m1 == m2
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_job_store_hardens_permissions_once_not_per_commit(
    tmp_path: Path, monkeypatch
) -> None:
    _ensure_astrbot_stubs()
    from tl.core.job_store import SQLiteJobStore
    from tl.core.models import Job
    from tl.core.requests import GenerationRequest

    chmod_calls = {"count": 0}
    real_chmod = os.chmod

    def spy_chmod(path, mode):
        chmod_calls["count"] += 1
        return real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", spy_chmod)
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        req = GenerationRequest(prompt="test", source="webui")
        await store.create(Job.create(req, job_id="job-1"))
        after_first = chmod_calls["count"]
        for i in range(2, 6):
            await store.create(Job.create(req, job_id=f"job-{i}"))
        assert chmod_calls["count"] == after_first
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_idle_polling_backoff_and_lazy_background_steal_queue(
    tmp_path: Path, monkeypatch
) -> None:
    _ensure_astrbot_stubs()
    from core.db.index_manager import IndexManager
    from core.events.event_handler import EventHandler
    from core.processing.pending_analysis_worker import PendingAnalysisWorker

    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "standard")
    plugin = SimpleNamespace(
        context=None,
        base_dir=tmp_path,
        steal_meme=False,
        plugin_config=SimpleNamespace(enable_embedding_search=False),
        db_service=None,
    )
    worker = PendingAnalysisWorker(plugin)
    assert worker._idle_poll_timeout() == 60.0

    im = IndexManager(plugin)
    assert im._idle_refresh_timeout() == 120.0

    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory")
    assert worker._idle_poll_timeout() == 120.0
    plugin.plugin_config.enable_embedding_search = True
    assert im._idle_refresh_timeout() == 120.0

    eh = EventHandler(plugin)
    await eh.start_background_workers()
    # Stealing is disabled -> no BackgroundStealQueue spawned eagerly
    assert eh._background_queue is None
    assert eh._lazy_queue_enabled is True
    # Lazy start on demand
    queue = await eh.ensure_background_workers_started()
    assert queue is not None and queue._accepting is True
    await eh.stop_background_workers()
    assert eh._background_queue is None
