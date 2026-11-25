"""Batch 4 regression tests: dependencies, text_similarity fallback, release packaging hygiene, hot reload & CI gates."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse_req_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.split("#", 1)[0].strip()
        if not raw or raw.startswith("-"):
            continue
        pkg = re.split(r"[<>=!~;\s\[]", raw, maxsplit=1)[0].strip()
        if pkg:
            names.add(pkg.lower().replace("_", "-"))
    return names


def _load_build_release():
    spec = importlib.util.spec_from_file_location(
        "visual_media_build_release_b4", REPO_ROOT / "tools" / "build_release.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_requirements_covers_rank_bm25_and_all_top_level_third_party_imports():
    req_names = _parse_req_names(REPO_ROOT / "requirements.txt")
    constraints_names = _parse_req_names(REPO_ROOT / "constraints-tested.txt")

    assert "rank-bm25" in req_names
    assert "rank-bm25" in constraints_names

    import_to_dist = {
        "pil": "pillow",
        "yaml": "pyyaml",
        "cv2": "opencv-python-headless",
        "skimage": "scikit-image",
        "rank_bm25": "rank-bm25",
        "aiohttp_socks": "aiohttp-socks",
    }
    host_or_optional = {
        "astrbot",
        "aiohttp",
        "pydantic",
        "starlette",
        "sqlmodel",
        "mcp",
        "aiocqhttp",
        "aiofiles",
        "faiss",
        "pillow_heif",
    }
    stdlib = set(sys.stdlib_module_names)
    local_top = {p.stem for p in REPO_ROOT.glob("*.py")} | {
        p.name for p in REPO_ROOT.iterdir() if p.is_dir()
    }
    skip_dirs = {
        ".git",
        "__pycache__",
        "tests",
        "integration_tests",
        "tools",
        "frontend",
    }

    missing: dict[str, list[str]] = {}
    for py_file in sorted(REPO_ROOT.rglob("*.py")):
        rel = py_file.relative_to(REPO_ROOT)
        if skip_dirs.intersection(rel.parts):
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(rel))
        for node in tree.body:
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                if mod in stdlib or mod in local_top or mod in host_or_optional:
                    continue
                dist = import_to_dist.get(mod.lower(), mod.lower().replace("_", "-"))
                if dist not in req_names:
                    missing.setdefault(mod, []).append(rel.as_posix())

    assert not missing, f"Undeclared top-level third-party imports: {missing}"


def test_text_similarity_graceful_fallback_without_rank_bm25_or_rapidfuzz(monkeypatch):
    monkeypatch.setitem(sys.modules, "rank_bm25", None)
    monkeypatch.setitem(sys.modules, "rapidfuzz", None)
    monkeypatch.setitem(sys.modules, "rapidfuzz.distance", None)
    monkeypatch.setitem(sys.modules, "rapidfuzz.distance.Levenshtein", None)

    spec = importlib.util.spec_from_file_location(
        "visual_media_text_similarity_fallback_test",
        REPO_ROOT / "core" / "search" / "text_similarity.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    corpus = [
        mod.tokenize_for_bm25("早安 开心 打招呼"),
        mod.tokenize_for_bm25("加班 好累 崩溃"),
        mod.tokenize_for_bm25("晚安 睡觉 休息"),
    ]
    bm25 = mod.BM25(corpus)
    top = bm25.get_top_k(mod.tokenize_for_bm25("早安开心"), k=2)
    assert top and top[0][0] == 0 and top[0][1] > 0

    sim_same = mod.calculate_levenshtein_similarity("早安", "早安")
    sim_diff = mod.calculate_levenshtein_similarity("早安", "晚安睡觉")
    assert sim_same == 1.0
    assert 0.0 <= sim_diff < sim_same

    hybrid = mod.calculate_hybrid_similarity("早安开心", "早安开心")
    assert hybrid > 0.8


def test_build_release_single_top_dir_and_dev_file_blacklist_synthetic(tmp_path):
    build_release = _load_build_release()
    root = tmp_path / "pkg"
    root.mkdir()
    sample_files = {
        "metadata.yaml": "name: astrbot_plugin_visual_media\nversion: 0.5.0-rc.9\n",
        "main.py": "pass\n",
        "media_settings.py": "pass\n",
        "requirements.txt": "rank-bm25>=0.2.2\n",
        "_conf_schema.json": "{}",
        "LICENSE": "AGPL",
        "THIRD_PARTY_NOTICES.md": "notices",
        "views/workshop/index.html": "<html/>",
        "views/workshop/studio/index.html": "<html/>",
        # Forbidden dev files that MUST NOT enter install.zip
        ".gitignore": "__pycache__/\n",
        ".gitattributes": "* text=auto\n",
        "package-lock.json": '{"lockfileVersion": 3}',
        ".github/workflows/ci.yml": "name: CI\n",
        "tools/build_release.py": "print('dev')\n",
        "tests/test_dummy.py": "assert True\n",
        "frontend/package-lock.json": '{"lockfileVersion": 3}',
    }
    for rel, content in sample_files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    out = tmp_path / "out"
    build_release.build(root, out)

    install_zip = out / "astrbot_plugin_visual_media-0.5.0-rc.9-install.zip"
    source_zip = out / "astrbot_plugin_visual_media-0.5.0-rc.9-source.zip"
    with zipfile.ZipFile(install_zip) as zi:
        inames = zi.namelist()
    with zipfile.ZipFile(source_zip) as zs:
        snames = set(zs.namelist())

    prefix = "astrbot_plugin_visual_media/"
    assert inames and all(name.startswith(prefix) for name in inames)
    inner = {name[len(prefix) :] for name in inames}

    assert {"metadata.yaml", "main.py", "requirements.txt", "_conf_schema.json"} <= inner
    assert ".gitignore" not in inner
    assert ".gitattributes" not in inner
    assert "package-lock.json" not in inner
    assert "tools/build_release.py" not in inner
    assert not any(
        name.startswith((".github/", "tools/", "tests/", "frontend/"))
        for name in inner
    )
    # Source archive still includes rebuild sources
    assert "astrbot_plugin_visual_media/tools/build_release.py" in snames


def test_build_release_real_repo_install_zip_hygiene(tmp_path):
    build_release = _load_build_release()
    report = build_release.build(REPO_ROOT, tmp_path)
    version = report["version"]
    install_zip = tmp_path / f"astrbot_plugin_visual_media-{version}-install.zip"
    assert install_zip.is_file()

    with zipfile.ZipFile(install_zip) as z:
        names = z.namelist()
        manifest = json.loads(z.read("astrbot_plugin_visual_media/release-manifest.json"))

    prefix = "astrbot_plugin_visual_media/"
    assert names and all(name.startswith(prefix) for name in names)
    top_dirs = {name.split("/", 1)[0] for name in names}
    assert top_dirs == {"astrbot_plugin_visual_media"}

    inner = {name[len(prefix) :] for name in names}
    for required in (
        "metadata.yaml",
        "main.py",
        "requirements.txt",
        "_conf_schema.json",
        "reload_bootstrap.py",
        "series_panels.py",
        "series_workspace.py",
        "views/workshop/index.html",
        "views/workshop/studio/index.html",
    ):
        assert required in inner, f"Missing {required} from real install.zip"

    for forbidden in (".gitignore", ".gitattributes", "package-lock.json", "tools/build_release.py"):
        assert forbidden not in inner, f"Forbidden dev file in real install.zip: {forbidden}"
    assert not any(
        name.startswith((".github/", "tools/", "tests/", "integration_tests/", "frontend/"))
        for name in inner
    )
    assert "requirements.txt" in manifest


def test_hot_reload_reloads_series_panels_and_uses_dynamic_metadata_version(tmp_path):
    main_src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_src, filename="main.py")

    # 1) ensure_current does not hardcode version= and includes series_panels in checks
    ensure_calls = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "ensure_current"
    ]
    assert len(ensure_calls) == 1
    kw_map = {kw.arg: kw.value for kw in ensure_calls[0].value.keywords}
    assert "version" not in kw_map, "main.py should let reload_bootstrap read metadata.yaml"
    assert "series_panels" in ast.unparse(kw_map["checks"])

    # 2) VisualMediaPlugin.initialize calls reload(series_panels).register_series_panels(...)
    assert "reload(series_panels).register_series_panels(" in main_src

    # 3) reload_bootstrap.ensure_current dynamically reads metadata.yaml and purges stale series_panels
    spec = importlib.util.spec_from_file_location(
        "visual_media_reload_bootstrap_b4", REPO_ROOT / "reload_bootstrap.py"
    )
    assert spec and spec.loader
    rb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rb)

    pkg_dir = tmp_path / "fake_vm_pkg"
    pkg_dir.mkdir()
    (pkg_dir / "metadata.yaml").write_text(
        "name: astrbot_plugin_visual_media\nversion: 0.5.0-rc.9\n", encoding="utf-8"
    )
    (pkg_dir / "main.py").write_text("pass\n", encoding="utf-8")
    (pkg_dir / "series_panels.py").write_text("X = 1\n", encoding="utf-8")

    pkg_name = "_test_vm_reload_pkg"
    parent_mod = ModuleType(pkg_name)
    stale_sp = ModuleType(f"{pkg_name}.series_panels")
    parent_mod.series_panels = stale_sp
    sys.modules[pkg_name] = parent_mod
    sys.modules[f"{pkg_name}.series_panels"] = stale_sp
    try:
        rb.ensure_current(pkg_name, f"{pkg_name}.main", str(pkg_dir / "main.py"))
        assert parent_mod.__version__ == "0.5.0-rc.9"
        assert f"{pkg_name}.series_panels" in sys.modules

        # Mutate series_panels.py -> next ensure_current MUST purge stale series_panels
        (pkg_dir / "series_panels.py").write_text("X = 2\n", encoding="utf-8")
        rb.ensure_current(pkg_name, f"{pkg_name}.main", str(pkg_dir / "main.py"))
        assert f"{pkg_name}.series_panels" not in sys.modules
        assert not hasattr(parent_mod, "series_panels")
    finally:
        sys.modules.pop(f"{pkg_name}.series_panels", None)
        sys.modules.pop(pkg_name, None)


def test_metadata_version_and_ci_workflow_gates():
    meta = yaml.safe_load((REPO_ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert meta["name"] == "astrbot_plugin_visual_media"
    assert re.fullmatch(
        r"\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?", str(meta["version"])
    )

    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    ci_text = ci_path.read_text(encoding="utf-8")
    ci_yaml = yaml.safe_load(ci_text)
    assert "jobs" in ci_yaml and "lint-and-unit" in ci_yaml["jobs"]
    assert "rank-bm25" in ci_text
    assert "build_release" in ci_text
    assert ".gitignore" in ci_text
    assert "tools/build_release.py" in ci_text
    assert "package-lock.json" in ci_text
