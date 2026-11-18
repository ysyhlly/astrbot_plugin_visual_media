import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_visual_media.media_runtime import MediaRuntime
from astrbot_plugin_visual_media.tl.delivery_evidence import (
    background_delivery_confirmed,
    expected_image_keys,
    note_confirmed_images,
)
from test_media_behavior import candidate, event


@pytest.mark.asyncio
async def test_disabled_preferences_override_an_already_prepared_snapshot(tmp_path):
    settings = {"preferences_enabled": True}
    store = MediaRuntime(tmp_path, settings)
    e = event("少发表情")
    c = candidate(tmp_path)
    store.remember_explicit(e)
    prepared = (await store.prepare_candidates(e, [c]))[0]
    await store.adelivered(event(), candidate(tmp_path, "prior.png", "red"))
    assert store.reason_prepared(e, prepared) == "member_prefers_fewer_memes"
    settings["preferences_enabled"] = False
    assert store.reason_prepared(e, prepared) == ""
    await store.close()


@pytest.mark.asyncio
async def test_sqlite_audit_failure_is_counted_not_reported_as_healthy(
    tmp_path, monkeypatch
):
    store = MediaRuntime(tmp_path)

    def broken():
        raise sqlite3.OperationalError("simulated busy database")

    monkeypatch.setattr(store, "connect", broken)
    store.queue_record(event(), "text", "selected", "probe")
    await store.close()
    assert store.dropped_records == 1


@pytest.mark.asyncio
async def test_direct_record_and_delivered_record_failures_count_without_rethrow(
    tmp_path, monkeypatch
):
    store = MediaRuntime(tmp_path)
    e = event()
    c = (await store.prepare_candidates(e, [candidate(tmp_path)]))[0]

    def broken():
        raise sqlite3.OperationalError("simulated busy database")

    monkeypatch.setattr(store, "connect", broken)
    await store.arecord(e, "image", "delivered", "receipt")
    await store.adelivered(e, c)
    assert store.dropped_records == 2
    assert store.reason_prepared(e, c) == "recent_exact_repeat", (
        "confirmed send stays in memory despite disk failure"
    )
    await store.close()


@pytest.mark.asyncio
async def test_intentionally_disabled_records_are_not_counted_as_lost(tmp_path):
    store = MediaRuntime(tmp_path, {"decision_records_enabled": False})
    await store.arecord(event(), "text", "selected", "test")
    store.queue_record(event(), "text", "selected", "test")
    await store.adelivered(event(), candidate(tmp_path))
    assert store.dropped_records == 0 and not store.records()
    await store.close()


@pytest.mark.asyncio
async def test_reenabled_preference_switch_uses_original_saved_preference(tmp_path):
    cfg = {"preferences_enabled": True}
    store = MediaRuntime(tmp_path, cfg)
    e = event("少发表情")
    store.remember_explicit(e)
    c = (await store.prepare_candidates(e, [candidate(tmp_path)]))[0]
    await store.adelivered(event(), candidate(tmp_path, "prior.png", "red"))
    cfg["preferences_enabled"] = False
    assert store.reserve_prepared(e, c)
    store.release_prepared(e, c)
    cfg["preferences_enabled"] = True
    assert not store.reserve_prepared(e, c)
    assert store.preferences(e)["frequency"] == "less"
    await store.close()


@pytest.mark.asyncio
async def test_character_background_delivery_keeps_its_task_id(owner, monkeypatch):
    await owner.library.save_profile(
        {"key": "test", "name": "Test", "appearance": "test", "references": []}
    )
    submitted = {}

    async def submit(**kwargs):
        submitted.update(kwargs)
        return {"task_id": "img-audit"}

    monkeypatch.setattr(owner.generation, "get_service", lambda: NS(submit=submit))
    monkeypatch.setattr(owner.generation.rate_limiter, "allows_group", lambda e: True)
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "astrbot_plugin_visual_media.tl.background_dispatch.dispatch_generation_result",
        dispatch,
    )
    accepted = await owner.library.generate_character("test", "smile", event())
    await submitted["on_complete"](
        {
            "task_id": accepted["task_id"],
            "job_id": "job-audit",
            "status": "succeeded",
            "image_paths": [],
            "image_urls": [],
        }
    )
    assert dispatch.await_args.kwargs.get("task_id") == "img-audit"


