import hashlib
import json
import sqlite3

import pytest
from astrbot_plugin_visual_media.tools.migrate_legacy import (
    migrate,
    OLD_MEMES,
    OLD_GENERATION,
)


def fixture(root):
    data = root / "data"
    config = root / "config"
    config.mkdir()
    for name in [OLD_MEMES, OLD_GENERATION]:
        (data / name).mkdir(parents=True)
        (config / (name + "_config.json")).write_text(
            json.dumps({"fixture": name}), encoding="utf-8"
        )
    memes = data / OLD_MEMES
    (memes / "categories").mkdir()
    (memes / "categories/image.png").write_bytes(b"fixture")
    with sqlite3.connect(memes / "emoji.db") as db:
        db.execute("CREATE TABLE emoji(path TEXT PRIMARY KEY, hash TEXT)")
        db.execute(
            "INSERT INTO emoji VALUES (?, ?)",
            (str(memes / "categories/image.png"), "digest"),
        )
    (data / OLD_GENERATION / "history.json").write_text(
        json.dumps({"path": str(data / OLD_GENERATION / "gallery/image.png")})
    )
    return data, config


def snapshot(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_migration_copies_both_libraries_and_preserves_originals(tmp_path):
    data, config = fixture(tmp_path)
    before = snapshot(data)
    target, config_target = tmp_path / "new", tmp_path / "new_config.json"
    result = migrate(data, config, target, config_target)
    assert result["files"] == 3
    assert snapshot(data) == before
    assert json.loads(config_target.read_text()) == {
        "memes": {"fixture": OLD_MEMES},
        "generation": {"fixture": OLD_GENERATION},
    }
    with sqlite3.connect(target / "memes/emoji.db") as db:
        assert db.execute("SELECT path FROM emoji").fetchone()[0] == str(
            target / "memes/categories/image.png"
        )
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert json.loads((target / "history.json").read_text())["path"] == str(
        target / "gallery/image.png"
    )
    with pytest.raises(ValueError, match="exists"):
        migrate(data, config, target, config_target)


def test_migration_refuses_nested_or_incomplete_sources(tmp_path):
    data, config = fixture(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        migrate(data, config, data / OLD_MEMES / "nested", tmp_path / "new.json")
    with pytest.raises(ValueError, match="required"):
        migrate(tmp_path / "missing", config, tmp_path / "new", tmp_path / "new.json")


def test_migration_uses_sqlite_backup_for_wal(tmp_path):
    data, config = fixture(tmp_path)
    db = sqlite3.connect(data / OLD_MEMES / "emoji.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "INSERT INTO emoji VALUES (?, ?)", (str(data / OLD_MEMES / "new.png"), "wal")
    )
    db.commit()
    try:
        migrate(data, config, tmp_path / "new", tmp_path / "new.json")
        with sqlite3.connect(tmp_path / "new/memes/emoji.db") as migrated:
            assert migrated.execute("SELECT count(*) FROM emoji").fetchone()[0] == 2
    finally:
        db.close()


def test_failed_config_publish_preserves_sources_and_allows_retry(
    tmp_path, monkeypatch
):
    import os

    data, config = fixture(tmp_path)
    before = snapshot(data)
    target = tmp_path / "new"
    config_target = tmp_path / "new_config.json"
    real_link = os.link

    def broken(*args, **kwargs):
        raise OSError("simulated config publish failure")

    monkeypatch.setattr(os, "link", broken)
    with pytest.raises(OSError, match="publish"):
        migrate(data, config, target, config_target)
    assert (
        snapshot(data) == before and not target.exists() and not config_target.exists()
    )
    assert not list(tmp_path.glob(".visual-migrate-*"))
    monkeypatch.setattr(os, "link", real_link)
    migrate(data, config, target, config_target)
    assert target.exists() and config_target.exists()


def test_migration_does_not_overwrite_racing_config(tmp_path, monkeypatch):
    import os

    data, config = fixture(tmp_path)
    target = tmp_path / "new"
    config_target = tmp_path / "new_config.json"
    real_link = os.link

    def racing(src, dst):
        config_target.write_text("concurrent owner")
        return real_link(src, dst)

    monkeypatch.setattr(os, "link", racing)
    with pytest.raises(FileExistsError):
        migrate(data, config, target, config_target)
    assert config_target.read_text() == "concurrent owner"
    assert not target.exists()
