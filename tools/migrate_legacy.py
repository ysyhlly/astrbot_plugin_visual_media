"""Offline copy-only migration. Run with both old and new plugins disabled."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
import os
from contextlib import closing
from pathlib import Path

OLD_MEMES = "astrbot_plugin_stealer"
OLD_GENERATION = "astrbot_plugin_gemini_image_generation"
NEW = "astrbot_plugin_visual_media"


def migrate(
    data_root: Path, config_root: Path, destination: Path, config_destination: Path
):
    """Copy legacy libraries and rewrite paths without modifying the originals.

    Args:
        data_root: Directory containing both legacy plugin data directories.
        config_root: Directory containing legacy plugin config JSON files.
        destination: New plugin data directory; must not exist.
        config_destination: New plugin config file; must not exist.

    Returns:
        Counts of copied files and rewritten database cells.
    """
    data_root, config_root = data_root.resolve(), config_root.resolve()
    destination, config_destination = (
        destination.resolve(),
        config_destination.resolve(),
    )
    if destination.exists() or config_destination.exists():
        raise ValueError(
            "Destination already exists; migration will not overwrite data"
        )
    sources = [(data_root / OLD_MEMES, "memes"), (data_root / OLD_GENERATION, "")]
    if any(src.is_symlink() for src, _ in sources):
        raise ValueError("Symlink sources require explicit resolution before migration")
    if not all(src.is_dir() for src, _ in sources):
        raise ValueError("Both legacy data directories are required")
    if any(
        destination.is_relative_to(src) or config_destination.is_relative_to(src)
        for src, _ in sources
    ):
        raise ValueError("Destination must be outside the legacy directories")
    mapping = [(str(src), str(destination / subdir)) for src, subdir in sources]

    def translated(value):
        if isinstance(value, dict):
            return {translated(key): translated(item) for key, item in value.items()}
        if isinstance(value, list):
            return [translated(item) for item in value]
        if isinstance(value, str):
            for old, new in mapping:
                value = value.replace(old, new).replace(
                    old.replace("\\", "/"), new.replace("\\", "/")
                )
        return value

    merged = {}
    for key, legacy in [("memes", OLD_MEMES), ("generation", OLD_GENERATION)]:
        file = config_root / f"{legacy}_config.json"
        merged[key] = (
            json.loads(file.read_text(encoding="utf-8-sig")) if file.is_file() else {}
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    config_destination.parent.mkdir(parents=True, exist_ok=True)
    counts = {"files": 0, "database_cells": 0}
    with tempfile.TemporaryDirectory(
        prefix=".visual-migration-", dir=destination.parent
    ) as temporary:
        stage = Path(temporary) / "data"
        stage.mkdir()
        for source, subdir in sources:
            for file in source.rglob("*"):
                if file.is_symlink():
                    raise ValueError(
                        "Symlink sources require explicit resolution before migration"
                    )
                if not file.is_file() or file.name.endswith(
                    ("-wal", "-shm", "-journal")
                ):
                    continue
                target = stage / subdir / file.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                with file.open("rb") as stream:
                    sqlite_file = stream.read(16) == b"SQLite format 3\x00"
                if sqlite_file:
                    with (
                        closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)) as src_db,
                        closing(sqlite3.connect(target)) as out_db,
                    ):
                        src_db.backup(out_db)
                        tables = [
                            row[0]
                            for row in out_db.execute(
                                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                            )
                        ]
                        for table in tables:
                            quoted = '"' + table.replace('"', '""') + '"'
                            columns = out_db.execute(
                                f"PRAGMA table_info({quoted})"
                            ).fetchall()
                            for column in columns:
                                name = '"' + column[1].replace('"', '""') + '"'
                                for old, new in mapping:
                                    cursor = out_db.execute(
                                        f"UPDATE {quoted} SET {name}=replace({name}, ?, ?) WHERE typeof({name})='text' AND instr({name}, ?) > 0",
                                        (old, new, old),
                                    )
                                    counts["database_cells"] += cursor.rowcount
                        if (
                            out_db.execute("PRAGMA integrity_check").fetchone()[0]
                            != "ok"
                        ):
                            raise ValueError("Migrated database failed integrity check")
                        out_db.commit()
                elif file.suffix.lower() == ".json":
                    try:
                        content = translated(
                            json.loads(file.read_text(encoding="utf-8-sig"))
                        )
                    except (ValueError, UnicodeError):
                        shutil.copy2(file, target)
                    else:
                        target.write_text(
                            json.dumps(content, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                else:
                    shutil.copy2(file, target)
                counts["files"] += 1
        # No plugin data is made active until all copies and DB checks have passed.
        config_stage = Path(temporary) / "config.json"
        config_stage.write_text(
            json.dumps(translated(merged), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        config_stage.chmod(0o600)
        # Publish the config with an exclusive hard link on its own filesystem.
        # A failed write/link leaves neither a partial config nor active data.
        config_destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=config_destination.parent, prefix='.visual-migrate-', delete=False) as stream:
            staged_config = Path(stream.name)
            try:
                stream.write(config_stage.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.close()
                staged_config.unlink(missing_ok=True)
                raise
        staged_config.chmod(0o600)
        try:
            stage.rename(destination)
            try:
                os.link(staged_config, config_destination)
            except BaseException:
                destination.rename(Path(temporary) / "uncommitted-data")
                raise
        finally:
            staged_config.unlink(missing_ok=True)
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--config-destination", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            migrate(
                args.data_root,
                args.config_root,
                args.destination,
                args.config_destination,
            )
        )
    )
