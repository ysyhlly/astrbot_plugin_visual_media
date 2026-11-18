"""Wave 1 integration tests for visual_media optimizations."""

from __future__ import annotations

import asyncio
import warnings
import zipfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import numpy as np
import pytest
from astrbot_plugin_visual_media import character_check
from astrbot_plugin_visual_media.core.search.embedding_service import EmbeddingService
from astrbot_plugin_visual_media.media_behavior import MediaBehavior
from astrbot_plugin_visual_media.tl import background_dispatch
from astrbot_plugin_visual_media.tools.build_release import build
from astrbot_plugin_visual_media.world_reference_access import expected_directory
from PIL import Image
from test_media_behavior import candidate, event


@pytest.mark.asyncio
async def test_character_delivery_precedes_async_check_and_only_mismatch_alerts(
    owner, tmp_path, monkeypatch
):
    ref_img = tmp_path / "ref.png"
    out_img = tmp_path / "out.png"
    Image.new("RGB", (32, 32), "blue").save(ref_img)
    Image.new("RGB", (32, 32), "red").save(out_img)

    monkeypatch.setattr(owner.library, "reference_path", lambda h: ref_img)
    await owner.library.save_profile(
        {"key": "anan", "name": "安安", "appearance": "blue hair", "references": ["hash1"]}
    )

    captured = {}
    delivered_event = asyncio.Event()
    check_gate = asyncio.Event()
    check_done = asyncio.Event()

    class _Service:
        async def submit(self, **kwargs):
            captured["on_complete"] = kwargs["on_complete"]
            return {"task_id": "task-async-1", "job_id": "job-async-1"}

    monkeypatch.setattr(owner.generation, "get_service", lambda *a: _Service())

    async def fake_dispatch(*args, **kwargs):
        delivered_event.set()
        return True

    async def slow_mismatch_check(*args, **kwargs):
        await check_gate.wait()
        try:
            return {
                "status": "mismatch",
                "issues": ["图1发色偏红"],
                "reason": "发色不一致",
                "checked_images": 1,
                "total_images": 1,
                "reference_count": 1,
            }
        finally:
            check_done.set()

    monkeypatch.setattr(background_dispatch, "dispatch_generation_result", fake_dispatch)
    monkeypatch.setattr(character_check, "check_character", slow_mismatch_check)

    sent_notices = []

    async def fake_send_message(session, chain):
        sent_notices.append((str(session), [getattr(seg, "text", "") for seg in chain.chain]))
        return True

    owner.context.send_message = fake_send_message

    await owner.library.generate_character("anan", "smile", event())

    # Image delivery MUST complete immediately without waiting for slow character verification.
    await asyncio.wait_for(
        captured["on_complete"](
            {
                "status": "succeeded",
                "task_id": "task-async-1",
                "job_id": "job-async-1",
                "image_paths": [str(out_img)],
                "image_urls": [],
                "text_content": "",
            }
        ),
        timeout=1.0,
    )
    assert delivered_event.is_set()
    assert not sent_notices

    # Wait for the task that persists the check and sends the follow-up.
    check_gate.set()
    await asyncio.wait_for(check_done.wait(), timeout=2.0)
    await asyncio.wait_for(asyncio.gather(*tuple(owner.library._check_tasks)), timeout=3.0)
    assert len(sent_notices) == 1
    assert "图1发色偏红" in "".join(sent_notices[0][1])

    # Unknown / pass verification must not emit extra chat alert messages.
    unknown_done = asyncio.Event()

    async def unknown_check(*args, **kwargs):
        try:
            return {
                "status": "unknown",
                "issues": [],
                "reason": "遮挡",
                "checked_images": 1,
                "total_images": 1,
                "reference_count": 1,
            }
        finally:
            unknown_done.set()

    monkeypatch.setattr(character_check, "check_character", unknown_check)
    await captured["on_complete"](
        {
            "status": "succeeded",
            "task_id": "task-async-1",
            "job_id": "job-async-1",
            "image_paths": [str(out_img)],
            "image_urls": [],
            "text_content": "",
        }
    )
    await asyncio.wait_for(unknown_done.wait(), timeout=2.0)
    await asyncio.wait_for(asyncio.gather(*tuple(owner.library._check_tasks)), timeout=3.0)
    assert len(sent_notices) == 1


