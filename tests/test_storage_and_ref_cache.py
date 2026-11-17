"""Tests for WAL/busy_timeout SQLite configuration and O(1) reference_path cache."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("cv2") is None and "cv2" not in sys.modules:
    sys.modules["cv2"] = types.ModuleType("cv2")

if getattr(sys.modules.get("astrbot"), "__spec__", None) is None and "astrbot.api.message_components" not in sys.modules:
    class _StubModule(types.ModuleType):
        def __getattr__(self, name: str):
            val = type(name, (), {})
            setattr(self, name, val)
            return val

    for _mod_name in (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.event.filter",
        "astrbot.api.message_components",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.platform",
        "astrbot.core.platform.message_session",
    ):
        if _mod_name not in sys.modules:
            _m = _StubModule(_mod_name)
            _m.__path__ = []
            sys.modules[_mod_name] = _m

    sys.modules["astrbot.api"].logger = SimpleNamespace(
        warning=lambda *a, **k: None,
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    sys.modules["astrbot.api.event"].MessageChain = list
    sys.modules["astrbot.api.message_components"].Image = SimpleNamespace(
        fromFileSystem=lambda p: p
    )
    sys.modules["astrbot.core.platform.message_session"].MessageSession = SimpleNamespace(
        from_str=lambda s: s
    )

# Keep absolute plugin imports separate from legacy top-level tl imports.
# Aliasing their parent module overwrites tl.core during collection.
_qualified_tl = "astrbot_plugin_visual_media.tl"
if _qualified_tl not in sys.modules:
    _package = types.ModuleType(_qualified_tl)
    _package.__path__ = [str(Path(__file__).resolve().parents[1] / "tl")]
    sys.modules[_qualified_tl] = _package

from astrbot_plugin_visual_media.media_behavior import MediaBehavior
from astrbot_plugin_visual_media.media_library import MediaLibrary
from astrbot_plugin_visual_media.media_tasks import MediaTasks


def test_sqlite_stores_enable_wal_and_busy_timeout(tmp_path: Path):
    owner = SimpleNamespace(
        generation=SimpleNamespace(_plugin_data_dir=str(tmp_path)),
        memes=SimpleNamespace(
            base_dir=tmp_path, plugin_api=SimpleNamespace(_get_index=lambda: {})
        ),
    )
    behavior = MediaBehavior(tmp_path)
    library = MediaLibrary(owner)
    tasks = MediaTasks(owner)

    with behavior.connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000

    with library._connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000

    with tasks._connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000


def test_reference_path_o1_cache_respects_inplace_scope_changes(tmp_path: Path):
    img_a = tmp_path / "a.png"
    img_b = tmp_path / "b.png"
    img_a.write_bytes(b"png-a")
    img_b.write_bytes(b"png-b")

    index = {
        str(img_a): {"hash": "hash_a", "scope_mode": "public"},
        str(img_b): {"hash": "hash_b", "scope_mode": "local"},
    }
    owner = SimpleNamespace(
        generation=SimpleNamespace(_plugin_data_dir=str(tmp_path)),
        memes=SimpleNamespace(
            base_dir=tmp_path,
            plugin_api=SimpleNamespace(_get_index=lambda: index),
        ),
    )
    library = MediaLibrary(owner)

    # Initial public hit populates cache
    assert library.reference_path("hash_a") == img_a.resolve()
    assert library._public_ref_cache.get("hash_a") == str(img_a)

    # Local image rejected
    with pytest.raises(ValueError, match="公开表情"):
        library.reference_path("hash_b")

    # In-place revocation of public -> local without changing dict length is immediately enforced
    index[str(img_a)]["scope_mode"] = "local"
    with pytest.raises(ValueError, match="公开表情"):
        library.reference_path("hash_a")

    # In-place promotion of local -> public without changing dict length is immediately discovered
    index[str(img_b)]["scope_mode"] = "public"
    assert library.reference_path("hash_b") == img_b.resolve()
