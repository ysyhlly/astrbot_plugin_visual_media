"""Unit tests for v2 series_panels presence/cache/catalog and Workshop UI contracts."""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

if getattr(sys.modules.get("astrbot"), "__spec__", None) is None:
    api_mod = sys.modules.get("astrbot.api")
    if api_mod is not None and not hasattr(api_mod, "__path__"):
        api_mod.__path__ = []
    if "astrbot.api.web" not in sys.modules:
        web_mod = types.ModuleType("astrbot.api.web")
        web_mod.request = SimpleNamespace(_request=None)
        web_mod.json_response = lambda data, status_code=200: (status_code, data)
        sys.modules["astrbot.api.web"] = web_mod

import series_panels  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKSHOP_DIR = ROOT / "views" / "workshop"


def test_series_panels_catalog_standardized_labels():
    labels = {item[0]: item[1] for item in series_panels.CATALOG}
    assert labels["astrbot_plugin_chat_dynamics"] == "对话主控"
    assert labels["astrbot_plugin_group_chronicle"] == "群文化沿革"
    assert labels["astrbot_plugin_visual_media"] == "视觉工坊"


def test_series_panels_presence_attaches_badge_pulse_and_caches_10s(tmp_path, monkeypatch):
    series_panels.invalidate_summary_cache()
    calls = {"vm": 0, "bad": 0}
    clock = {"now": 1000.0}
    monkeypatch.setattr(series_panels.time, "monotonic", lambda: clock["now"])

    class GoodOwner:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_summary(self):
            calls["vm"] += 1
            return {
                "badge": {"count": 2, "tone": "info", "label": "2 项生成中"},
                "pulse": {
                    "headline": "2 项图像任务执行中",
                    "updated_at": "2026-11-21T02:00:00+00:00",
                },
            }

    class BrokenOwner:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_summary(self):
            calls["bad"] += 1
            raise RuntimeError("boom")

    class LegacyOwner:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

    class AsyncBadOwner:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        async def get_series_summary(self):
            return {"badge": {"count": 9, "tone": "warm", "label": "async"}}

    good = GoodOwner()
    broken = BrokenOwner()
    legacy = LegacyOwner()
    async_bad = AsyncBadOwner()

    ctx = SimpleNamespace(
        get_all_stars=lambda: {
            "vm": SimpleNamespace(
                name="astrbot_plugin_visual_media", activated=True, star_cls=good
            ),
            "cd": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics", activated=True, star_cls=broken
            ),
            "dl": SimpleNamespace(
                name="astrbot_plugin_dynamics_learning", activated=True, star_cls=legacy
            ),
            "dg": SimpleNamespace(
                name="astrbot_plugin_dynamics_games", activated=True, star_cls=async_bad
            ),
        }
    )

    first = series_panels.presence(ctx, "astrbot_plugin_visual_media", root=tmp_path)
    by_id = {p["id"]: p for p in first["plugins"]}

    assert by_id["astrbot_plugin_visual_media"]["badge"] == {
        "count": 2,
        "tone": "info",
        "label": "2 项生成中",
    }
    assert by_id["astrbot_plugin_visual_media"]["pulse"] == {
        "headline": "2 项图像任务执行中",
        "updated_at": "2026-11-21T02:00:00+00:00",
    }
    assert by_id["astrbot_plugin_chat_dynamics"]["badge"] is None
    assert by_id["astrbot_plugin_chat_dynamics"]["pulse"] is None
    assert by_id["astrbot_plugin_dynamics_learning"]["badge"] is None
    assert by_id["astrbot_plugin_dynamics_learning"]["pulse"] is None
    assert by_id["astrbot_plugin_dynamics_games"]["badge"] is None
    assert by_id["astrbot_plugin_dynamics_games"]["pulse"] is None
    assert calls["vm"] == 1

    # Within 10s TTL -> served from cache without calling get_series_summary again
    clock["now"] = 1005.0
    second = series_panels.presence(ctx, "astrbot_plugin_visual_media", root=tmp_path)
    assert calls["vm"] == 1
    assert {p["id"]: p for p in second["plugins"]}["astrbot_plugin_visual_media"][
        "badge"
    ]["count"] == 2

    # After 10s TTL -> refreshes
    clock["now"] = 1011.0
    series_panels.presence(ctx, "astrbot_plugin_visual_media", root=tmp_path)
    assert calls["vm"] == 2

    series_panels.invalidate_summary_cache()


def test_series_panels_presence_standalone_degradation(tmp_path):
    series_panels.invalidate_summary_cache()
    result = series_panels.presence(
        SimpleNamespace(get_all_stars=lambda: {}),
        "astrbot_plugin_visual_media",
        root=tmp_path,
    )
    assert result["schema_version"] == 1
    assert result["current"] == "astrbot_plugin_visual_media"
    assert all(p["badge"] is None and p["pulse"] is None for p in result["plugins"])


def test_workshop_html_standardized_i18n_and_v2_shell():
    loader_src = (WORKSHOP_DIR / "series-messages.js").read_text(encoding="utf-8")
    assert "loadLocaleMessages" in loader_src
    locale_packs = {}
    for lang in ("en", "ja", "ru"):
        pack_src = (WORKSHOP_DIR / f"series-messages-{lang}.js").read_text(
            encoding="utf-8"
        )
        locale_packs[lang] = json.loads(
            pack_src[pack_src.index("{") : pack_src.rindex("}") + 1]
        )
    html_files = [
        WORKSHOP_DIR / "index.html",
        WORKSHOP_DIR / "tasks" / "index.html",
        WORKSHOP_DIR / "behavior" / "index.html",
        WORKSHOP_DIR / "memes" / "index.html",
        WORKSHOP_DIR / "studio" / "index.html",
    ]
    for path in html_files:
        content = path.read_text(encoding="utf-8")
        assert "data-series-i18n=" not in content, f"legacy attribute in {path.name}"
        assert "data-series-aria=" not in content, f"legacy aria attribute in {path.name}"
        assert 'id="dynamics-series-panel"' in content
        for match in re.finditer(
            r'data-series-(?:text|aria-label|placeholder|title)="([^"]+)"', content
        ):
            key = match.group(1)
            for lang, pack in locale_packs.items():
                assert key in pack, (
                    f"missing {lang} translation key {key!r} in {path.name}"
                )

    # Verify polaroid cards & phase progress bar hooks exist in workshop assets
    workshop_js = (WORKSHOP_DIR / "workshop.js").read_text(encoding="utf-8")
    assert "vm-polaroid" in workshop_js
    assert "vm-polaroid-placeholder" in workshop_js
    tasks_js = (WORKSHOP_DIR / "shared" / "tasks.js").read_text(encoding="utf-8")
    assert "vm-phase-progress" in tasks_js
    assert 'role\',\'progressbar\'' in tasks_js or '"progressbar"' in tasks_js
    shell_js = (WORKSHOP_DIR / "shared" / "shell.js").read_text(encoding="utf-8")
    assert "dynamics-cmdk-deeplink" in shell_js