@pytest.mark.asyncio
async def test_character_check_reuses_vlm_service_and_library_close_cancels_tasks(
    owner, tmp_path, monkeypatch
):
    ref_img = tmp_path / "ref.png"
    out_img = tmp_path / "out.png"
    Image.new("RGB", (24, 24), "blue").save(ref_img)
    Image.new("RGB", (24, 24), "blue").save(out_img)

    created = 0

    class FakeVLM:
        def __init__(self, memes):
            nonlocal created
            created += 1
            self.memes = memes

        async def _resolve_vision_provider(self, event=None):
            return "vision-p"

    monkeypatch.setattr(character_check, "VLMCallService", FakeVLM)
    owner.context.llm_generate = AsyncMock(
        return_value=NS(completion_text='{"status":"pass","issues":[],"reason":"ok"}')
    )
    profile = {"name": "安安", "appearance": "blue"}
    res1 = await character_check.check_character(owner, profile, [str(out_img)], [str(ref_img)], "test")
    res2 = await character_check.check_character(owner, profile, [str(out_img)], [str(ref_img)], "test")
    assert res1["status"] == "pass" and res2["status"] == "pass"
    assert created == 1

    # Pending check task must cancel cleanly when library.close() runs.
    never = asyncio.Event()
    task = asyncio.create_task(never.wait())
    owner.library._check_tasks = {task}
    await owner.library.close()
    assert task.cancelled()


def test_media_behavior_single_connect_and_no_pillow_deprecation(tmp_path):
    store = MediaBehavior(tmp_path)
    img_item = candidate(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        digest, phash, _ = store.fingerprint(img_item)
    assert digest and len(phash) == 64
    assert not any("getdata" in str(w.message) for w in caught)

    opens = 0
    real_connect = store.connect

    def counting_connect():
        nonlocal opens
        opens += 1
        return real_connect()

    store.connect = counting_connect
    plain_prompt = store.remember_and_prompt(event("你好呀"), attach_prompt=True)
    assert opens == 1
    assert "视觉表达偏好" in plain_prompt

    opens = 0
    pref_prompt = store.remember_and_prompt(event("以后少发表情"), attach_prompt=True)
    assert opens == 2  # 1 connection for preference read+upsert + 1 for decision audit
    assert "少用表情" in pref_prompt


@pytest.mark.asyncio
async def test_internal_narrative_turns_skipped_and_public_summary_exposed(owner, tmp_path):
    req = NS(system_prompt="base")
    internal = event("hello")
    internal.set_extra("_narrative_internal_v1", True)
    owner.behavior.remember_and_prompt = lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("should not query behavior on internal narrative turn")
    )
    owner.memes._start_joint_meme_prefetch = AsyncMock()
    owner.memes._prepare_joint_meme_reply = AsyncMock()
    await owner._start_joint_meme_prefetch(internal, req)
    await owner._prepare_joint_meme_reply(internal, req)
    assert not owner.memes._start_joint_meme_prefetch.called
    assert not owner.memes._prepare_joint_meme_reply.called
    assert req.system_prompt == "base"

    # Public summary & World public reference directory integration.
    summary = owner.get_dynamics_summary("qq:GroupMessage:1")
    assert summary["schema_version"] == 1
    assert isinstance(summary["characters"], list)

    custom_dir = tmp_path / "world-assets"
    custom_dir.mkdir()
    fake_world = NS(get_reference_assets_directory=lambda: str(custom_dir))
    fake_plugin = NS(
        context=NS(
            get_registered_star=lambda name: fake_world
            if name == "astrbot_plugin_dynamics_world"
            else None
        )
    )
    assert expected_directory(fake_plugin) == custom_dir.resolve()


@pytest.mark.asyncio
async def test_media_tasks_indexes_and_paged_lookup(owner):
    await owner.tasks.observe("job:j1", {"job_id": "j1", "stage": "ready"})
    with owner.tasks._connect() as db:
        indexes = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert {"idx_tasks_updated", "idx_operations_created", "idx_tasks_job_id"} <= indexes
    scoped = await asyncio.to_thread(owner.tasks._for_jobs, ["j1"])
    assert "job:j1" in scoped


