import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_visual_media.character_check import (
    check_character,
    parse_check_response,
)
from astrbot_plugin_visual_media.media_behavior import MediaBehavior
from astrbot_plugin_visual_media.media_receipts import image_receipt
from astrbot_plugin_visual_media.media_runtime import MediaRuntime
from astrbot_plugin_visual_media.media_settings import MediaSettings
from test_media_behavior import candidate, event


@pytest.mark.parametrize(
    "session,receipt,confirmed,target",
    [
        ("", "qq:GroupMessage:1", True, "qq:GroupMessage:1"),
        ("qq:GroupMessage:2", "qq:GroupMessage:2", True, "qq:GroupMessage:2"),
        ("2", "qq:GroupMessage:2", True, "qq:GroupMessage:2"),
        ("qq:GroupMessage:2", "qq:GroupMessage:1", False, "qq:GroupMessage:2"),
        ("", "qq:GroupMessage:2", False, "qq:GroupMessage:1"),
    ],
)
def test_receipt_destination_exact_match(session, receipt, confirmed, target):
    e = event()
    result = NS(content=[NS(text="Message sent to session " + receipt)])
    parsed = image_receipt(
        e,
        {
            "session": session,
            "messages": [{"type": "image", "url": "https://example.invalid/a"}],
        },
        result,
    )
    assert parsed["confirmed"] is confirmed
    assert parsed["event"].unified_msg_origin == target
    if target != e.unified_msg_origin:
        assert parsed["event"].get_sender_id() == ""


@pytest.mark.parametrize(
    "receipt",
    [
        "error: Message sent to session qq:GroupMessage:1",
        "Message sent to session qq:GroupMessage:1\nerror: partial failure",
        "error: failed to send",
    ],
)
def test_failed_or_ambiguous_receipts_cannot_mark_sent(receipt):
    parsed = image_receipt(
        event(),
        {"messages": [{"type": "image", "url": "x"}]},
        NS(content=[NS(text=receipt)]),
    )
    assert not parsed["confirmed"]


@pytest.mark.asyncio
async def test_cross_session_does_not_poison_source_or_source_member(
    owner, tmp_path, monkeypatch
):
    one = event()
    other = event(umo="qq:GroupMessage:2")
    c = candidate(tmp_path)
    monkeypatch.setattr(
        owner.memes.plugin_api, "_get_index", lambda: {c["path"]: {"hash": "test"}}
    )
    old_tracker = AsyncMock()
    monkeypatch.setattr(owner.memes, "_track_external_image_delivery", old_tracker)
    await owner._track_external_image_delivery(
        one,
        NS(name="send_message_to_user"),
        {
            "session": other.unified_msg_origin,
            "messages": [{"type": "image", "path": c["path"]}],
        },
        NS(content=[NS(text="Message sent to session " + other.unified_msg_origin)]),
    )
    assert not old_tracker.called
    assert owner.behavior.filter_candidates(one, [c]) == [c]
    assert owner.behavior.filter_candidates(other, [c]) == []
    receipts = [r for r in owner.behavior.records() if r["phase"] == "delivered"]
    assert receipts and all(
        r["scope"] == owner.behavior.identity(other)[0] and r["member"] == ""
        for r in receipts
    )


@pytest.mark.parametrize(
    "payload,status,reason",
    [
        (
            {"status": "pass", "issues": ["发色不符"], "reason": "通过"},
            "unknown",
            "contradictory_vision_response",
        ),
        (
            {"status": "mismatch", "issues": [], "reason": "有问题"},
            "unknown",
            "mismatch_without_evidence",
        ),
        (
            {"status": "unknown", "issues": [], "reason": "头饰被遮挡"},
            "unknown",
            "头饰被遮挡",
        ),
        (
            {"status": "pass", "issues": [], "reason": "全部特征符合"},
            "pass",
            "全部特征符合",
        ),
    ],
)
def test_check_contradiction_is_not_pass(payload, status, reason):
    result = parse_check_response(json.dumps(payload), 1, 1, 1)
    assert result["status"] == status and result["reason"] == reason


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"status": "pass", "issues": None, "reason": "ok"},
        {"status": "pass", "issues": [" "], "reason": "ok"},
        {"status": "pass", "issues": [], "reason": None},
    ],
)
def test_malformed_check_rejected(payload):
    with pytest.raises((ValueError, TypeError)):
        parse_check_response(json.dumps(payload), 1, 1, 1)


def test_partial_check_never_claims_all_passed():
    assert (
        parse_check_response('{"status":"pass","issues":[],"reason":"符合"}', 2, 3, 1)[
            "status"
        ]
        == "unknown"
    )


@pytest.mark.asyncio
async def test_disabled_check_and_missing_output_do_not_call_provider(owner, tmp_path):
    owner.context.llm_generate = AsyncMock()
    owner.raw_config["media"] = {"character_check_enabled": False}
    assert (await check_character(owner, {}, [], [], "test"))["status"] == "skipped"
    owner.raw_config["media"]["character_check_enabled"] = True
    path = candidate(tmp_path)["path"]
    assert (
        await check_character(
            owner, {}, [path, str(tmp_path / "missing.png")], [path], "test"
        )
    )["status"] == "unknown"
    assert not owner.context.llm_generate.called


def test_settings_validate_types_and_bounds():
    value = MediaSettings.parse(
        {
            "preferences_enabled": "false",
            "repeat_window_seconds": -1,
            "max_records": True,
            "character_check_timeout": 999,
            "character_check_provider": " test ",
        }
    )
    assert value.preferences_enabled is True and value.repeat_window_seconds == 0
    assert value.max_records == 5000 and value.character_check_timeout == 120
    assert value.character_check_provider == "test"


