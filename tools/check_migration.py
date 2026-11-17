"""Standalone migration smoke test; does not need AstrBot or touch live data."""

import json
import sqlite3
import tempfile
from pathlib import Path
from contextlib import closing
from migrate_legacy import migrate, OLD_MEMES, OLD_GENERATION

with tempfile.TemporaryDirectory(prefix="visual-media-migration-smoke-") as tmp:
    root = Path(tmp)
    data = root / "data"
    config = root / "config"
    config.mkdir()
    for name in (OLD_MEMES, OLD_GENERATION):
        (data / name).mkdir(parents=True)
        (config / f"{name}_config.json").write_text("{}", encoding="utf-8")
    source = data / OLD_MEMES / "test.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE images(path TEXT)")
        db.execute(
            "INSERT INTO images VALUES (?)", (str(data / OLD_MEMES / "sample.png"),)
        )
        db.commit()
    result = migrate(data, config, root / "new", root / "new.json")
    with closing(sqlite3.connect(root / "new/memes/test.sqlite3")) as db:
        assert db.execute("SELECT path FROM images").fetchone()[0] == str(
            root / "new/memes/sample.png"
        )
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert source.exists() and json.loads((root / "new.json").read_text()) == {
        "memes": {},
        "generation": {},
    }
    print(
        json.dumps(
            {
                "status": "passed",
                "scope": "temporary files only",
                "copied_files": result["files"],
            }
        )
    )
