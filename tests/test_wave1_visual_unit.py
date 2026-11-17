"""Wave 1 optimization tests for GenerationTracker and JobScheduler."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tl.core.job_store import SQLiteJobStore
from tl.core.scheduler import JobScheduler
from tl.core.states import JobState
from tl.generation_tracker import GenerationTracker


@pytest.mark.asyncio
async def test_generation_tracker_debounces_running_updates_and_prunes_expired_failed(tmp_path):
    tracker = GenerationTracker(tmp_path, max_records=20)
    save_calls = 0
    original_save_sync = tracker._save_sync

    def counting_save():
        nonlocal save_calls
        save_calls += 1
        return original_save_sync()

    tracker._save_sync = counting_save
    try:
        record = await tracker.begin(
            source="webui",
            prompt="burst updates",
            params={},
            requester={},
        )
        assert save_calls == 1

        for idx in range(5):
            await tracker.update(record["job_id"], text_content=f"step {idx}")
        # Burst updates on running jobs defer disk writes instead of flushing every update.
        assert save_calls == 1
        await asyncio.sleep(0.35)
        assert save_calls == 2

        await tracker.complete(
            record["job_id"],
            image_files=[],
            text_content="done",
            stats={},
        )
        assert save_calls == 3

        # Seed an expired failed record (>30d), an expired shared failed record, and a recent failed record.
        old_stamp = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        recent_stamp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        tracker._jobs["job-old-failed"] = {
            "job_id": "job-old-failed",
            "parent_job_id": None,
            "status": "failed",
            "shared": False,
            "shared_at": None,
            "images": [],
            "created_at": old_stamp,
            "finished_at": old_stamp,
        }
        tracker._jobs["job-old-shared"] = {
            "job_id": "job-old-shared",
            "parent_job_id": None,
            "status": "failed",
            "shared": True,
            "shared_at": old_stamp,
            "images": [],
            "created_at": old_stamp,
            "finished_at": old_stamp,
        }
        tracker._jobs["job-recent-failed"] = {
            "job_id": "job-recent-failed",
            "parent_job_id": None,
            "status": "failed",
            "shared": False,
            "shared_at": None,
            "images": [],
            "created_at": recent_stamp,
            "finished_at": recent_stamp,
        }
        tracker._prune_locked()
        assert tracker.get("job-old-failed") is None
        assert tracker.get("job-old-shared") is not None
        assert tracker.get("job-recent-failed") is not None
    finally:
        await tracker.close()


@pytest.mark.asyncio
async def test_scheduler_recover_runs_prune_and_checkpoint_and_handles_minimal_store(tmp_path):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    pruned_calls = []
    checkpointed = 0

    async def fake_prune(before, states):
        pruned_calls.append((before, set(states)))
        return 2

    async def fake_checkpoint():
        nonlocal checkpointed
        checkpointed += 1

    store.prune_jobs = fake_prune
    store.checkpoint = fake_checkpoint

    class _Provider:
        pass

    scheduler = JobScheduler(store, provider=_Provider())
    try:
        recovered = await scheduler.recover()
        assert recovered == []
        assert len(pruned_calls) == 1
        assert JobState.FAILED in pruned_calls[0][1]
        assert checkpointed == 1
    finally:
        await scheduler.close()
        await store.close()

    # Minimal mock stores without prune_jobs/checkpoint should still recover cleanly.
    minimal_store = SimpleNamespace(
        owner_id="owner-1",
        heartbeat=lambda: asyncio.sleep(0),
        load_owner_jobs=lambda owner_id: asyncio.sleep(0, result=[]),
        recoverable_jobs=lambda: asyncio.sleep(0, result=[]),
        list_jobs=lambda **kwargs: asyncio.sleep(0, result=[]),
        close=lambda: asyncio.sleep(0),
    )
    minimal_scheduler = JobScheduler(minimal_store, provider=_Provider())
    try:
        await minimal_scheduler.recover()
    finally:
        await minimal_scheduler.close()
