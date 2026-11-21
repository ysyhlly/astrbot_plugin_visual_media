"""Build private install/source archives from an explicit allowlist (no publishing)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path


def release_metadata(root):
    """Identify clean checkouts and preserve the commit recorded by git archive."""
    raw = (root / "metadata.yaml").read_bytes()
    marker = b"$Format:%H$"
    if marker not in raw or not (root / ".git").exists():
        return raw
    try:
        if subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain"], stderr=subprocess.DEVNULL, timeout=5
        ).strip():
            return raw
        revision = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], stderr=subprocess.DEVNULL, timeout=5
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return raw
    if len(revision) not in (40, 64) or any(c not in b"0123456789abcdef" for c in revision):
        return raw
    return raw.replace(marker, revision)


ROOT_FILES = {
    "__init__.py",
    "main.py",
    "reload_bootstrap.py",
    "memes_engine.py",
    "generation_engine.py",
    "media_api.py",
    "media_library.py",
    "media_behavior.py",
    "media_runtime.py",
    "media_tasks.py",
    "media_receipts.py",
    "media_settings.py",
    "character_check.py",
    "runtime.py",
    "world_reference_access.py",
    "series_panels.py",
    "series_workspace.py",
    "plugin_api.py",
    "task_scheduler.py",
    "prompts.json",
    "_conf_schema.json",
    "metadata.yaml",
    "logo.png",
    "requirements.txt",
    "requirements-faiss.txt",
    "requirements-tested.txt",
    "constraints-tested.txt",
    "README.md",
    "CHANGELOG.md",
    "RELEASE_NOTES.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "handler_manifest.json",
    "upstream-manifest.json",
    ".gitignore",
}
RUNTIME_DIRS = {
    "api",
    "core",
    "tl",
    "views",
    "templates",
    "licenses",
    "docs",
    "tools",
    ".astrbot-plugin",
}
INSTALL_EXCLUDE_PREFIXES = (("docs", "upstream-generation"), ("docs", "upstream-memes"))
SOURCE_DIRS = RUNTIME_DIRS | {"frontend", "tests", "integration_tests"}
SKIP = {
    "__pycache__",
    "node_modules",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    "dist",
    "release",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".pyc",
    ".pyo",
    ".zip",
    ".pem",
    ".key",
}


def build(root: Path, output: Path):
    root = root.resolve()
    output = output.resolve()
    version = re.search(
        r"^version:\s*(\S+)", (root / "metadata.yaml").read_text(encoding="utf-8"), re.M
    ).group(1)
    if not re.fullmatch(r"[0-9A-Za-z.+-]+", version):
        raise ValueError("Invalid release version")
    output.mkdir(parents=True, exist_ok=True)
    metadata_raw = release_metadata(root)
    files = []
    for file in sorted(root.rglob("*")):
        rel = file.relative_to(root)
        if SKIP.intersection(rel.parts) or file.is_relative_to(output):
            continue
        if file.is_symlink():
            raise ValueError("Symlinks are not allowed in release inputs: " + str(rel))
        if not file.is_file():
            continue
        if rel.parts[0] == ".astrbot-plugin" and (
            len(rel.parts) != 3 or rel.parts[1] != "i18n" or file.suffix != ".json"
        ):
            continue
        if (len(rel.parts) == 1 and rel.name not in ROOT_FILES) or (
            len(rel.parts) > 1 and rel.parts[0] not in SOURCE_DIRS
        ):
            continue
        if (
            file.suffix in FORBIDDEN_SUFFIXES
            or file.name.startswith(".env")
            or file.name.endswith("_config.json")
        ):
            raise ValueError("Runtime/private data found in source tree: " + str(rel))
        raw = metadata_raw if rel.as_posix() == "metadata.yaml" else file.read_bytes()
        if file.suffix in {
            ".py",
            ".json",
            ".js",
            ".ts",
            ".yaml",
            ".yml",
            ".md",
            ".txt",
        }:
            text = raw.decode("utf-8-sig", errors="replace")
            if re.search(
                r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|sk-[A-Za-z0-9]{24,}",
                text,
            ):
                raise ValueError("Possible embedded credential in " + str(rel))
        if file.suffix == ".py":
            compile(raw, str(rel), "exec")
        files.append((rel, file, raw))
    required = {
        "metadata.yaml",
        "main.py",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "views/workshop/index.html",
        "views/workshop/studio/index.html",
        "media_settings.py",
    }
    assert required <= {rel.as_posix() for rel, _, _ in files}, (
        "Incomplete release inputs"
    )
    reports = []
    for kind in ("install", "source"):
        selected = [
            (rel, raw)
            for rel, _, raw in files
            if kind == "source"
            or (
                (len(rel.parts) == 1 or rel.parts[0] in RUNTIME_DIRS)
                and not any(rel.parts[: len(p)] == p for p in INSTALL_EXCLUDE_PREFIXES)
            )
        ]
        archive = output / f"astrbot_plugin_visual_media-{version}-{kind}.zip"
        manifest = {
            rel.as_posix(): hashlib.sha256(raw).hexdigest() for rel, raw in selected
        }
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, raw in selected:
                info = zipfile.ZipInfo(
                    "astrbot_plugin_visual_media/" + rel.as_posix(),
                    (2020, 1, 1, 0, 0, 0),
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                z.writestr(info, raw)
            info = zipfile.ZipInfo(
                "astrbot_plugin_visual_media/release-manifest.json",
                (2020, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, json.dumps(manifest, indent=2, sort_keys=True))
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            for name, sha in manifest.items():
                assert (
                    hashlib.sha256(
                        z.read("astrbot_plugin_visual_media/" + name)
                    ).hexdigest()
                    == sha
                )
        reports.append(
            {
                "file": archive.name,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "bytes": archive.stat().st_size,
                "files": len(manifest),
            }
        )
    report = {
        "version": version,
        "visibility": "release-candidate-build",
        "published": False,
        "artifacts": reports,
    }
    (output / "release-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.root, args.output)
