"""Tests for VisualMediaPlugin v2 series backend hooks in main.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from astrbot_plugin_visual_media.main import VisualMediaPlugin


def _make_stub_plugin(
    *,
    closed: bool = False,
    rows: list[dict] | None = None,
    profiles: list[dict] | None = None,
    meme_index: dict[str, dict] | None = None,
    gen_allowed: bool = True,
    meme_allowed: bool = True,
) -> VisualMediaPlugin:
    plugin = VisualMediaPlugin.__new__(VisualMediaPlugin)
    plugin._closed = closed
    plugin.generation = NS(
        generation_tracker=NS(records_snapshot=lambda: list(rows or [])),
        rate_limiter=NS(allows_group=lambda ev: gen_allowed),
    )
    plugin.library = NS(profiles=lambda: list(profiles or []))
    plugin.memes = NS(
        plugin_api=NS(_get_index=lambda: dict(meme_index or {})),
        plugin_config=NS(is_targets_allowed=lambda action, targets: meme_allowed),
    )
    return plugin


def test_get_series_summary_tones_and_states():
    now_iso = datetime.now(timezone.utc).isoformat()
    old_iso = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

    # Idle / ready
    idle_plugin = _make_stub_plugin(
        rows=[{"job_id": "j-ok", "status": "succeeded", "finished_at": now_iso, "images": ["a.png"]}],
        profiles=[{"key": "anan", "name": "夏目安安"}],
        meme_index={"/tmp/m1.png": {"hash": "h1", "scope_mode": "public"}},
    )
    res_idle = idle_plugin.get_series_summary()
    assert res_idle["badge"]["tone"] == "accent"
    assert res_idle["badge"]["count"] == 0
    assert "今日完成 1 次创作" in res_idle["pulse"]["headline"]
    assert res_idle["pulse"]["updated_at"]

    # Running / queued -> info
    running_plugin = _make_stub_plugin(
        rows=[
            {"job_id": "j1", "status": "running", "created_at": now_iso},
            {"job_id": "j2", "status": "queued", "created_at": now_iso},
            # Old failed (>24h) should not trigger danger
            {"job_id": "j-old", "status": "failed", "finished_at": old_iso},
        ]
    )
    res_run = running_plugin.get_series_summary()
    assert res_run["badge"]["tone"] == "info"
    assert res_run["badge"]["count"] == 2
    assert "2" in res_run["badge"]["label"]

    # Recent failed -> danger
    failed_plugin = _make_stub_plugin(
        rows=[
            {"job_id": "j-fail", "status": "failed", "finished_at": now_iso},
            {"job_id": "j-run", "status": "running", "created_at": now_iso},
        ]
    )
    res_fail = failed_plugin.get_series_summary()
    assert res_fail["badge"]["tone"] == "danger"
    assert res_fail["badge"]["count"] == 2

    # Closed -> muted
    closed_plugin = _make_stub_plugin(closed=True)
    assert closed_plugin.get_series_summary()["badge"]["tone"] == "muted"

    # Raising internals -> never raises uncaught exception
    broken = VisualMediaPlugin.__new__(VisualMediaPlugin)
    broken._closed = False
    broken.memes = NS(plugin_api=NS(_get_index=lambda: (_ for _ in ()).throw(RuntimeError("db down"))))
    broken.generation = NS(
        generation_tracker=NS(
            records_snapshot=lambda: (_ for _ in ()).throw(RuntimeError("tracker down"))
        )
    )
    broken.library = NS(profiles=lambda: (_ for _ in ()).throw(RuntimeError("lib down")))
    safe = broken.get_series_summary()
    assert safe["badge"]["tone"] in {"accent", "muted"}
    assert "headline" in safe["pulse"]


def test_get_series_scope_status_permissions_and_activity():
    now_iso = datetime.now(timezone.utc).isoformat()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    umo = "aiocqhttp:GroupMessage:12345"

    plugin = _make_stub_plugin(
        rows=[
            {"job_id": "j1", "status": "succeeded", "finished_at": now_iso, "requester": {"umo": umo}},
            {"job_id": "j2", "status": "succeeded", "finished_at": now_iso, "requester": {"umo": "aiocqhttp:GroupMessage:99999"}},
        ],
        meme_index={
            "/tmp/a.png": {"hash": "ha", "scope_mode": "local", "origin_target": f"umo:{umo}", "created_at": now_ts},
            "/tmp/b.png": {"hash": "hb", "scope_mode": "local", "origin_target": "umo:other", "created_at": now_ts},
        },
        gen_allowed=True,
        meme_allowed=True,
    )
    st = plugin.get_series_scope_status(umo)
    assert st["enabled"] is True
    assert st["supported"] is True
    assert st["activity_today"] == 2
    assert "今日 2 次" in st["summary"]
    assert st["version"]

    # Both disallowed -> enabled is False
    blocked = _make_stub_plugin(gen_allowed=False, meme_allowed=False)
    st_blocked = blocked.get_series_scope_status(umo)
    assert st_blocked["enabled"] is False
    assert st_blocked["supported"] is True

    # Closed -> not supported
    closed = _make_stub_plugin(closed=True)
    st_closed = closed.get_series_scope_status(umo)
    assert st_closed["enabled"] is False
    assert st_closed["supported"] is False


def test_series_search_items_respects_privacy_and_limit():
    now_iso = datetime.now(timezone.utc).isoformat()
    umo_a = "aiocqhttp:GroupMessage:100"
    umo_b = "aiocqhttp:GroupMessage:200"

    plugin = _make_stub_plugin(
        profiles=[
            {"key": "anan", "name": "夏目安安", "appearance": "白发森林裙", "style": "水彩"},
        ],
        rows=[
            {
                "job_id": "job-forest-1",
                "prompt": "安安在森林小屋喝茶",
                "status": "succeeded",
                "source": "webui",
                "created_at": now_iso,
                "requester": {"umo": umo_a},
            },
            {
                "job_id": "job-forest-2",
                "prompt": "森林秘密图",
                "status": "succeeded",
                "source": "llm_tool",
                "created_at": now_iso,
                "requester": {"umo": umo_b},
            },
        ],
        meme_index={
            "/tmp/pub.png": {
                "hash": "pub1",
                "desc": "森林猫猫点头",
                "category": "happy",
                "scope_mode": "public",
                "origin_target": "",
            },
            "/tmp/priv_a.png": {
                "hash": "priva",
                "desc": "森林专属梗A",
                "category": "funny",
                "scope_mode": "local",
                "origin_target": f"umo:{umo_a}",
            },
            "/tmp/priv_b.png": {
                "hash": "privb",
                "desc": "森林私密梗B",
                "category": "funny",
                "scope_mode": "local",
                "origin_target": f"umo:{umo_b}",
            },
        },
    )

    hits = plugin.series_search_items("森林", scope=umo_a, limit=10)
    kinds = {item["kind"] for item in hits}
    assert {"character", "task", "meme"} <= kinds
    titles = [item["title"] for item in hits]
    assert "夏目安安" in titles
    assert any("安安在森林小屋喝茶" in t for t in titles)
    assert "森林猫猫点头" in titles
    assert "森林专属梗A" in titles
    # Must NOT leak group B's task or private local meme
    assert not any("森林秘密图" in t for t in titles)
    assert not any("森林私密梗B" in t for t in titles)
    assert all(
        item["plugin_id"] == "astrbot_plugin_visual_media" and item["page"] == "workshop"
        for item in hits
    )

    assert plugin.series_search_items("", scope=umo_a) == []
    assert len(plugin.series_search_items("森林", scope=umo_a, limit=2)) == 2


def test_get_journal_events_returns_todays_creations_and_memes_sorted():
    now_dt = datetime.now(timezone.utc)
    older_today = (now_dt - timedelta(minutes=5)).isoformat()
    newer_today = now_dt.isoformat()
    yesterday_iso = (now_dt - timedelta(days=2)).isoformat()
    umo = "aiocqhttp:GroupMessage:777"

    plugin = _make_stub_plugin(
        rows=[
            {
                "job_id": "job-done",
                "status": "succeeded",
                "prompt": "雨后窗边的小茶杯",
                "source": "webui",
                "images": ["cup.png"],
                "finished_at": newer_today,
                "requester": {"umo": umo},
            },
            {
                "job_id": "job-running",
                "status": "running",
                "prompt": "还在画",
                "created_at": newer_today,
                "requester": {"umo": umo},
            },
            {
                "job_id": "job-old",
                "status": "succeeded",
                "prompt": "前天的画",
                "images": ["old.png"],
                "finished_at": yesterday_iso,
                "requester": {"umo": umo},
            },
        ],
        meme_index={
            "/tmp/m_today.png": {
                "hash": "m1",
                "desc": "开心拍手",
                "category": "happy",
                "character": "anan",
                "scope_mode": "local",
                "origin_target": f"umo:{umo}",
                "is_favorite": 1,
                "created_at": int(datetime.fromisoformat(older_today).timestamp()),
            },
            "/tmp/m_other.png": {
                "hash": "m2",
                "desc": "别群的表情",
                "category": "happy",
                "scope_mode": "local",
                "origin_target": "umo:aiocqhttp:GroupMessage:888",
                "created_at": int(now_dt.timestamp()),
            },
        },
    )

    events = plugin.get_journal_events(scope=umo, date="today", limit=10)
    assert len(events) == 2
    assert events[0]["id"] == "gen:job-done"
    assert events[0]["category"] == "visual_media"
    assert events[0]["source_label"] == "视觉工坊"
    assert events[0]["target_plugin"] == "astrbot_plugin_visual_media"
    assert events[0]["target_route"] == "workshop"
    assert events[0]["target_id"] == "job-done"
    assert events[0]["tone"] == "accent"

    assert events[1]["id"] == "meme:m1"
    assert events[1]["tone"] == "warm"
    assert events[1]["target_id"] == "m1"

    # Invalid date string -> empty list safely
    assert plugin.get_journal_events(date="not-a-date") == []


@pytest.mark.asyncio
async def test_real_owner_exposes_all_v2_hooks_safely(owner):
    summary = owner.get_series_summary()
    assert summary["badge"]["tone"] == "accent"
    assert summary["badge"]["count"] == 0
    assert summary["pulse"]["headline"]

    scope_status = owner.get_series_scope_status("aiocqhttp:GroupMessage:1001")
    assert scope_status["supported"] is True
    assert scope_status["enabled"] is True
    assert scope_status["activity_today"] == 0

    assert owner.series_search_items("不存在的词") == []
    assert owner.get_journal_events(date="today") == []


def test_meme_origin_matching_supports_legacy_group_user_and_isolates_multi_adapter():
    """Cover both legacy group:/user: and full umo: origins plus multi-adapter isolation."""
    from astrbot_plugin_visual_media.core.config.config import PluginConfig
    from astrbot_plugin_visual_media.core.search.meme_scope_service import (
        MemeScopeService,
    )

    now_ts = int(datetime.now(timezone.utc).timestamp())
    umo_a = "aiocqhttp:GroupMessage:1001"
    umo_b = "qq_official:GroupMessage:1001"
    umo_other_group = "aiocqhttp:GroupMessage:2002"
    umo_dm = "aiocqhttp:FriendMessage:42"

    meme_index = {
        "/tmp/legacy_grp.png": {
            "hash": "lg1",
            "desc": "抹茶猫猫-旧群收录",
            "category": "happy",
            "scope_mode": "local",
            "origin_target": "group:1001",
            "created_at": now_ts,
        },
        "/tmp/umo_a.png": {
            "hash": "ua1",
            "desc": "抹茶猫猫-适配器A专属",
            "category": "generated",
            "scope_mode": "local",
            "origin_target": f"umo:{umo_a}",
            "created_at": now_ts,
        },
        "/tmp/umo_b.png": {
            "hash": "ub1",
            "desc": "抹茶猫猫-适配器B专属",
            "category": "generated",
            "scope_mode": "local",
            "origin_target": f"umo:{umo_b}",
            "created_at": now_ts,
        },
        "/tmp/legacy_other.png": {
            "hash": "lo1",
            "desc": "抹茶猫猫-别群旧收录",
            "category": "happy",
            "scope_mode": "local",
            "origin_target": "group:2002",
            "created_at": now_ts,
        },
        "/tmp/legacy_dm.png": {
            "hash": "dm1",
            "desc": "抹茶猫猫-私聊收录",
            "category": "happy",
            "scope_mode": "local",
            "origin_target": "user:42",
            "created_at": now_ts,
        },
        "/tmp/legacy_user_1001.png": {
            "hash": "u1001",
            "desc": "抹茶猫猫-用户1001私聊",
            "category": "happy",
            "scope_mode": "local",
            "origin_target": "user:1001",
            "created_at": now_ts,
        },
    }

    plugin = _make_stub_plugin(meme_index=meme_index)

    # 1. Scope activity count for Adapter A group 1001: matches legacy group:1001 + umo_a (NOT umo_b, NOT user:1001, NOT group:2002)
    st_a = plugin.get_series_scope_status(umo_a)
    assert st_a["activity_today"] == 2
    st_b = plugin.get_series_scope_status(umo_b)
    assert st_b["activity_today"] == 2
    st_other = plugin.get_series_scope_status(umo_other_group)
    assert st_other["activity_today"] == 1
    st_dm = plugin.get_series_scope_status(umo_dm)
    assert st_dm["activity_today"] == 1

    # 2. Panel search for Adapter A group 1001
    hits_a = plugin.series_search_items("抹茶猫猫", scope=umo_a, limit=10)
    hashes_a = {item["deep_link"]["hash"] for item in hits_a if item["kind"] == "meme"}
    assert hashes_a == {"lg1", "ua1"}

    # Adapter B with SAME numeric group_id 1001 must NOT see Adapter A's umo-scoped meme
    hits_b = plugin.series_search_items("抹茶猫猫", scope=umo_b, limit=10)
    hashes_b = {item["deep_link"]["hash"] for item in hits_b if item["kind"] == "meme"}
    assert hashes_b == {"lg1", "ub1"}

    # Private chat FriendMessage:42 matches legacy user:42 and nothing from groups
    hits_dm = plugin.series_search_items("抹茶猫猫", scope=umo_dm, limit=10)
    hashes_dm = {item["deep_link"]["hash"] for item in hits_dm if item["kind"] == "meme"}
    assert hashes_dm == {"dm1"}

    # 3. Journal events match both legacy group:<id> and full umo:<umo>, while isolating adapters
    journal_a = plugin.get_journal_events(scope=umo_a, date="today", limit=20)
    assert {e["target_id"] for e in journal_a} == {"lg1", "ua1"}
    journal_b = plugin.get_journal_events(scope=umo_b, date="today", limit=20)
    assert {e["target_id"] for e in journal_b} == {"lg1", "ub1"}
    journal_dm = plugin.get_journal_events(scope=umo_dm, date="today", limit=20)
    assert {e["target_id"] for e in journal_dm} == {"dm1"}

    # 4. get_event_origin_target preserves full UMO when present and falls back to group:/user:
    cfg = PluginConfig({})
    ev_a = NS(unified_msg_origin=umo_a, get_group_id=lambda: "1001", get_sender_id=lambda: "7")
    ev_b = NS(unified_msg_origin=umo_b, get_group_id=lambda: "1001", get_sender_id=lambda: "7")
    ev_legacy_grp = NS(get_group_id=lambda: "1001", get_sender_id=lambda: "7")
    ev_legacy_dm = NS(get_group_id=lambda: "", get_sender_id=lambda: "42")
    assert cfg.get_event_origin_target(ev_a) == f"umo:{umo_a}"
    assert cfg.get_event_origin_target(ev_b) == f"umo:{umo_b}"
    assert cfg.get_event_origin_target(ev_legacy_grp) == "group:1001"
    assert cfg.get_event_origin_target(ev_legacy_dm) == "user:42"

    # 5. MemeScopeService isolates newly captured umo-tagged memes across adapters
    scope_svc = MemeScopeService(NS(plugin_config=cfg))
    assert scope_svc._is_entry_allowed_for_event(meme_index["/tmp/umo_a.png"], ev_a) is True
    assert scope_svc._is_entry_allowed_for_event(meme_index["/tmp/umo_a.png"], ev_b) is False
    assert scope_svc._is_entry_allowed_for_event(meme_index["/tmp/legacy_grp.png"], ev_a) is True


def test_build_release_includes_root_logo_png(tmp_path):
    """Verify tools/build_release.py includes root logo.png in install and source ZIPs."""
    import importlib.util
    import zipfile
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "tools" / "build_release.py"
    spec = importlib.util.spec_from_file_location("vm_build_release", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    out_dir = tmp_path / "release_out"
    mod.build(repo_root, out_dir)
    zips = sorted(out_dir.glob("*.zip"))
    assert len(zips) == 2
    for zpath in zips:
        with zipfile.ZipFile(zpath) as zf:
            assert "astrbot_plugin_visual_media/logo.png" in zf.namelist()