def test_disabled_preferences_and_records_preserve_receipt_function(tmp_path):
    store = MediaBehavior(
        tmp_path, {"preferences_enabled": False, "decision_records_enabled": False}
    )
    e = event("少发表情")
    store.remember_explicit(e)
    assert store.preferences(e)["frequency"] == "auto" and store.prompt(e) == ""
    c = candidate(tmp_path)
    store.delivered(e, c)
    assert not store.records() and not store.preference_rows()
    assert store.reason(event(), c) == "recent_exact_repeat"


def test_repeat_switch_and_window_are_effective(tmp_path):
    cfg = {"repeat_guard_enabled": False}
    store = MediaBehavior(tmp_path, cfg)
    c = candidate(tmp_path)
    store.delivered(event(), c)
    assert store.filter_candidates(event(), [c]) == [c]
    cfg["repeat_guard_enabled"] = True
    assert not store.filter_candidates(event(), [c])
    cfg["repeat_window_seconds"] = 0
    assert store.filter_candidates(event(), [c]) == [c]


def test_fingerprint_cache_keeps_current_caption(tmp_path):
    store = MediaBehavior(tmp_path)
    c = candidate(tmp_path)
    first = store.fingerprint(c)
    assert store.fingerprint({**c, "overlay_text": "new"}) == (
        first[0],
        first[1],
        "new",
    )


@pytest.mark.asyncio
async def test_worker_does_not_block_loop_and_sync_gate_has_no_io(
    tmp_path, monkeypatch
):
    store = MediaRuntime(tmp_path)
    e = event()
    c = candidate(tmp_path)
    original = store.fingerprint
    observed = []

    def slow(value):
        time.sleep(0.04)
        observed.append(ticks)
        return original(value)

    monkeypatch.setattr(store, "fingerprint", slow)
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(15):
            ticks += 1
            await asyncio.sleep(0.005)

    prepared, _ = await asyncio.gather(store.prepare_candidates(e, [c]), heartbeat())
    assert ticks == 15
    assert max(observed) >= 3, "image preparation blocked the event loop"
    monkeypatch.setattr(
        store, "connect", lambda: pytest.fail("sync gate touched database")
    )
    monkeypatch.setattr(
        store, "fingerprint", lambda *a: pytest.fail("sync gate read image")
    )
    assert store.reserve_prepared(e, prepared[0])
    store.release_prepared(e, prepared[0])
    await store.close()


@pytest.mark.asyncio
async def test_concurrent_reservation_and_shutdown_flush(tmp_path):
    store = MediaRuntime(tmp_path)
    c = candidate(tmp_path)
    a, b = event(), event()
    ca, cb = await asyncio.gather(
        store.prepare_candidates(a, [c]), store.prepare_candidates(b, [c])
    )
    assert store.reserve_prepared(a, ca[0])
    assert not store.reserve_prepared(b, cb[0])
    await store.adelivered(a, ca[0])
    assert not store.reserve_prepared(b, cb[0])
    for i in range(15):
        store.queue_record(a, "text", "selected", "shutdown_test", {"n": i})
    fresh = (
        await store.prepare_candidates(a, [candidate(tmp_path, "other.png", "red")])
    )[0]
    assert store.reserve_prepared(a, fresh)
    store.release_prepared(a, fresh)
    store.stop_sends = True
    assert not store.reserve_prepared(a, fresh)
    store.stop_sends = False
    await store.close()
    assert len([r for r in store.records() if r["reason"] == "shutdown_test"]) == 15
    assert not store.reserve_prepared(a, fresh)


def test_zero_version_upgrade_keeps_existing_rows_and_future_version_fails(tmp_path):
    store = MediaBehavior(tmp_path)
    e = event("少发表情")
    store.remember_explicit(e)
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA user_version=0")
    upgraded = MediaBehavior(tmp_path)
    assert upgraded.preferences(e)["frequency"] == "less" and upgraded.records()
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="newer"):
        MediaBehavior(tmp_path)


def test_faiss_missing_falls_back_to_numpy(monkeypatch):
    import builtins

    from astrbot_plugin_visual_media.core.search.embedding_service import (
        EmbeddingService,
    )

    service = EmbeddingService(NS())
    provider = NS()
    monkeypatch.setattr(service, "_get_provider", lambda: provider)
    monkeypatch.setattr(service, "_reset_provider_state_if_changed", lambda: None)
    monkeypatch.setattr(service, "_provider_identity", lambda p: "test")
    monkeypatch.setattr(service, "_fallback_signature", lambda p: "test:2")
    service._observed_identity = "test"
    service._observed_dim = 2
    original = builtins.__import__

    def without_faiss(name, *args, **kwargs):
        if name == "faiss":
            raise ImportError("optional dependency absent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_faiss)
    assert not service._init_faiss()
    assert service._init_fallback()


def test_candidate_batch_uses_constant_database_reads(tmp_path, monkeypatch):
    from contextlib import contextmanager

    store = MediaBehavior(tmp_path)
    c = candidate(tmp_path)
    original = store.connect
    reads = []

    @contextmanager
    def traced():
        with original() as db:
            db.set_trace_callback(
                lambda sql: reads.append(sql) if sql.startswith("SELECT") else None
            )
            yield db

    monkeypatch.setattr(store, "connect", traced)
    assert len(store.filter_candidates(event(), [c] * 50)) == 50
    assert len(reads) == 2
