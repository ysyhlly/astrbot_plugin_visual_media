"""Batch 3 regression tests: Faiss lock decoupling, hot-path de-blocking, retrieval quality & fault tolerance."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]


def _ensure_astrbot_stubs() -> None:
    """Install minimal AstrBot/optional stubs so tests run offline without full AstrBot SDK."""
    astrbot_api = sys.modules.get("astrbot.api")
    if astrbot_api is not None:
        if not hasattr(astrbot_api, "__path__"):
            astrbot_api.__path__ = []
        if not hasattr(astrbot_api, "AstrBotConfig"):
            astrbot_api.AstrBotConfig = dict

    if "astrbot.api.star" not in sys.modules:
        star_mod = ModuleType("astrbot.api.star")

        class _StarTools:
            @staticmethod
            def get_data_dir(_name: str = "") -> Path:
                return REPO_ROOT / ".tmp_test_data"

        class _Context:
            pass

        class _Star:
            html_render = None
            text_to_image = None

        star_mod.StarTools = _StarTools
        star_mod.Context = _Context
        star_mod.Star = _Star
        sys.modules["astrbot.api.star"] = star_mod
        if astrbot_api is not None:
            astrbot_api.star = star_mod

    if "astrbot.api.event" not in sys.modules:
        event_mod = ModuleType("astrbot.api.event")

        class _AstrMessageEvent:
            pass

        class _MessageChain:
            pass

        event_mod.AstrMessageEvent = _AstrMessageEvent
        event_mod.MessageChain = _MessageChain
        event_mod.filter = SimpleNamespace()
        sys.modules["astrbot.api.event"] = event_mod
        if astrbot_api is not None:
            astrbot_api.event = event_mod

    if "astrbot.api.message_components" not in sys.modules:
        mc_mod = ModuleType("astrbot.api.message_components")
        mc_mod.Image = type("Image", (), {})
        mc_mod.Plain = type("Plain", (), {})
        sys.modules["astrbot.api.message_components"] = mc_mod
        if astrbot_api is not None:
            astrbot_api.message_components = mc_mod

    if "astrbot.api.provider" not in sys.modules:
        prov_api_mod = ModuleType("astrbot.api.provider")
        prov_api_mod.LLMResponse = type("LLMResponse", (), {})
        prov_api_mod.ProviderRequest = type("ProviderRequest", (), {})
        sys.modules["astrbot.api.provider"] = prov_api_mod
        if astrbot_api is not None:
            astrbot_api.provider = prov_api_mod

    if "astrbot.core.provider.provider" not in sys.modules:
        core_mod = sys.modules.setdefault("astrbot.core", ModuleType("astrbot.core"))
        prov_pkg = sys.modules.setdefault(
            "astrbot.core.provider", ModuleType("astrbot.core.provider")
        )
        prov_mod = ModuleType("astrbot.core.provider.provider")

        class _EmbeddingProvider:
            pass

        prov_mod.EmbeddingProvider = _EmbeddingProvider
        core_mod.provider = prov_pkg
        prov_pkg.provider = prov_mod
        sys.modules["astrbot.core.provider.provider"] = prov_mod

    if "quart" not in sys.modules:
        quart_mod = ModuleType("quart")
        quart_mod.jsonify = lambda payload: payload
        quart_mod.request = SimpleNamespace(args={})
        quart_mod.send_file = None
        sys.modules["quart"] = quart_mod

    if "cv2" not in sys.modules:
        sys.modules["cv2"] = ModuleType("cv2")

    if "rank_bm25" not in sys.modules:
        bm25_mod = ModuleType("rank_bm25")

        class _BM25Plus:
            def __init__(self, corpus):
                self._corpus = corpus

            def get_scores(self, _tokens):
                return [0.0 for _ in self._corpus]

        bm25_mod.BM25Plus = _BM25Plus
        sys.modules["rank_bm25"] = bm25_mod

    if "rapidfuzz.distance.Levenshtein" not in sys.modules:
        rf_mod = sys.modules.setdefault("rapidfuzz", ModuleType("rapidfuzz"))
        rf_dist = sys.modules.setdefault(
            "rapidfuzz.distance", ModuleType("rapidfuzz.distance")
        )
        rf_lev = ModuleType("rapidfuzz.distance.Levenshtein")
        rf_lev.distance = lambda a, b: 0 if a == b else max(len(a), len(b))
        rf_mod.distance = rf_dist
        rf_dist.Levenshtein = rf_lev
        sys.modules["rapidfuzz.distance.Levenshtein"] = rf_lev


def _import_plugin_pkg_module(submodule: str):
    """Import a repo module under a parent package so relative imports work."""
    import importlib

    pkg_name = REPO_ROOT.name
    parent_dir = str(REPO_ROOT.parent)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    if pkg_name not in sys.modules:
        pkg = ModuleType(pkg_name)
        pkg.__path__ = [str(REPO_ROOT)]
        sys.modules[pkg_name] = pkg
    return importlib.import_module(f"{pkg_name}.{submodule}")


def _attach_fake_faiss(service, provider, fake_db, dim: int = 3) -> None:
    service._last_enable_embedding_search = True
    service._last_embedding_provider_id = ""
    service._get_provider = lambda: provider
    service._observed_identity = service._provider_identity(provider)
    service._observed_dim = dim
    service._faiss_available = True
    service._initialized_faiss = fake_db
    service._faiss_db = fake_db


def _make_png_bytes(size: tuple[int, int] = (16, 16), color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. P0-2: Faiss lock decoupled from remote embedding & rate-limit sleeps
# ---------------------------------------------------------------------------


def test_p0_2_background_embedding_sleep_does_not_starve_interactive_search():
    _ensure_astrbot_stubs()
    from core.search.embedding_provider_adapter import EmbeddingProviderAdapter
    from core.search.embedding_service import EmbeddingService

    async def _run():
        plugin = SimpleNamespace(
            enable_embedding_search=True,
            plugin_config=SimpleNamespace(model_request_interval=0.0),
            context=None,
            db_service=None,
        )
        service = EmbeddingService(plugin)

        bg_started = asyncio.Event()
        lock_held_during_remote: list[bool] = []

        class _SlowBgFastInteractiveProvider:
            provider_config = {"id": "emb-p0-2"}

            def __init__(self):
                self.bg_calls = 0
                self.interactive_calls = 0

            def get_dim(self):
                return 3

            def get_model(self):
                return "emb-test-v1"

            async def get_embedding(self, text: str):
                lock_held_during_remote.append(service._faiss_operation_lock.locked())
                if text.startswith("bg:"):
                    self.bg_calls += 1
                    bg_started.set()
                    await asyncio.sleep(0.55)
                    return [1.0, 0.0, 0.0]
                self.interactive_calls += 1
                await asyncio.sleep(0.02)
                return [1.0, 0.0, 0.0]

            async def get_embeddings(self, texts: list[str]):
                lock_held_during_remote.append(service._faiss_operation_lock.locked())
                self.bg_calls += 1
                bg_started.set()
                await asyncio.sleep(0.55)
                return [[1.0, 0.0, 0.0] for _ in texts]

        provider = _SlowBgFastInteractiveProvider()
        adapter = EmbeddingProviderAdapter(service, provider)

        class _FakeFaissDB:
            def __init__(self):
                self.docs: list[tuple[str, dict, list[float]]] = []
                self.lock_held_during_local_ops: list[bool] = []

            async def insert_batch(self, contents, metadatas, **_kwargs):
                self.lock_held_during_local_ops.append(
                    service._faiss_operation_lock.locked()
                )
                # Real FaissVecDB invokes its embedding_provider inside insert_batch.
                vecs = await adapter.get_embeddings_batch(contents)
                for text, meta, vec in zip(contents, metadatas, vecs):
                    self.docs.append((text, meta, vec))
                return list(range(len(contents)))

            async def retrieve(self, query, k=80, **_kwargs):
                self.lock_held_during_local_ops.append(
                    service._faiss_operation_lock.locked()
                )
                # Real FaissVecDB invokes its embedding_provider inside retrieve.
                _q = await adapter.get_embedding(query)
                return [
                    SimpleNamespace(
                        data={"metadata": {"path": "/memes/hit.png"}},
                        similarity=0.95,
                    )
                ]

        fake_db = _FakeFaissDB()
        _attach_fake_faiss(service, provider, fake_db)

        # Start slow background insert_emoji (simulating 0.55s remote/rate-limit wait)
        bg_task = asyncio.create_task(
            service.insert_emoji("/memes/bg1.png", {"desc": "bg:slow-doc"})
        )
        await asyncio.wait_for(bg_started.wait(), timeout=1.0)

        # While background insert is waiting on remote embedding, fire 4 concurrent interactive searches.
        t0 = time.monotonic()
        results = await asyncio.gather(
            service.search("interactive-q1"),
            service.search("interactive-q2"),
            service.search("interactive-q3"),
            service.search("interactive-q4"),
        )
        elapsed = time.monotonic() - t0

        assert all(r == [("/memes/hit.png", 0.95)] for r in results)
        assert elapsed < 0.30, (
            f"Interactive searches were starved by background embedding: {elapsed:.3f}s"
        )
        assert await bg_task is True
        # Remote provider calls must NEVER run while _faiss_operation_lock is held.
        assert lock_held_during_remote and not any(lock_held_during_remote)
        # Local Faiss ops MUST run while _faiss_operation_lock is held.
        assert fake_db.lock_held_during_local_ops and all(
            fake_db.lock_held_during_local_ops
        )
        # Precomputed embeddings prevented duplicate remote calls from inside FaissVecDB!
        assert provider.bg_calls == 1
        assert provider.interactive_calls == 4

    asyncio.run(_run())


def test_p0_2_backfill_decoupled_and_rollback_delete_acquires_faiss_lock():
    _ensure_astrbot_stubs()
    from core.search.embedding_provider_adapter import EmbeddingProviderAdapter
    from core.search.embedding_service import EmbeddingService

    async def _run():
        enqueued: list[list[str]] = []

        class _FakeDB:
            def get_all_paths(self):
                return ["/memes/a.png", "/memes/b.png"]

            def get_search_refresh_generations(self, batch):
                return {p: 1 for p in batch}

            def get_entries(self, batch):
                return {p: {"desc": f"desc-{p}"} for p in batch}

            def enqueue_search_refresh(self, paths):
                enqueued.append(list(paths))

            def finish_search_refresh(self, _path, _gen):
                pass

        plugin = SimpleNamespace(
            enable_embedding_search=True,
            plugin_config=SimpleNamespace(model_request_interval=0.0),
            db_service=_FakeDB(),
        )
        service = EmbeddingService(plugin)
        remote_lock_states: list[bool] = []
        delete_lock_states: list[bool] = []

        class _Provider:
            provider_config = {"id": "emb-bf"}

            def get_dim(self):
                return 3

            def get_model(self):
                return "m1"

            async def get_embeddings(self, texts):
                remote_lock_states.append(service._faiss_operation_lock.locked())
                await asyncio.sleep(0.02)
                return [[1.0, 0.0, 0.0] for _ in texts]

        provider = _Provider()
        adapter = EmbeddingProviderAdapter(service, provider)

        class _FailingInsertFaissDB:
            class _DocStorage:
                async def get_documents(self, **_kwargs):
                    return []

            document_storage = _DocStorage()

            async def insert_batch(self, contents, **_kwargs):
                assert service._faiss_operation_lock.locked()
                # Verify precomputed vectors work inside insert_batch
                vecs = await adapter.get_embeddings_batch(contents)
                assert len(vecs) == len(contents)
                return [1]  # incomplete -> triggers rollback

            async def delete_documents(self, **_kwargs):
                delete_lock_states.append(service._faiss_operation_lock.locked())

        _attach_fake_faiss(service, provider, _FailingInsertFaissDB())

        written = await service.backfill_existing(batch_size=4)
        assert written == 0
        assert remote_lock_states == [False]
        assert delete_lock_states == [True, True]
        assert enqueued == [["/memes/a.png", "/memes/b.png"]]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 2. P2-7: Eliminate double-remote embedding call when Faiss search fails
# ---------------------------------------------------------------------------


def test_p2_7_no_double_remote_embedding_on_remote_or_faiss_failure():
    _ensure_astrbot_stubs()
    from core.search.embedding_service import EmbeddingService

    async def _run():
        # Case A: Remote embedding raises -> return [] without second remote call
        plugin = SimpleNamespace(
            enable_embedding_search=True,
            plugin_config=SimpleNamespace(model_request_interval=0.0),
            db_service=None,
        )
        service_a = EmbeddingService(plugin)

        class _FailingProvider:
            provider_config = {"id": "emb-fail"}

            def __init__(self):
                self.calls = 0

            def get_dim(self):
                return 3

            def get_model(self):
                return "m-fail"

            async def get_embedding(self, _text):
                self.calls += 1
                raise RuntimeError("upstream 503")

        p_fail = _FailingProvider()
        _attach_fake_faiss(service_a, p_fail, SimpleNamespace(retrieve=lambda **_kw: []))
        service_a._fallback_loaded = True
        service_a._fallback_model_sig = service_a._fallback_signature(p_fail)
        service_a._fallback_paths = ["/memes/x.png"]
        service_a._fallback_matrix = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)

        assert await service_a.search("hello") == []
        assert p_fail.calls == 1

        # Case B: Remote embedding succeeds, Faiss local retrieve raises ->
        # fallback numpy reuses precomputed vector with ZERO second remote call!
        service_b = EmbeddingService(plugin)

        class _OkProvider:
            provider_config = {"id": "emb-ok"}

            def __init__(self):
                self.calls = 0

            def get_dim(self):
                return 3

            def get_model(self):
                return "m-ok"

            async def get_embedding(self, _text):
                self.calls += 1
                return [1.0, 0.0, 0.0]

        class _BrokenFaiss:
            async def retrieve(self, **_kwargs):
                raise RuntimeError("corrupt faiss index")

        p_ok = _OkProvider()
        _attach_fake_faiss(service_b, p_ok, _BrokenFaiss())
        service_b._fallback_loaded = True
        service_b._fallback_model_sig = service_b._fallback_signature(p_ok)
        service_b._fallback_paths = ["/memes/fallback_hit.png"]
        service_b._fallback_matrix = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)

        res = await service_b.search("hello")
        assert len(res) == 1 and res[0][0] == "/memes/fallback_hit.png"
        assert p_ok.calls == 1

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 3. P1-4: JointReplyPrefetch uses positive embedding_search_timeout (single pass)
# ---------------------------------------------------------------------------


def test_p1_4_joint_prefetch_uses_positive_embedding_timeout_single_pass():
    _ensure_astrbot_stubs()
    from core.search.meme_smart_select_service import (
        MemeSmartSelectService,
        _EMBEDDING_TIMEOUT_VAR,
    )
    from core.tools.joint_prefetch import JointReplyPrefetch

    async def _run():
        observed_timeouts: list[float | None] = []
        search_invocations = 0

        class _TurnState:
            def __init__(self):
                self.candidates = []

            def set_candidates(self, items):
                self.candidates = list(items)

            def get_candidates(self):
                return list(self.candidates)

        class _Event:
            unified_msg_origin = "g:100"

            def __init__(self):
                self._extras = {}
                self._emoji_turn_state = {}

            def get_platform_id(self):
                return "qq"

            def get_self_id(self):
                return "bot"

            def get_sender_id(self):
                return "u1"

        class _SlowEmb:
            def is_available(self):
                return True

            async def search(self, _q, k=48):
                await asyncio.sleep(2.0)
                return []

        scope = SimpleNamespace(_is_entry_allowed_for_event=lambda _d, _e: True)
        search_engine = SimpleNamespace(
            _bm25_index=None,
            _bm25_dirty=False,
            _bm25_doc_paths=[],
            _bm25_signature=None,
            plugin=SimpleNamespace(db_service=None),
            _build_bm25_index=lambda *_a, **_kw: asyncio.sleep(0),
        )
        smart_svc = MemeSmartSelectService(None, search_engine, None, scope)
        smart_svc._embedding_service = _SlowEmb()
        smart_svc._is_embedding_ready = lambda: True

        idx = {
            "/memes/local_overlay.png": {
                "category": "happy",
                "overlay_text": "早安",
                "description": "早安问候",
            }
        }

        class _Plugin:
            def __init__(self):
                self.plugin_config = SimpleNamespace(
                    llm_joint_reply=True,
                    auto_send_meme=True,
                    joint_prefetch_embedding_timeout=0.06,
                    get_event_target=lambda _e: ("group", "100"),
                )
                self.db_service = SimpleNamespace(get_library_version=lambda: 1)
                self.meme_selector = SimpleNamespace(
                    is_path_allowed_for_event=lambda _p, _e: True
                )
                self._emoji_sender_engine = SimpleNamespace(
                    emoji_turn_state=lambda ev: ev._emoji_turn_state.setdefault(
                        "s", _TurnState()
                    )
                )

            def is_send_enabled_for_event(self, _ev):
                return True

            async def _search_meme_impl(self, ev, query, *, on_outcome=None):
                nonlocal search_invocations
                search_invocations += 1
                observed_timeouts.append(_EMBEDDING_TIMEOUT_VAR.get())
                paths, _emb = await smart_svc._recall_candidate_paths(
                    idx, query, ev, set(), require_files=False
                )
                self._emoji_sender_engine.emoji_turn_state(ev).set_candidates(
                    [{"id": 1, "path": p, "desc": "本地命中"} for p in paths]
                )
                if on_outcome:
                    on_outcome("candidates", 1)
                yield "ok"

        plugin = _Plugin()
        ev = _Event()
        prefetch = JointReplyPrefetch(plugin, ev, "早安")
        try:
            candidates = await prefetch.result(plugin, ev, "早安", wait_seconds=0.5)
        finally:
            await prefetch.close()

        # Positive timeout passed into _search -> embedding cancelled in-place,
        # local recall executed ONCE (no duplicate local_only retry!).
        assert observed_timeouts == [0.06]
        assert search_invocations == 1
        assert len(candidates) == 1
        assert candidates[0]["path"] == "/memes/local_overlay.png"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 4. P1-5: MemeSearchToolWorkflow filters behavior BEFORE truncating [:5]
# ---------------------------------------------------------------------------


def test_p1_5_meme_search_tool_filters_before_truncating_top_k(tmp_path):
    _ensure_astrbot_stubs()
    from core.tools.meme_search_tool import MemeSearchToolWorkflow

    async def _run():
        files = []
        idx = {}
        for i in range(8):
            p = tmp_path / f"meme_{i}.png"
            p.write_bytes(_make_png_bytes())
            files.append(str(p))
            idx[str(p)] = {"category": "happy", "description": f"desc_{i}"}

        raw_results = [(p, f"desc_{i}", "happy", []) for i, p in enumerate(files)]
        blocked = set(files[:5])

        class _TurnState:
            def __init__(self):
                self.candidates = []

            def set_candidates(self, items):
                self.candidates = list(items)

        class _Behavior:
            def bind(self, _event):
                pass

            async def prepare_candidates(self, _event, candidates):
                return [c for c in candidates if c["path"] not in blocked]

        class _Host(MemeSearchToolWorkflow):
            MAX_SEARCH_RESULTS = 5

            def __init__(self):
                self.db_service = SimpleNamespace(
                    get_library_version=lambda: 1,
                    count_total=lambda: len(idx),
                    get_index_cache_readonly=lambda: idx,
                )
                self.media_behavior = _Behavior()
                self._turn_state = _TurnState()
                self._emoji_sender_engine = SimpleNamespace(
                    emoji_turn_state=lambda _ev: self._turn_state
                )

            def is_send_enabled_for_event(self, _ev):
                return True

            async def _search_meme_candidates(self, _ev, _q, **_kw):
                return list(raw_results)

        host = _Host()
        event = SimpleNamespace()
        chunks = [c async for c in host._search_meme_impl(event, "happy")]
        assert chunks
        returned_paths = [c["path"] for c in host._turn_state.candidates]
        # Top 5 were filtered out -> candidates #6..#8 must still surface!
        assert returned_paths == files[5:8]

        # When none are blocked, result is still capped at MAX_SEARCH_RESULTS=5
        blocked.clear()
        chunks2 = [c async for c in host._search_meme_impl(event, "happy")]
        assert chunks2
        assert len(host._turn_state.candidates) == 5

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 5. P1-6: MemeSelector narrow lock + sync IO/PIL offloaded to asyncio.to_thread
# ---------------------------------------------------------------------------


def test_p1_6_meme_selector_does_not_serialize_concurrent_groups(tmp_path):
    _ensure_astrbot_stubs()
    from core.search.meme_selector import MemeSelector

    async def _run():
        img_a = tmp_path / "group-a.png"
        img_b = tmp_path / "group-b.png"
        img_a.write_bytes(_make_png_bytes())
        img_b.write_bytes(_make_png_bytes())

        plugin = SimpleNamespace(
            plugin_config=SimpleNamespace(smart_meme_selection=True, categories=[]),
            is_send_enabled_for_event=lambda _e: True,
            db_service=None,
        )
        selector = MemeSelector(plugin)

        async def _slow_rank(category, context_text, *_a, **_kw):
            await asyncio.sleep(0.15)
            target = str(img_a) if "group-a" in context_text else str(img_b)
            return [(target, 0.9, 0.9, 0.0, 0.0, category or "happy")]

        selector._smart_select_service._rank_emoji_candidates = _slow_rank
        selector._selection_strategy._update_recent_usage = lambda *_a: None
        selector._scope_service._is_entry_allowed_for_event = lambda *_a: True
        selector._search_engine.get_index = lambda: {
            str(img_a): {"category": "happy"},
            str(img_b): {"category": "happy"},
        }

        t0 = time.monotonic()
        r1, r2 = await asyncio.gather(
            selector.select_emoji("happy", "group-a"),
            selector.select_emoji("happy", "group-b"),
        )
        elapsed = time.monotonic() - t0
        assert r1 == str(img_a)
        assert r2 == str(img_b)
        assert elapsed < 0.25, f"select_emoji serialized across groups: {elapsed:.3f}s"

        # Also verify select_emoji_with_jev does not serialize concurrent groups
        selector._jev_selector.get_history = lambda _ev: asyncio.sleep(0, result=[])
        selector._jev_selector.select = lambda cands, **_kw: asyncio.sleep(
            0, result=cands[0][0] if cands else None
        )
        t1 = time.monotonic()
        j1, j2 = await asyncio.gather(
            selector.select_emoji_with_jev(SimpleNamespace(), "group-a"),
            selector.select_emoji_with_jev(SimpleNamespace(), "group-b"),
        )
        elapsed_jev = time.monotonic() - t1
        assert j1 == str(img_a)
        assert j2 == str(img_b)
        assert elapsed_jev < 0.25, (
            f"select_emoji_with_jev serialized across groups: {elapsed_jev:.3f}s"
        )

    asyncio.run(_run())


def test_p1_6_sync_io_and_pil_offloaded_to_worker_threads(tmp_path):
    _ensure_astrbot_stubs()
    lib_mod = _import_plugin_pkg_module("api.library")
    LibraryRoutes = lib_mod.LibraryRoutes
    from core.events.event_handler import EventHandler
    from core.search.embedding_service import EmbeddingService
    from tl.reference_image import normalize_reference_image_input

    main_tid = threading.get_ident()

    async def _run():
        # 1) EmbeddingService._ensure_provider_ready -> db.get_all_paths in worker thread
        tids_emb: list[int] = []

        class _DB:
            def get_meta_value(self, _k):
                return "old-sig"

            def get_all_paths(self):
                tids_emb.append(threading.get_ident())
                return ["/memes/1.png"]

            def enqueue_search_refresh(self, _paths):
                tids_emb.append(threading.get_ident())

            def set_meta_value(self, _k, _v):
                pass

        class _Prov:
            provider_config = {"id": "p-th"}

            def get_dim(self):
                return 2

            def get_model(self):
                return "m"

            async def get_embedding(self, _text):
                return [1.0, 0.0]

        svc = EmbeddingService(
            SimpleNamespace(
                enable_embedding_search=True,
                plugin_config=SimpleNamespace(model_request_interval=0.0),
                db_service=_DB(),
            )
        )
        svc._get_provider = lambda: _Prov()
        svc._cleanup_stale_faiss_files = lambda _sig: None
        await svc._ensure_provider_ready()
        assert len(tids_emb) == 2
        assert all(tid != main_tid for tid in tids_emb)

        # 2) EventHandler._enforce_capacity -> normalize & select in worker threads
        tids_cap: list[int] = []
        eh = EventHandler(SimpleNamespace(base_dir=str(tmp_path), plugin_config=None))
        eh._normalize_capacity_index_paths = lambda _idx: (
            tids_cap.append(threading.get_ident()) or (0, 0)
        )
        eh._select_items_for_removal = lambda _idx: (
            tids_cap.append(threading.get_ident()) or []
        )
        await eh._enforce_capacity({})
        assert len(tids_cap) == 2
        assert all(tid != main_tid for tid in tids_cap)

        # 3) tl.reference_image.normalize_reference_image_input -> PIL/file read in worker thread
        img_file = tmp_path / "ref.png"
        img_file.write_bytes(_make_png_bytes())
        tids_ref: list[int] = []
        import tl.reference_image as ref_mod

        orig_coerce = ref_mod.coerce_reference_image_bytes

        def _wrapped_coerce(*args, **kwargs):
            tids_ref.append(threading.get_ident())
            return orig_coerce(*args, **kwargs)

        ref_mod.coerce_reference_image_bytes = _wrapped_coerce
        try:
            mime, b64 = await normalize_reference_image_input(str(img_file))
            assert mime == "image/png" and b64
            data_uri = "data:image/png;base64," + base64.b64encode(_make_png_bytes()).decode()
            mime2, b64_2 = await normalize_reference_image_input(data_uri)
            assert mime2 == "image/png" and b64_2
        finally:
            ref_mod.coerce_reference_image_bytes = orig_coerce
        assert len(tids_ref) == 2
        assert all(tid != main_tid for tid in tids_ref)

        # 4) api.library.handle_thumbnail -> index lookup & _file_base64 in worker thread
        tids_lib: list[int] = []

        class _FakeReq:
            class _Args:
                def get(self, key, default="", type=None):
                    if key == "hash":
                        return "abc123"
                    if key == "size":
                        return 128
                    return default

            args = _Args()

        class _LibraryHost(LibraryRoutes):
            def __init__(self):
                self._data_dir = tmp_path
                self._db = None

            def _find_index_entry_by_hash(self, _h):
                tids_lib.append(threading.get_ident())
                return (str(img_file), {"hash": "abc123"})

            def _file_base64(self, p: str) -> str:
                tids_lib.append(threading.get_ident())
                return "data:image/jpeg;base64," + base64.b64encode(Path(p).read_bytes()).decode()

        orig_req, orig_jsonify = lib_mod.request, lib_mod.jsonify
        lib_mod.request = _FakeReq()
        lib_mod.jsonify = lambda payload: payload
        try:
            resp = await _LibraryHost().handle_thumbnail()
            assert resp["success"] is True
        finally:
            lib_mod.request = orig_req
            lib_mod.jsonify = orig_jsonify
        assert len(tids_lib) == 2
        assert all(tid != main_tid for tid in tids_lib)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 6. P2-8 & P2-9: Refusal fast-fail, VLM timeout/lock, config defaults
# ---------------------------------------------------------------------------


def test_p2_8_refusal_and_invalid_reference_image_fail_fast():
    _ensure_astrbot_stubs()
    from tl.api.gemini_interactions import GeminiInteractionsProvider
    from tl.api.google import GoogleProvider
    from tl.api.openai_compat import OpenAICompatProvider
    from tl.api_types import APIError, ApiRequestConfig

    async def _run():
        class _DummyClient:
            def _collect_fallback_texts(self, _data):
                return []

            def _find_image_urls_in_text(self, _t):
                return []

            async def _extract_from_content(self, _t):
                return [], []

            async def _process_reference_image(self, *_a, **_kw):
                raise RuntimeError("bad ref")

        # 1) GoogleProvider refusal fast-fail
        google_resp = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": "I cannot generate this image due to safety policy."
                            }
                        ]
                    }
                }
            ]
        }
        try:
            await GoogleProvider().parse_response(
                client=_DummyClient(),
                response_data=google_resp,
                session=None,
                request_config=None,
            )
            raise AssertionError("Expected APIError")
        except APIError as err:
            assert err.error_type == "content_policy_refusal"
            assert err.retryable is False

        # 2) GeminiInteractionsProvider refusal fast-fail
        gi_resp = {
            "steps": [
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "text",
                            "text": "抱歉，由于内容政策限制我无法生成此图片。",
                        }
                    ],
                }
            ]
        }
        try:
            await GeminiInteractionsProvider().parse_response(
                client=_DummyClient(),
                response_data=gi_resp,
                session=None,
                request_config=None,
            )
            raise AssertionError("Expected APIError")
        except APIError as err:
            assert err.error_type == "content_policy_refusal"
            assert err.retryable is False

        # 3) GeminiInteractionsProvider invalid_reference_image is non-retryable
        cfg_gi = ApiRequestConfig(
            api_base="https://generativelanguage.googleapis.com/v1beta",
            api_key="k",
            model="gemini-3.1-flash-image",
            prompt="draw",
            reference_images=["bad://ref"],
        )
        try:
            await GeminiInteractionsProvider()._build_input(
                client=_DummyClient(), config=cfg_gi
            )
            raise AssertionError("Expected APIError")
        except APIError as err:
            assert err.error_type == "invalid_reference_image"
            assert err.retryable is False

        # 4) OpenAICompatProvider invalid_reference_image is non-retryable
        cfg_oai = ApiRequestConfig(
            api_base="https://example.com/v1",
            api_key="k",
            model="gpt-image-1",
            prompt="draw",
            reference_images=["bad://ref"],
        )
        try:
            await OpenAICompatProvider().build_request(
                client=_DummyClient(), config=cfg_oai
            )
            raise AssertionError("Expected APIError")
        except APIError as err:
            assert err.error_type == "invalid_reference_image"
            assert err.retryable is False

    asyncio.run(_run())


def test_p2_8_vlm_call_timeout_and_gate_lock_not_held_across_network():
    _ensure_astrbot_stubs()
    from core.processing.vlm_call_service import VLMCallService
    from core.util.model_requests import ModelRequestGate

    async def _run():
        # 1) ModelRequestGate releases pacing lock before awaiting operation()
        gate = ModelRequestGate(
            SimpleNamespace(plugin_config=SimpleNamespace(model_request_interval=0.05))
        )
        op1_entered = asyncio.Event()
        release_op1 = asyncio.Event()

        async def _hanging_op():
            op1_entered.set()
            await release_op1.wait()
            return "op1"

        t1 = asyncio.create_task(gate.call("vlm-p1", _hanging_op))
        await asyncio.wait_for(op1_entered.wait(), timeout=1.0)

        # Even while op1 is still in-flight, op2 must NOT deadlock on gate lock!
        t0 = time.monotonic()
        res2 = await asyncio.wait_for(
            gate.call("vlm-p1", lambda: asyncio.sleep(0.01, result="op2")),
            timeout=0.5,
        )
        elapsed = time.monotonic() - t0
        assert res2 == "op2"
        assert elapsed < 0.35
        release_op1.set()
        assert await t1 == "op1"

        # 2) VLMCallService enforces explicit vision_call_timeout on llm_generate
        async def _forever_llm(**_kwargs):
            await asyncio.sleep(10.0)

        plugin = SimpleNamespace(
            plugin_config=SimpleNamespace(
                model_request_interval=0.0,
                vision_call_timeout=0.05,
            ),
            vision_max_retries=1,
            vision_retry_delay=0.0,
            context=SimpleNamespace(llm_generate=_forever_llm),
        )
        vlm = VLMCallService(plugin)
        t_start = time.monotonic()
        try:
            await vlm._do_vlm_call("vlm-p2", "describe", "file:///tmp/a.png")
            raise AssertionError("Expected TimeoutError")
        except Exception as exc:
            assert isinstance(exc, TimeoutError) or isinstance(exc.__cause__, TimeoutError)
        assert time.monotonic() - t_start < 0.5

    asyncio.run(_run())


def test_p2_9_config_schema_and_validation_defaults_unified():
    _ensure_astrbot_stubs()
    schema_path = REPO_ROOT / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    memes_items = schema["memes"]["items"]

    assert memes_items["max_disk_bytes"]["default"] == 268435456
    assert memes_items["max_pending_disk_bytes"]["default"] == 134217728
    assert memes_items["meme_chance"]["default"] == 0.2
    assert memes_items["steal_chance"]["default"] == 0.3

    memes_mod = _import_plugin_pkg_module("memes_engine")
    Main = memes_mod.Main

    updated: dict = {}

    class _EngineHarness:
        def __init__(self):
            self.plugin_config = SimpleNamespace(
                max_reg_num=-1,
                meme_chance=5.0,
                steal_mode="invalid",
                steal_chance=-0.5,
                steal_pool_capacity=1,
                max_disk_bytes=-10,
                max_pending_disk_bytes=-5,
                external_source_max_items=2000,
                external_source_max_image_bytes=32 * 1024 * 1024,
                external_source_max_archive_bytes=1024 * 1024 * 1024,
                external_source_max_uncompressed_bytes=4 * 1024 * 1024 * 1024,
                external_source_max_pixels=40_000_000,
            )

        def update_config(self, vals):
            updated.update(vals)

    harness = _EngineHarness()
    Main._validate_config(harness)

    assert updated["meme_chance"] == memes_items["meme_chance"]["default"] == 0.2
    assert updated["steal_chance"] == memes_items["steal_chance"]["default"] == 0.3
    assert updated["max_disk_bytes"] == memes_items["max_disk_bytes"]["default"] == 268435456
    assert (
        updated["max_pending_disk_bytes"]
        == memes_items["max_pending_disk_bytes"]["default"]
        == 134217728
    )