def test_text_notice_and_unrelated_images_are_not_generation_delivery():
    e = event()
    e._has_send_oper = True
    payload = {"image_paths": ["/tmp/a.png", "/tmp/b.png"]}
    e._visual_expected_image_keys = expected_image_keys(payload)
    assert not background_delivery_confirmed(e, payload)
    assert (
        note_confirmed_images(
            e, [{"url": "https://example.invalid/other.png"}], same_session=True
        )
        == 0
    )
    assert not background_delivery_confirmed(e, payload)
    note_confirmed_images(e, [{"path": "/tmp/a.png"}], same_session=True)
    assert not background_delivery_confirmed(e, payload)
    note_confirmed_images(e, [{"path": "file:///tmp/b.png"}], same_session=False)
    assert not background_delivery_confirmed(e, payload)
    note_confirmed_images(e, [{"path": "file:///tmp/b.png"}], same_session=True)
    assert background_delivery_confirmed(e, payload)
    assert background_delivery_confirmed(e, {"result": "plain failure notice"})


def test_batch_delivery_requires_each_distinct_expected_image():
    e = event()
    payload = {
        "items": [
            {"image_paths": ["/tmp/a.png"]},
            {"image_urls": ["https://example.invalid/b.png"]},
        ]
    }
    e._visual_expected_image_keys = expected_image_keys(payload)
    note_confirmed_images(e, [{"path": "/tmp/a.png"}], same_session=True)
    assert not background_delivery_confirmed(e, payload)
    note_confirmed_images(
        e, [{"url": "https://example.invalid/b.png"}], same_session=True
    )
    assert background_delivery_confirmed(e, payload)


@pytest.mark.asyncio
async def test_real_hook_sets_generation_marker_only_after_matching_current_receipt(
    owner,
):
    e = event()
    e._visual_generation_task = "job"
    payload = {"image_urls": ["https://example.invalid/generated.png"]}
    e._visual_expected_image_keys = expected_image_keys(payload)
    tool = NS(name="send_message_to_user")

    def receipt(target):
        return NS(content=[NS(text="Message sent to session " + target)])

    await owner._track_external_image_delivery(
        e,
        tool,
        {"messages": [{"type": "image", "url": "https://example.invalid/other.png"}]},
        receipt(e.unified_msg_origin),
    )
    assert not background_delivery_confirmed(e, payload)
    assert owner.behavior.records()[0]["mode"] == "image"
    await owner._track_external_image_delivery(
        e,
        tool,
        {"messages": [{"type": "image", "url": payload["image_urls"][0]}]},
        receipt(e.unified_msg_origin),
    )
    assert background_delivery_confirmed(e, payload)
    assert owner.behavior.records()[0]["details"]["task_id"] == "job"


@pytest.mark.asyncio
@pytest.mark.parametrize("image_sent", [False, True])
async def test_background_notifier_uses_image_receipt_not_just_text(
    owner, monkeypatch, image_sent
):
    from astrbot_plugin_visual_media.tl import background_notify as notify

    from astrbot.core.tools.message_tools import SendMessageToUserTool

    e = event()
    payload = {
        "task_id": "job",
        "image_urls": ["https://example.invalid/generated.png"],
    }
    monkeypatch.setattr(notify, "CronMessageEvent", lambda **kwargs: e)

    async def conversation(**kwargs):
        return NS(history="[]", cid="test")

    monkeypatch.setattr(notify, "_get_session_conv", conversation)
    monkeypatch.setattr(notify, "persist_agent_history", AsyncMock())
    counted = AsyncMock()
    monkeypatch.setattr(notify, "_bump_delivery_count", counted)

    class Runner:
        async def step_until_done(self, *args):
            e._has_send_oper = True
            if image_sent:
                note_confirmed_images(
                    e, [{"url": payload["image_urls"][0]}], same_session=True
                )
            if False:
                yield None

    async def build(**kwargs):
        return NS(agent_runner=Runner())

    monkeypatch.setattr(notify, "build_main_agent", build)
    owner.context.conversation_manager = NS()
    owner.context.get_llm_tool_manager = lambda: NS(
        get_builtin_tool=lambda kind: SendMessageToUserTool()
    )
    result = await notify.notify_llm_background_result(
        owner.generation, event(), payload, notice="test", scene="test"
    )
    assert result is image_sent
    assert counted.await_count == int(image_sent)


@pytest.mark.asyncio
async def test_disabling_audit_storage_does_not_disable_delivery_evidence(owner):
    owner.raw_config["media"] = {"decision_records_enabled": False}
    e = event()
    e._visual_generation_task = "job"
    payload = {"image_urls": ["https://example.invalid/result.png"]}
    e._visual_expected_image_keys = expected_image_keys(payload)
    await owner._track_external_image_delivery(
        e,
        NS(name="send_message_to_user"),
        {"messages": [{"type": "image", "url": payload["image_urls"][0]}]},
        NS(content=[NS(text="Message sent to session " + e.unified_msg_origin)]),
    )
    assert background_delivery_confirmed(e, payload)
    assert not owner.behavior.records()
