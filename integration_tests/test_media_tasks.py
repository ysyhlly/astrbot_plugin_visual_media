import asyncio
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest

from astrbot_plugin_visual_media.media_tasks import MediaTasks
from test_media_library import generated
from test_http_and_generation import app_for


async def failed(owner, umo=""):
    row = await owner.generation.generation_tracker.begin(
        source="webui", prompt="test scene", params={}, requester={"umo": umo}
    )
    await owner.generation.generation_tracker.fail(
        row["job_id"], error="provider unavailable"
    )
    # Simulate the submission observer's proof that all original inputs are reusable.
    await owner.tasks.update(
        "job:" + row["job_id"], {"job_id": row["job_id"], "input_complete": True}
    )
    return row["job_id"]


@pytest.mark.asyncio
async def test_all_failed_jobs_appear_and_existing_images_never_offer_regeneration(
    owner,
):
    job = await failed(owner)
    successful, _ = await generated(owner)
    listing = await owner.tasks.listing()
    assert {r["job_id"] for r in listing["items"]} == {job, successful}
    card = await owner.tasks.detail(job)
    assert card["stage"] == "generation_failed" and card["can_retry"]
    card = await owner.tasks.detail(successful)
    assert not card["can_retry"] and card["can_resend"]


@pytest.mark.asyncio
async def test_retry_reuses_params_once_and_keeps_original(owner, monkeypatch):
    job = await failed(owner, "qq:GroupMessage:1234")
    monkeypatch.setattr(
        owner.generation.web_studio_service,
        "validate_payload",
        lambda payload: ({}, None),
    )
    submitted = AsyncMock(return_value={"job_id": "new-job"})
    monkeypatch.setattr(owner.generation.web_studio_service, "generate", submitted)
    first, second = await asyncio.gather(
        *(owner.tasks.recover(job, "retry", "same-operation-123456") for _ in range(2))
    )
    assert first == second and first["ok"]
    assert submitted.await_count == 1
    assert submitted.await_args.args[0] == {
        "reuse_job_id": job,
        "reference_names": [],
        "image_count": 1,
    }
    assert submitted.await_args.kwargs["requester"]["umo"] == "qq:GroupMessage:1234"
    assert owner.generation.generation_tracker.get(job)["status"] == "failed"
    assert not (await owner.tasks.detail(job))["can_retry"]


@pytest.mark.asyncio
async def test_missing_reference_or_changed_route_never_starts_retry(
    owner, monkeypatch
):
    ref_job, name = await generated(owner, umo=None)
    job = await owner.generation.generation_tracker.begin(
        source="webui", prompt="draw", params={}, requester={}, reference_names=[name]
    )
    await owner.generation.generation_tracker.fail(job["job_id"], error="failed")
    await owner.tasks.update(
        "job:" + job["job_id"], {"job_id": job["job_id"], "input_complete": True}
    )
    owner.generation.generation_tracker.gallery_image_path(name).unlink()
    generate = AsyncMock()
    monkeypatch.setattr(owner.generation.web_studio_service, "generate", generate)
    result = await owner.tasks.recover(job["job_id"], "retry", "missing-reference-123")
    assert not result["ok"] and not generate.called
    other = await failed(owner)
    monkeypatch.setattr(
        owner.generation.web_studio_service,
        "validate_payload",
        lambda data: ({}, "model changed"),
    )
    result = await owner.tasks.recover(other, "retry", "changed-model-123456")
    assert not result["ok"] and not generate.called


@pytest.mark.asyncio
async def test_resend_uses_only_archived_images_and_original_destination(
    owner, monkeypatch
):
    job, name = await generated(owner, umo="qq:GroupMessage:1234")
    sender = AsyncMock(return_value=True)
    owner.context.send_message = sender
    generate = AsyncMock()
    monkeypatch.setattr(owner.generation.web_studio_service, "generate", generate)
    a, b = await asyncio.gather(
        *(owner.tasks.recover(job, "resend", "resend-operation-123") for _ in range(2))
    )
    assert a == b and a["ok"] and sender.await_count == 1 and not generate.called
    assert str(sender.await_args.args[0]) == "qq:GroupMessage:1234"
    assert len(sender.await_args.args[1].chain) == 1
    card = await owner.tasks.detail(job)
    assert card["stage"] == "delivered" and not card["can_resend"]
    again = await owner.tasks.recover(job, "resend", "another-operation-123")
    assert not again["ok"] and sender.await_count == 1