@pytest.mark.asyncio
async def test_embedding_incremental_numpy_argpartition_and_faiss_retirement(tmp_path):
    vecs: dict[str, tuple[bytes, int, str]] = {}

    class _FakeProvider:
        def get_model(self):
            return "text-embedding-3-small"

        def get_dim(self):
            return 4

        async def get_embedding(self, text):
            return (await self.get_embeddings([text]))[0]

        async def get_embeddings(self, texts):
            out = []
            for t in texts:
                if t == "a":
                    out.append([1.0, 0.0, 0.0, 0.0])
                elif t == "b":
                    out.append([0.0, 1.0, 0.0, 0.0])
                elif t == "c":
                    out.append([0.7, 0.7, 0.0, 0.0])
                else:
                    out.append([0.9, 0.2, 0.0, 0.0])
            return out

    load_calls = 0

    class _FakeDB:
        def get_meta_value(self, key):
            return None

        def set_meta_value(self, key, val):
            pass

        def get_all_paths(self):
            return list(vecs)

        def enqueue_search_refresh(self, paths):
            pass

        def upsert_embedding(self, path, blob, *, dim, model_sig):
            vecs[path] = (blob, dim, model_sig)

        def delete_embedding(self, path):
            vecs.pop(path, None)

        def load_embeddings_by_sig(self, sig):
            nonlocal load_calls
            load_calls += 1
            return [
                {"path": p, "vector": b, "dim": d}
                for p, (b, d, s) in vecs.items()
                if s == sig
            ]

    plugin = NS(
        enable_embedding_search=True,
        embedding_provider_id="p1",
        provider=_FakeProvider(),
        plugin_config=NS(data_dir=str(tmp_path)),
        db_service=_FakeDB(),
    )
    svc = EmbeddingService(plugin)
    svc._get_provider = lambda: plugin.provider
    svc._faiss_available = False

    # Retired Faiss client closes immediately on _ensure_provider_ready.
    closed = []

    class _FakeFaiss:
        async def close(self):
            closed.append(True)

    svc._retired_faiss.append(_FakeFaiss())
    await svc._ensure_provider_ready()
    assert closed == [True]

    # Seed initial fallback matrix load when Faiss is disabled / unavailable.
    svc._init_faiss = lambda: False
    svc._load_fallback_matrix()
    assert load_calls == 1

    # Insert & delete must update in-memory fallback matrix incrementally without SQLite reload.
    assert await svc._fallback_insert("p/a.png", "a")
    assert await svc._fallback_insert("p/b.png", "b")
    assert await svc._fallback_insert("p/c.png", "c")
    assert svc._fallback_loaded is True
    assert len(svc._fallback_paths) == 3

    top = await svc._fallback_search("q", k=2)
    assert load_calls == 1
    assert len(top) == 2
    assert top[0][0] == "p/a.png"

    assert await svc.delete_by_path("p/b.png")
    assert svc._fallback_loaded is True
    assert "p/b.png" not in svc._fallback_paths
    assert isinstance(svc._fallback_matrix, np.ndarray)
    assert svc._fallback_matrix.shape[0] == 2


def test_build_release_excludes_upstream_docs_from_install_zip(tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    for rel, text in {
        "metadata.yaml": "name: astrbot_plugin_visual_media\nversion: 0.5.0-rc.1\n",
        "main.py": "pass\n",
        "media_settings.py": "pass\n",
        "LICENSE": "AGPL",
        "THIRD_PARTY_NOTICES.md": "notices",
        "views/workshop/index.html": "<html/>",
        "views/workshop/studio/index.html": "<html/>",
        "docs/TASKS-AND-REFERENCES.md": "# tasks",
        "docs/upstream-generation/plugin-api.md": "# upstream",
        "docs/upstream-memes/external-source.md": "# upstream meme",
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    out = tmp_path / "out"
    build(root, out)
    with zipfile.ZipFile(out / "astrbot_plugin_visual_media-0.5.0-rc.1-install.zip") as zi:
        inames = set(zi.namelist())
    with zipfile.ZipFile(out / "astrbot_plugin_visual_media-0.5.0-rc.1-source.zip") as zs:
        snames = set(zs.namelist())

    assert "astrbot_plugin_visual_media/docs/TASKS-AND-REFERENCES.md" in inames
    assert "astrbot_plugin_visual_media/docs/upstream-generation/plugin-api.md" not in inames
    assert "astrbot_plugin_visual_media/docs/upstream-memes/external-source.md" not in inames
    assert "astrbot_plugin_visual_media/docs/upstream-generation/plugin-api.md" in snames
