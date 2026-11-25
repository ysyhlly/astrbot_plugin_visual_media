"""Batch 2 storage, data-safety, thread-safety, and reliability regression tests.

Covers:
1. P1-3: pending/ disk quota cleanup excludes queued/processing/approved/library-referenced
   records and deletes DB rows BEFORE unlinking disk files.
2. P1-6:
   - TaskScheduler.cancel_task avoids KeyError when done-callback removes the task.
   - ImageRenderService._gif_base64_cache is thread-safe under concurrent read/write/evict/cleanup.
   - tl/vision_handler.py::detect_grid_rows_cols cleans up downloaded & resized temp files
     on normal return, early return, and exception (without touching caller local files).
3. P2-7:
   - EmbeddingService cleans up stale emoji_faiss_{old_sig}.* files on model switch.
   - IndexManager.drain_search_refresh and EmbeddingService.backfill_existing skip empty
     text entries cleanly instead of looping infinitely.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import sqlite3
import sys
import threading
import time
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


def _ensure_pkg_and_star_stub(monkeypatch) -> str:
    repo_root = Path(__file__).resolve().parents[1]
    pkg_name = "astrbot_plugin_visual_media"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(repo_root)]
        monkeypatch.setitem(sys.modules, pkg_name, pkg)
    if "astrbot.api.star" not in sys.modules:
        star_mod = types.ModuleType("astrbot.api.star")
        star_mod.StarTools = SimpleNamespace(
            get_data_dir=lambda *_a, **_kw: str(repo_root)
        )
        monkeypatch.setitem(sys.modules, "astrbot.api.star", star_mod)
        if "astrbot.api" in sys.modules:
            monkeypatch.setattr(
                sys.modules["astrbot.api"], "star", star_mod, raising=False
            )
    if "astrbot.api.provider" not in sys.modules:
        prov_mod = types.ModuleType("astrbot.api.provider")
        prov_mod.ProviderRequest = object
        monkeypatch.setitem(sys.modules, "astrbot.api.provider", prov_mod)
        if "astrbot.api" in sys.modules:
            monkeypatch.setattr(
                sys.modules["astrbot.api"], "provider", prov_mod, raising=False
            )
    return pkg_name


# ---------------------------------------------------------------------------
# 1. P1-3: pending/ disk quota cleanup safety & DB-before-disk ordering
# ---------------------------------------------------------------------------


def test_pending_eviction_candidates_excludes_inflight_approved_and_referenced(
    tmp_path: Path, monkeypatch
) -> None:
    pkg_name = _ensure_pkg_and_star_stub(monkeypatch)
    retention = importlib.import_module(f"{pkg_name}.core.maintenance.retention")

    files = {}
    for name in ("queued", "processing", "approved", "in_lib", "plain_old", "plain_new"):
        p = tmp_path / f"{name}.png"
        p.write_bytes(b"x" * 400)
        files[name] = p

    rows = [
        {"id": 1, "path": str(files["queued"]), "created_at": 10, "review_status": "queued"},
        {"id": 2, "path": str(files["processing"]), "created_at": 20, "review_status": "processing"},
        {"id": 3, "path": str(files["approved"]), "created_at": 30, "review_status": "approved"},
        {"id": 4, "path": str(files["in_lib"]), "created_at": 40, "review_status": "pending"},
        {"id": 5, "path": str(files["plain_old"]), "created_at": 50, "review_status": "pending"},
        {"id": 6, "path": str(files["plain_new"]), "created_at": 60, "review_status": "failed"},
    ]

    # Total bytes = 2400, cap = 1700 -> needs to evict >= 700 bytes, ONLY ids 5 and 6 are eligible!
    evict = retention.pending_eviction_candidates(
        rows,
        max_disk_bytes=1700,
        protected_paths=[str(files["in_lib"])],
    )
    assert [r["id"] for r in evict] == [5, 6]


def test_pending_disk_cap_deletes_db_first_and_protects_concurrent_inflight_or_approved(
    tmp_path: Path, monkeypatch
) -> None:
    pkg_name = _ensure_pkg_and_star_stub(monkeypatch)
    svc_mod = importlib.import_module(f"{pkg_name}.core.maintenance.service")
    pending_repo_mod = importlib.import_module(f"{pkg_name}.core.db.pending_repository")

    # Build a real SQLite emoji_pending table backed by PendingRepository.delete_pending_batch
    db_file = tmp_path / "test_pending.db"
    conn_init = sqlite3.connect(db_file)
    conn_init.execute(
        """
        CREATE TABLE emoji_pending (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            hash TEXT NOT NULL,
            review_status TEXT DEFAULT 'pending',
            created_at INTEGER DEFAULT 0
        )
        """
    )

    p_evict = tmp_path / "evict.png"
    p_racing_processing = tmp_path / "racing_processing.png"
    p_racing_queued = tmp_path / "racing_queued.png"
    p_racing_approved = tmp_path / "racing_approved.png"
    p_keep = tmp_path / "keep.png"
    for p in (p_evict, p_racing_processing, p_racing_queued, p_racing_approved, p_keep):
        p.write_bytes(b"a" * 500)

    conn_init.executemany(
        "INSERT INTO emoji_pending (id, path, hash, review_status, created_at) VALUES (?, ?, ?, ?, ?)",
        [
            (1, str(p_evict), "h1", "pending", 100),
            (2, str(p_racing_processing), "h2", "pending", 200),
            (3, str(p_racing_queued), "h3", "pending", 300),
            (4, str(p_racing_approved), "h4", "pending", 400),
            (5, str(p_keep), "h5", "pending", 500),
        ],
    )
    conn_init.commit()
    conn_init.close()

    events: list[str] = []
    live_library: list[str] = []

    class _RealPendingDB(pending_repo_mod.PendingRepository):
        @contextmanager
        def _get_connection(self):
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

        def get_all_paths(self):
            return list(live_library)

        def get_pending_paginated(self, page=1, page_size=100000):
            with self._get_connection() as conn:
                rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT id, path, hash, review_status, created_at FROM emoji_pending"
                    ).fetchall()
                ]
            # Simulate concurrent transitions happening AFTER snapshot read & BEFORE delete:
            # - id=2 claimed by VLM worker -> processing
            # - id=3 queued for VLM -> queued
            # - id=4 approved into main library (removed from emoji_pending + added to library)
            with self._get_connection() as conn:
                conn.execute("UPDATE emoji_pending SET review_status='processing' WHERE id=2")
                conn.execute("UPDATE emoji_pending SET review_status='queued' WHERE id=3")
                conn.execute("DELETE FROM emoji_pending WHERE id=4")
                conn.commit()
            live_library.append(str(p_racing_approved))
            return rows, len(rows), {}

        def delete_pending_batch(self, ids, *, exclude_statuses=("processing",)):
            events.append(f"db_delete:{sorted(ids)}")
            return super().delete_pending_batch(ids, exclude_statuses=exclude_statuses)

    real_safe_remove = svc_mod.safe_remove_file

    async def tracked_remove(path: str):
        events.append(f"disk_remove:{Path(path).name}")
        return await real_safe_remove(path)

    monkeypatch.setattr(svc_mod, "safe_remove_file", tracked_remove)

    db = _RealPendingDB()
    plugin = SimpleNamespace(
        db_service=db,
        plugin_config=SimpleNamespace(max_pending_disk_bytes=1100),
    )
    svc = svc_mod.MaintenanceService(plugin)
    removed = asyncio.run(svc._enforce_pending_disk_cap())

    # Only id=1 was genuinely evicted in DB and removed from disk!
    assert removed == 1
    assert not p_evict.exists()
    # Concurrently claimed/queued/approved files MUST survive!
    assert p_racing_processing.exists()
    assert p_racing_queued.exists()
    assert p_racing_approved.exists()
    assert p_keep.exists()
    # DB delete MUST occur strictly before disk unlink!
    assert events[0].startswith("db_delete:")
    assert events[1:] == ["disk_remove:evict.png"]


# ---------------------------------------------------------------------------
# 2. P1-6: TaskScheduler KeyError, ImageRenderService cache lock, VisionHandler temp leak
# ---------------------------------------------------------------------------


def test_task_scheduler_cancel_task_never_raises_keyerror_and_preserves_replacement(
    monkeypatch,
) -> None:
    pkg_name = _ensure_pkg_and_star_stub(monkeypatch)
    sched_mod = importlib.import_module(f"{pkg_name}.task_scheduler")

    async def _run():
        scheduler = sched_mod.TaskScheduler()
        started = asyncio.Event()

        async def _worker():
            started.set()
            await asyncio.sleep(60)

        t1 = scheduler.create_task("stealer_search_refresh", _worker())
        assert t1 is not None
        await started.wait()

        # Previously: done callback popped key during gather(), then `del self._tasks[name]` raised KeyError!
        cancelled = await scheduler.cancel_task("stealer_search_refresh")
        assert cancelled is True
        assert "stealer_search_refresh" not in scheduler._tasks

        # Cancelling already-gone task returns False cleanly
        assert await scheduler.cancel_task("stealer_search_refresh") is False

        # Test cancel_all_tasks / cleanup with multiple live tasks
        e2, e3 = asyncio.Event(), asyncio.Event()

        async def _w(ev):
            ev.set()
            await asyncio.sleep(60)

        scheduler.create_task("job_a", _w(e2))
        scheduler.create_task("job_b", _w(e3))
        await e2.wait()
        await e3.wait()
        await scheduler.cleanup()
        assert scheduler._tasks == {}

    asyncio.run(_run())


def test_image_render_service_gif_cache_thread_safety(monkeypatch) -> None:
    pkg_name = _ensure_pkg_and_star_stub(monkeypatch)
    render_mod = importlib.import_module(
        f"{pkg_name}.core.processing.image_render_service"
    )

    svc = render_mod.ImageRenderService(plugin_instance=SimpleNamespace(send_meme_as_gif=True))
    svc._gif_base64_cache_max_size = 8
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def _writer(worker_id: int):
        try:
            barrier.wait(timeout=5)
            for i in range(80):
                key = f"file_{worker_id}_{i}.png:1"
                with svc._gif_cache_lock:
                    svc._gif_base64_cache[key] = (time.time(), "A" * 512)
                    svc._evict_gif_base64_cache()
        except BaseException as exc:
            errors.append(exc)

    def _reader():
        try:
            barrier.wait(timeout=5)
            for i in range(120):
                with svc._gif_cache_lock:
                    _ = svc._gif_base64_cache.get(f"file_0_{i % 80}.png:1")
                svc._evict_gif_base64_cache()
        except BaseException as exc:
            errors.append(exc)

    def _cleaner():
        try:
            barrier.wait(timeout=5)
            for _ in range(40):
                svc.cleanup()
                time.sleep(0.001)
        except BaseException as exc:
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs = [
            pool.submit(_writer, 0),
            pool.submit(_writer, 1),
            pool.submit(_writer, 2),
            pool.submit(_reader),
            pool.submit(_reader),
            pool.submit(_cleaner),
        ]
        for f in futs:
            f.result(timeout=15)

    assert errors == []


def test_vision_handler_detect_grid_cleans_downloaded_and_resized_temp_files(
    tmp_path: Path, monkeypatch
) -> None:
    _ensure_pkg_and_star_stub(monkeypatch)
    if "cv2" not in sys.modules:
        monkeypatch.setitem(sys.modules, "cv2", types.ModuleType("cv2"))
    tl_utils_stub = sys.modules.get("tl.tl_utils")
    if tl_utils_stub is not None:
        monkeypatch.setattr(
            tl_utils_stub, "is_valid_base64_image_str", lambda *_a, **_kw: False, raising=False
        )
        monkeypatch.setattr(
            tl_utils_stub, "resolve_image_source_to_path", lambda *_a, **_kw: None, raising=False
        )
    import tl.vision_handler as vh_mod

    # 1. Remote URL: downloaded temp + resized temp cleaned on success, early return, and exception
    orig_ntf = vh_mod.tempfile.NamedTemporaryFile
    for scenario in ("success", "empty_return", "exception"):
        downloaded_file = tmp_path / f"downloaded_{scenario}.png"
        # > 1200px to also trigger resized tmp_path creation
        Image.new("RGB", (1400, 1400), (10, 20, 30)).save(downloaded_file, format="PNG")
        created_tmps: list[Path] = []

        def spy_ntf(*args, _tmps=created_tmps, **kwargs):
            f = orig_ntf(*args, **kwargs)
            _tmps.append(Path(f.name))
            return f

        monkeypatch.setattr(vh_mod.tempfile, "NamedTemporaryFile", spy_ntf)

        class _FakeClient:
            async def _get_session(self):
                return object()

            async def _download_image(self, url, session, use_cache=False):
                return str(downloaded_file), str(downloaded_file)

        async def _llm_gen(**_kw):
            if scenario == "exception":
                raise RuntimeError("vision boom")
            if scenario == "empty_return":
                return SimpleNamespace(output_text="")
            return SimpleNamespace(output_text='{"rows": 2, "cols": 3}')

        ctx = SimpleNamespace(llm_generate=_llm_gen)
        handler = vh_mod.VisionHandler(
            ctx,
            vision_provider_id="vision-1",
            api_client=_FakeClient(),
        )
        res = asyncio.run(handler.detect_grid_rows_cols("https://93.184.216.34/grid.png"))
        if scenario == "success":
            assert res == (2, 3)
        else:
            assert res is None

        # BOTH downloaded file AND resized temp file MUST be deleted!
        assert not downloaded_file.exists(), f"downloaded file leaked in {scenario}"
        assert created_tmps and all(not p.exists() for p in created_tmps), (
            f"resized temp leaked in {scenario}"
        )

    # 2. Local caller file: resized temp cleaned, caller's original local file preserved!
    local_img = tmp_path / "caller_original.png"
    Image.new("RGB", (1400, 1400), (80, 90, 100)).save(local_img, format="PNG")

    async def _llm_ok(**_kw):
        return SimpleNamespace(output_text='{"rows": 2, "cols": 2}')

    handler_local = vh_mod.VisionHandler(
        SimpleNamespace(llm_generate=_llm_ok),
        vision_provider_id="vision-1",
        api_client=None,
    )
    assert asyncio.run(handler_local.detect_grid_rows_cols(str(local_img))) == (2, 2)
    assert local_img.exists(), "caller's local image must NOT be deleted!"


# ---------------------------------------------------------------------------
# 3. P2-7: Model switch stale Faiss cleanup & empty-text infinite retry guard
# ---------------------------------------------------------------------------


def test_embedding_model_switch_cleans_stale_faiss_files(
    tmp_path: Path, monkeypatch
) -> None:
    pkg_name = _ensure_pkg_and_star_stub(monkeypatch)
    emb_mod = importlib.import_module(f"{pkg_name}.core.search.embedding_service")

    data_dir = tmp_path / "memes"
    data_dir.mkdir()

    # Pre-create stale Faiss files from old model + legacy files
    old_db = data_dir / "emoji_faiss_oldsig1234567890abcdef.db"
    old_wal = data_dir / "emoji_faiss_oldsig1234567890abcdef.db-wal"
    old_shm = data_dir / "emoji_faiss_oldsig1234567890abcdef.db-shm"
    old_idx = data_dir / "emoji_faiss_oldsig1234567890abcdef.index"
    legacy_db = data_dir / "emoji_faiss.db"
    legacy_idx = data_dir / "emoji_faiss.index"
    unrelated = data_dir / "emoji.db"
    for f in (old_db, old_wal, old_shm, old_idx, legacy_db, legacy_idx, unrelated):
        f.write_bytes(b"stub")

    class _Provider:
        provider_config = {"id": "emb-new", "embedding_api_base": "https://api.example"}

        def get_model(self):
            return "text-embedding-new"

        def get_dim(self):
            return 4

        async def get_embedding(self, text: str):
            return [0.1, 0.2, 0.3, 0.4]

    meta_store = {"embedding_active_signature": "fallback:oldsig1234567890abcdef"}
    enqueued: list[list[str]] = []

    class _FakeDB:
        def get_meta_value(self, key: str):
            return meta_store.get(key)

        def set_meta_value(self, key: str, value: str):
            meta_store[key] = value

        def get_all_paths(self):
            return ["/a.png"]

        def enqueue_search_refresh(self, paths):
            enqueued.append(list(paths))

    provider = _Provider()
    plugin = SimpleNamespace(
        enable_embedding_search=True,
        embedding_provider_id="emb-new",
        base_dir=data_dir,
        db_service=_FakeDB(),
        context=SimpleNamespace(
            get_all_embedding_providers=lambda: [provider],
            provider_manager=SimpleNamespace(inst_map={"emb-new": provider}),
        ),
    )

    svc = emb_mod.EmbeddingService(plugin)
    # Pre-create active model's own faiss file to ensure it is NOT deleted!
    svc._observed_dim = 4
    expected_sig = svc._fallback_signature(provider)
    active_short = expected_sig.split(":")[-1][:24]
    svc._observed_dim = 0
    active_db = data_dir / f"emoji_faiss_{active_short}.db"
    active_idx = data_dir / f"emoji_faiss_{active_short}.index"
    active_db.write_bytes(b"keep-db")
    active_idx.write_bytes(b"keep-idx")

    asyncio.run(svc._ensure_provider_ready())

    # Old & legacy Faiss files must be cleaned up
    for stale in (old_db, old_wal, old_shm, old_idx, legacy_db, legacy_idx):
        assert not stale.exists(), f"stale file not cleaned: {stale.name}"
    # Active model files & unrelated main DB must be preserved
    assert active_db.exists()
    assert active_idx.exists()
    assert unrelated.exists()
    assert meta_store["embedding_active_signature"] == expected_sig
    assert enqueued == [["/a.png"]]


def test_search_refresh_and_backfill_skip_empty_text_without_infinite_retry(
    monkeypatch,
) -> None:
    pkg_name = _ensure_pkg_and_star_stub(monkeypatch)
    idx_mod = importlib.import_module(f"{pkg_name}.core.db.index_manager")
    emb_mod = importlib.import_module(f"{pkg_name}.core.search.embedding_service")

    finished: list[tuple[str, int, str]] = []
    deleted: list[str] = []
    inserted: list[str] = []

    class _FakeEmbedding:
        def _embedding_enabled(self):
            return True

        def is_available(self):
            return True

        async def _ensure_provider_ready(self):
            return None

        def _build_search_text(self, entry: dict) -> str:
            return str(entry.get("desc") or "").strip()

        async def delete_by_path(self, path: str):
            deleted.append(path)
            return True

        async def insert_emoji(self, path: str, entry: dict) -> bool:
            inserted.append(path)
            if path == "/fail.png":
                return False
            return True

    entries = {
        "/empty.png": {"desc": "   "},
        "/ok.png": {"desc": "开心猫猫"},
        "/fail.png": {"desc": "触发供应商故障"},
    }

    class _FakeDB:
        def get_search_refresh_jobs(self):
            return [
                {"path": "/empty.png", "generation": 1},
                {"path": "/ok.png", "generation": 2},
                {"path": "/fail.png", "generation": 3},
            ]

        def get_emoji(self, path: str):
            return entries.get(path)

        def finish_search_refresh(self, path: str, generation: int, error: str = ""):
            finished.append((path, generation, error))

    plugin = SimpleNamespace(
        db_service=_FakeDB(),
        meme_selector=SimpleNamespace(
            _smart_select_service=SimpleNamespace(_embedding_service=_FakeEmbedding())
        ),
    )
    mgr = idx_mod.IndexManager(plugin)
    count = asyncio.run(mgr.drain_search_refresh())

    assert count == 3
    assert deleted == ["/empty.png", "/ok.png", "/fail.png"]
    # Empty text entry was NOT passed to insert_emoji and finished with error="" (no infinite retry!)
    assert inserted == ["/ok.png", "/fail.png"]
    assert finished[0] == ("/empty.png", 1, "")
    assert finished[1] == ("/ok.png", 2, "")
    assert finished[2][0] == "/fail.png" and finished[2][2] != ""

    # Also verify EmbeddingService.backfill_existing finishes search_refresh for empty-text rows
    bf_finished: list[tuple[str, int]] = []
    bf_enqueued: list[list[str]] = []

    class _Provider:
        provider_config = {"id": "emb-1"}

        def get_model(self):
            return "m1"

        def get_dim(self):
            return 2

        async def get_embedding(self, text: str):
            return [1.0, 0.0]

    class _BackfillDB:
        def get_meta_value(self, key):
            return None

        def set_meta_value(self, key, val):
            pass

        def get_all_embedding_paths(self, model_sig=""):
            return []

        def get_all_paths(self):
            return ["/empty.png", "/ok.png"]

        def get_search_refresh_generations(self, batch):
            return {"/empty.png": 10, "/ok.png": 11}

        def get_entries(self, batch):
            return {
                "/empty.png": {"desc": "", "tags": [], "scenes": []},
                "/ok.png": {"desc": "可爱", "tags": ["笑"], "scenes": []},
            }

        def upsert_embedding(self, path, blob, dim, model_sig):
            pass

        def enqueue_search_refresh(self, paths):
            bf_enqueued.append(list(paths))

        def finish_search_refresh(self, path, generation, error=""):
            bf_finished.append((path, generation))

    prov = _Provider()
    bf_plugin = SimpleNamespace(
        enable_embedding_search=True,
        embedding_provider_id="emb-1",
        db_service=_BackfillDB(),
        context=SimpleNamespace(
            get_all_embedding_providers=lambda: [prov],
            provider_manager=SimpleNamespace(inst_map={"emb-1": prov}),
        ),
    )
    emb_svc = emb_mod.EmbeddingService(bf_plugin)
    monkeypatch.setattr(emb_svc, "_init_faiss", lambda: False)
    # Pre-seed active signature so _ensure_provider_ready doesn't count initial model-switch enqueue
    emb_svc._observed_dim = 2
    active_sig = emb_svc._fallback_signature(prov)
    emb_svc._observed_dim = 0
    monkeypatch.setattr(bf_plugin.db_service, "get_meta_value", lambda _k: active_sig)
    written = asyncio.run(emb_svc.backfill_existing(batch_size=10))
    assert written == 1
    assert ("/ok.png", 11) in bf_finished
    assert ("/empty.png", 10) in bf_finished
    assert bf_enqueued == []