@pytest.mark.asyncio
async def test_send_failure_stays_uncertain_and_does_not_generate(owner):
    job, _ = await generated(owner)
    owner.context.send_message = AsyncMock(side_effect=RuntimeError("connection lost"))
    result = await owner.tasks.recover(job, "resend", "failure-operation-123")
    assert not result["ok"]
    assert (await owner.tasks.detail(job))["stage"] == "delivery_uncertain"
    assert owner.generation.generation_tracker.get(job)["images"]


@pytest.mark.asyncio
async def test_dashboard_generation_cannot_be_sent_to_an_invented_target(owner):
    job, _ = await generated(owner, umo=None)
    owner.context.send_message = AsyncMock(return_value=True)
    result = await owner.tasks.recover(job, "resend", "no-target-operation-123")
    assert not result["ok"] and not owner.context.send_message.called


@pytest.mark.asyncio
async def test_recovery_rechecks_current_group_access_without_consuming_quota(owner):
    job, _ = await generated(owner, umo="qq:GroupMessage:1234")
    owner.generation.cfg.group_limit_mode = "blacklist"
    owner.generation.cfg.group_limit_list = ["1234"]
    owner.context.send_message = AsyncMock(return_value=True)
    assert not (await owner.tasks.detail(job))["can_resend"]
    result = await owner.tasks.recover(job, "resend", "blocked-group-operation")
    assert not result["ok"] and not owner.context.send_message.called


@pytest.mark.asyncio
async def test_check_stage_is_separate_from_generation_and_delivery(owner):
    job, _ = await generated(owner, umo=None)
    await owner.tasks.update(
        "job:" + job,
        {
            "job_id": job,
            "character": "test",
            "stage": "checking",
            "profile": {"references": []},
        },
    )
    assert (await owner.tasks.detail(job))["stage"] == "checking"
    await owner.tasks.update(
        "job:" + job,
        {
            "stage": "ready",
            "check": {
                "status": "mismatch",
                "issues": ["发色不同"],
                "reason": "颜色差异",
            },
        },
    )
    card = await owner.tasks.detail(job)
    assert (
        card["stage"] == "ready"
        and card["check"]["status"] == "mismatch"
        and not card["can_retry"]
    )


@pytest.mark.asyncio
async def test_interrupted_recovery_is_not_automatically_replayed(owner):
    job, _ = await generated(owner)
    owner.tasks._operation("pending-operation-123", job, "resend")
    await owner.tasks.update("job:" + job, {"job_id": job, "stage": "delivering"})
    resumed = MediaTasks(owner)
    assert (await resumed.detail(job))["stage"] == "delivery_uncertain"
    with pytest.raises(ValueError, match="尚未确认"):
        await resumed.recover(job, "resend", "pending-operation-123")


@pytest.mark.asyncio
async def test_character_retry_keeps_snapshot_and_waits_for_manual_delivery(
    owner, monkeypatch
):
    job = await failed(owner, "qq:GroupMessage:1234")
    profile = {
        "key": "old",
        "name": "Original",
        "appearance": "old hair",
        "style": "old style",
        "references": [],
    }
    await owner.tasks.update(
        "job:" + job,
        {
            "job_id": job,
            "character": "old",
            "profile": profile,
            "prepared_prompt": "EXACT original prompt",
            "user_prompt": "smile",
            "target": "qq:GroupMessage:1234",
        },
    )
    captured = {}

    async def submit(**kwargs):
        captured.update(kwargs)
        return {"task_id": "new-task"}

    async def task(*args, **kwargs):
        return {"job_id": "new-job"}

    monkeypatch.setattr(
        owner.generation, "get_service", lambda: NS(submit=submit, get_task=task)
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(
        "astrbot_plugin_visual_media.tl.background_dispatch.dispatch_generation_result",
        dispatch,
    )
    result = await owner.tasks.recover(job, "retry", "role-retry-operation")
    assert result["ok"] and result["job_id"] == "new-job"
    assert (
        captured["prompt"] == "EXACT original prompt"
        and captured["umo"] == "qq:GroupMessage:1234"
    )
    await captured["on_complete"](
        {
            "task_id": "new-task",
            "job_id": "new-job",
            "status": "succeeded",
            "image_paths": [],
        }
    )
    assert not dispatch.called, "manual retry must not send automatically"
    stored = owner.tasks._all()["new-task"]
    assert (
        stored["profile"] == profile
        and stored["manual_retry"]
        and stored["target"] == "qq:GroupMessage:1234"
    )


@pytest.mark.asyncio
async def test_projection_failure_does_not_abort_generation_callback(
    owner, monkeypatch
):
    await owner.library.save_profile({"key": "test", "name": "test", "references": []})
    captured = {}

    async def submit(**kwargs):
        captured.update(kwargs)
        return {"task_id": "task"}

    monkeypatch.setattr(owner.generation, "get_service", lambda: NS(submit=submit))
    monkeypatch.setattr(owner.generation.rate_limiter, "allows_group", lambda e: True)
    monkeypatch.setattr(
        owner.tasks, "update", AsyncMock(side_effect=sqlite3.OperationalError("busy"))
    )
    from test_media_behavior import event

    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "astrbot_plugin_visual_media.tl.background_dispatch.dispatch_generation_result",
        dispatch,
    )
    assert (await owner.library.generate_character("test", "smile", event()))[
        "task_id"
    ] == "task"
    await captured["on_complete"](
        {"task_id": "task", "job_id": "job", "status": "succeeded", "image_paths": []}
    )
    assert dispatch.await_count == 1


@pytest.mark.asyncio
async def test_unknown_retry_submission_is_not_silently_repeated(owner, monkeypatch):
    job = await failed(owner)
    monkeypatch.setattr(
        owner.generation.web_studio_service, "validate_payload", lambda p: ({}, None)
    )
    submit = AsyncMock(side_effect=RuntimeError("connection lost after admission"))
    monkeypatch.setattr(owner.generation.web_studio_service, "generate", submit)
    assert not (await owner.tasks.recover(job, "retry", "uncertain-retry-123"))["ok"]
    assert not (await owner.tasks.detail(job))["can_retry"]
    assert not (await owner.tasks.recover(job, "retry", "new-operation-12345"))["ok"]
    assert submit.await_count == 1


@pytest.mark.asyncio
async def test_upload_or_unarchived_references_require_review_instead_of_silent_drop(
    owner,
):
    from types import SimpleNamespace as NS

    job = await failed(owner)
    req = NS(
        prompt="test", reference_images=("cache-ref",), metadata={}, watermark=None
    )
    await owner.generation._capture_task_inputs(NS(job_id=job + "-input", request=req))
    stored = owner.tasks._all()["job:" + job + "-input"]
    assert stored["input_complete"] is False and stored["input_reference_count"] == 1
    await owner.tasks.update("job:" + job, {"input_complete": False})
    assert not (await owner.tasks.detail(job))["can_retry"]
    req.reference_images = ()
    await owner.generation._capture_task_inputs(NS(job_id=job + "-text", request=req))
    assert owner.tasks._all()["job:" + job + "-text"]["input_complete"] is True


@pytest.mark.asyncio
async def test_reference_and_recovery_endpoints_are_admin_only(owner):
    public_job, name = await generated(owner, umo=None)
    public = await owner.library.save_generation(
        public_job, public=True, description="public reference"
    )
    private_job, _ = await generated(owner, color="red")
    private = await owner.library.save_generation(private_job)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(owner)), base_url="http://test"
    ) as client:
        prefix = "/astrbot_plugin_visual_media/media"
        for route in ("/tasks", "/references", "/reference?hash=" + public["hash"]):
            assert (await client.get(prefix + route)).status_code == 403
        assert (
            await client.post(prefix + "/tasks/recover", json={})
        ).status_code == 403
        client.headers["x-test-user"] = "admin"
        listing = (await client.get(prefix + "/references")).json()["data"]
        assert [item["hash"] for item in listing["items"]] == [public["hash"]]
        assert (
            await client.get(prefix + "/reference", params={"hash": private["hash"]})
        ).status_code == 404
        assert (
            (await client.get(prefix + "/reference", params={"hash": public["hash"]}))
            .json()["data"]["url"]
            .startswith("data:image/png;base64,")
        )
        assert (
            await client.get(
                prefix + "/task-preview", params={"name": "../cmd_config.json"}
            )
        ).status_code == 404
        assert (
            await client.get(prefix + "/task-preview", params={"name": name})
        ).status_code == 200
