"""Batch 1 security regression tests for visual_media.

Covers:
1. /serve-image directory & image extension whitelist, boundary-before-existence check,
   and uniform non-leaking error responses.
2. CSRF protection for DELETE / PUT / PATCH / POST in tl/web_api.py.
3. SSRF blocking (private/loopback/link-local/metadata/IPv4-mapped/numeric IP/DNS/redirects)
   and streaming response byte caps across tl/core/path_policy.py, tl/reference_image.py,
   core/events/image_download_service.py, and tl/api/openai_compat.py.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import socket
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from tl.core.errors import PathPolicyError
from tl.core.path_policy import check_remote_url, validate_remote_url_async


def _make_png_bytes(size: tuple[int, int] = (8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (40, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. /serve-image tests
# ---------------------------------------------------------------------------


def _load_library_routes(monkeypatch):
    """Import astrbot_plugin_visual_media.api.library with a lightweight quart stub."""
    repo_root = Path(__file__).resolve().parents[1]
    pkg_name = "astrbot_plugin_visual_media"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(repo_root)]
        monkeypatch.setitem(sys.modules, pkg_name, pkg)

    fake_quart = types.ModuleType("quart")
    fake_quart.request = SimpleNamespace(args={})
    fake_quart.jsonify = lambda payload: payload

    async def _fake_send_file(path: str):
        return {"served": path}, 200

    fake_quart.send_file = _fake_send_file
    monkeypatch.setitem(sys.modules, "quart", fake_quart)

    # Clear cached module so it binds cleanly to our stubbed quart
    mod_name = f"{pkg_name}.api.library"
    sys.modules.pop(mod_name, None)
    mod = importlib.import_module(mod_name)
    return mod, fake_quart


class _DummyLibraryAPI:
    ALLOWED_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir


def test_serve_image_allows_only_whitelisted_subdirs_and_image_exts(
    tmp_path: Path, monkeypatch
) -> None:
    mod, fake_quart = _load_library_routes(monkeypatch)

    class API(_DummyLibraryAPI, mod.LibraryRoutes):
        pass

    data_dir = tmp_path / "memes"
    (data_dir / "categories" / "happy").mkdir(parents=True)
    (data_dir / "pending").mkdir(parents=True)
    (data_dir / "thumb_cache").mkdir(parents=True)
    (data_dir / "raw").mkdir(parents=True)

    valid_cat = data_dir / "categories" / "happy" / "ok.png"
    valid_cat.write_bytes(_make_png_bytes())
    valid_pending = data_dir / "pending" / "p.jpg"
    valid_pending.write_bytes(b"\xff\xd8\xff\xe0test")
    valid_thumb = data_dir / "thumb_cache" / "t.webp"
    valid_thumb.write_bytes(b"RIFFxxxxWEBP")
    valid_raw = data_dir / "raw" / "r.gif"
    valid_raw.write_bytes(b"GIF89a")

    # Sensitive non-image files in root & even inside media subdirs
    db_file = data_dir / "emojis.db"
    db_file.write_bytes(b"SQLite format 3\x00")
    sqlite_file = data_dir / "categories" / "leak.sqlite3"
    sqlite_file.write_bytes(b"SQLite format 3\x00")
    json_file = data_dir / "categories.json"
    json_file.write_text('{"secret": true}', encoding="utf-8")
    cat_json = data_dir / "categories" / "meta.json"
    cat_json.write_text("{}", encoding="utf-8")
    root_png = data_dir / "root_level.png"
    root_png.write_bytes(_make_png_bytes())

    # Outside file (both existing and non-existing)
    outside_existing = tmp_path / "secret.png"
    outside_existing.write_bytes(_make_png_bytes())
    outside_missing = tmp_path / "does_not_exist.png"

    api = API(data_dir)

    async def _call(path_arg: str):
        monkeypatch.setattr(mod, "request", SimpleNamespace(args={"path": path_arg}))
        return await api.handle_serve_image()

    # Valid images in whitelisted subdirs succeed (absolute & relative)
    for ok_path in (
        str(valid_cat),
        "categories/happy/ok.png",
        str(valid_pending),
        str(valid_thumb),
        str(valid_raw),
    ):
        resp, status = asyncio.run(_call(ok_path))
        assert status == 200
        assert "served" in resp

    # Blocked targets MUST all return the exact same 404 & generic error (no existence oracle)
    blocked_cases = [
        "",
        str(db_file),
        "emojis.db",
        str(sqlite_file),
        str(json_file),
        str(cat_json),
        str(root_png),
        str(data_dir / "categories" / ".." / "emojis.db"),
        "categories/../../secret.png",
        str(outside_existing),
        str(outside_missing),
        str(data_dir / "categories" / "happy" / "missing.png"),
        str(data_dir / "categories" / "happy"),  # directory, not file
        f"{valid_cat}\x00.png",
    ]
    responses = [asyncio.run(_call(p)) for p in blocked_cases]
    expected_body = {"success": False, "error": mod.LibraryRoutes._SERVE_IMAGE_ERROR}
    for case, (body, status) in zip(blocked_cases, responses, strict=True):
        assert status == 404, f"case {case!r} returned {status}"
        assert body == expected_body, f"case {case!r} leaked details: {body}"
        assert str(tmp_path) not in str(body)


def test_serve_image_checks_boundary_before_filesystem_existence(
    tmp_path: Path, monkeypatch
) -> None:
    """Verify out-of-bounds paths never even trigger Path.is_file() existence probing."""
    mod, fake_quart = _load_library_routes(monkeypatch)

    class API(_DummyLibraryAPI, mod.LibraryRoutes):
        pass

    data_dir = tmp_path / "memes"
    (data_dir / "categories").mkdir(parents=True)
    outside = tmp_path / "host_secret.png"
    outside.write_bytes(_make_png_bytes())

    api = API(data_dir)
    probed: list[Path] = []
    real_is_file = Path.is_file

    def tracking_is_file(self: Path):
        probed.append(self)
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", tracking_is_file)

    monkeypatch.setattr(mod, "request", SimpleNamespace(args={"path": str(outside)}))
    body, status = asyncio.run(api.handle_serve_image())
    assert status == 404
    assert body["success"] is False
    assert outside.resolve() not in probed


# ---------------------------------------------------------------------------
# 2. CSRF DELETE / PUT / PATCH / POST tests in tl/web_api.py
# ---------------------------------------------------------------------------


def _load_web_api_module(monkeypatch):
    """Import tl.web_api without replacing an already-loaded module object."""
    import importlib.util

    if "cv2" not in sys.modules and importlib.util.find_spec("cv2") is None:
        monkeypatch.setitem(sys.modules, "cv2", types.ModuleType("cv2"))
    mod = importlib.import_module("tl.web_api")
    if getattr(mod, "json_response", None) is None:
        monkeypatch.setattr(
            mod,
            "json_response",
            lambda data, status_code=200, headers=None: SimpleNamespace(
                data=data, status_code=status_code, headers=headers or {}
            ),
        )
    if getattr(mod, "error_response", None) is None:
        monkeypatch.setattr(
            mod,
            "error_response",
            lambda message, status_code=400, data=None, headers=None: SimpleNamespace(
                data={"status": "error", "message": message, "data": data},
                status_code=status_code,
                headers=headers or {},
            ),
        )
    if getattr(mod, "file_response", None) is None:
        monkeypatch.setattr(
            mod, "file_response", lambda *a, **kw: SimpleNamespace(status_code=200)
        )
    if getattr(mod, "stream_response", None) is None:
        monkeypatch.setattr(
            mod, "stream_response", lambda *a, **kw: SimpleNamespace(status_code=200)
        )
    if getattr(mod, "WEB_API_AVAILABLE", False) is False:
        monkeypatch.setattr(mod, "WEB_API_AVAILABLE", True)
    return mod


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT", "PATCH"])
def test_web_api_csrf_blocks_cross_origin_for_all_state_changing_methods(
    monkeypatch, method: str
) -> None:
    module = _load_web_api_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "request",
        SimpleNamespace(
            username="admin",
            method=method,
            path="/astrbot_plugin_visual_media/webui/templates/hub/key",
            headers={
                "Host": "dashboard.local",
                "Origin": "https://evil.example",
            },
        ),
    )
    resp = module.WebStudioAPI._access_guard("/templates/hub/key")
    assert resp is not None
    assert resp.status_code == 403


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT", "PATCH"])
def test_web_api_csrf_blocks_cross_site_fetch_metadata_and_referer(
    monkeypatch, method: str
) -> None:
    module = _load_web_api_module(monkeypatch)
    # Cross-site Sec-Fetch-Site
    monkeypatch.setattr(
        module,
        "request",
        SimpleNamespace(
            username="admin",
            method=method,
            path="/astrbot_plugin_visual_media/webui/templates/hub/key",
            headers={
                "Host": "dashboard.local",
                "Sec-Fetch-Site": "cross-site",
            },
        ),
    )
    resp = module.WebStudioAPI._access_guard("/templates/hub/key")
    assert resp is not None
    assert resp.status_code == 403

    # Cross-site Referer when Origin absent
    monkeypatch.setattr(
        module,
        "request",
        SimpleNamespace(
            username="admin",
            method=method,
            path="/astrbot_plugin_visual_media/webui/templates/hub/key",
            headers={
                "Host": "dashboard.local",
                "Referer": "https://evil.example/attack",
            },
        ),
    )
    resp2 = module.WebStudioAPI._access_guard("/templates/hub/key")
    assert resp2 is not None
    assert resp2.status_code == 403


def test_web_api_allows_same_origin_delete_for_admin_and_blocks_api_key(
    monkeypatch,
) -> None:
    module = _load_web_api_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "request",
        SimpleNamespace(
            username="admin",
            method="DELETE",
            path="/astrbot_plugin_visual_media/webui/templates/hub/key",
            headers={
                "Host": "dashboard.local",
                "Origin": "https://dashboard.local",
                "Sec-Fetch-Site": "same-origin",
            },
        ),
    )
    assert module.WebStudioAPI._access_guard("/templates/hub/key") is None

    monkeypatch.setattr(
        module,
        "request",
        SimpleNamespace(
            username="api_key:k1",
            method="DELETE",
            path="/astrbot_plugin_visual_media/webui/templates/hub/key",
            headers={"Host": "dashboard.local", "Origin": "https://dashboard.local"},
        ),
    )
    denied = module.WebStudioAPI._access_guard("/templates/hub/key")
    assert denied is not None
    assert denied.status_code == 403


# ---------------------------------------------------------------------------
# 3. SSRF & streaming download limit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1/img.png",
        "http://localhost:8080/img.png",
        "http://sub.localhost/img.png",
        "http://10.0.0.1/img.png",
        "http://172.16.5.4/img.png",
        "http://192.168.1.10/img.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/img.png",
        "http://0.0.0.0/img.png",
        "http://[::1]/img.png",
        "http://[::ffff:127.0.0.1]/img.png",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "http://[fe80::1]/img.png",
        "http://[fc00::1]/img.png",
        "http://2130706433/img.png",
        "http://0x7f000001/img.png",
        "http://0177.0.0.1/img.png",
        "http://127.1/img.png",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://service.internal/img.png",
        "http://printer.local/img.png",
        "file:///etc/passwd",
        "ftp://93.184.216.34/img.png",
        "https://user:pass@example.com/img.png",
    ],
)
def test_ssrf_check_remote_url_blocks_private_and_metadata_targets(
    bad_url: str,
) -> None:
    with pytest.raises(PathPolicyError):
        check_remote_url(bad_url)


def test_ssrf_async_dns_validation_blocks_domain_resolving_to_private_ip(
    monkeypatch,
) -> None:
    async def _run():
        loop = asyncio.get_running_loop()

        async def fake_getaddrinfo(host, port, *args, **kwargs):
            if host == "rebind.example":
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        6,
                        "",
                        ("169.254.169.254", port),
                    )
                ]
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
            ]

        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

        with pytest.raises(PathPolicyError):
            await validate_remote_url_async("https://rebind.example/img.png")

        assert (
            await validate_remote_url_async("https://public.example/img.png")
            == "https://public.example/img.png"
        )

    asyncio.run(_run())


class _FakeStreamContent:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.chunks_yielded = 0

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            self.chunks_yielded += 1
            yield chunk


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        reason: str = "OK",
    ):
        self.status = status
        self.headers = headers or {}
        self.reason = reason
        self.content = _FakeStreamContent(chunks or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


def test_reference_image_blocks_ssrf_redirects_and_oversized_streams(
    tmp_path: Path, monkeypatch
) -> None:
    import tl.reference_image as ref_mod

    # 1. Direct SSRF target never calls session.get
    session1 = _FakeSession([])
    mime, b64 = asyncio.run(
        ref_mod.normalize_reference_image_input(
            "http://169.254.169.254/latest/meta-data/",
            image_cache_dir=tmp_path / "c1",
            session=session1,
        )
    )
    assert (mime, b64) == (None, None)
    assert session1.calls == []

    # 2. Redirect from public URL to internal IP is blocked before second request
    session2 = _FakeSession(
        [
            _FakeResponse(
                status=302,
                headers={"Location": "http://127.0.0.1:8080/secret.png"},
            )
        ]
    )
    mime, b64 = asyncio.run(
        ref_mod.normalize_reference_image_input(
            "https://93.184.216.34/start.png",
            image_cache_dir=tmp_path / "c2",
            session=session2,
        )
    )
    assert (mime, b64) == (None, None)
    assert len(session2.calls) == 1
    assert session2.calls[0][1].get("allow_redirects") is False

    # 3. Streaming response over max_bytes aborts early without consuming all chunks
    oversized_resp = _FakeResponse(
        status=200,
        headers={"Content-Type": "image/png"},
        chunks=[b"\x89PNG\r\n\x1a\n" + b"a" * 600, b"b" * 600, b"c" * 600],
    )
    session3 = _FakeSession([oversized_resp])
    mime, b64 = asyncio.run(
        ref_mod.normalize_reference_image_input(
            "https://93.184.216.34/huge.png",
            image_cache_dir=tmp_path / "c3",
            session=session3,
            max_bytes=1024,
        )
    )
    assert (mime, b64) == (None, None)
    assert oversized_resp.content.chunks_yielded == 2  # stopped before 3rd chunk!

    # 4. Content-Length over max_bytes is rejected before reading any chunk
    cl_resp = _FakeResponse(
        status=200,
        headers={"Content-Type": "image/png", "Content-Length": "99999999"},
        chunks=[b"should-never-read"],
    )
    session4 = _FakeSession([cl_resp])
    mime, b64 = asyncio.run(
        ref_mod.normalize_reference_image_input(
            "https://93.184.216.34/huge_cl.png",
            image_cache_dir=tmp_path / "c4",
            session=session4,
            max_bytes=1024,
        )
    )
    assert (mime, b64) == (None, None)
    assert cl_resp.content.chunks_yielded == 0

    # 5. Valid small PNG succeeds
    png_data = _make_png_bytes()
    ok_resp = _FakeResponse(
        status=200,
        headers={"Content-Type": "image/png", "Content-Length": str(len(png_data))},
        chunks=[png_data[:32], png_data[32:]],
    )
    session5 = _FakeSession([ok_resp])
    mime, b64 = asyncio.run(
        ref_mod.normalize_reference_image_input(
            "https://93.184.216.34/good.png",
            image_cache_dir=tmp_path / "c5",
            session=session5,
            max_bytes=64 * 1024,
        )
    )
    assert mime == "image/png"
    assert b64 is not None


def test_image_download_service_blocks_ssrf_redirects_and_oversized_streams() -> None:
    from core.events.image_download_service import ImageDownloadService

    svc = ImageDownloadService()

    # 1. Direct SSRF blocked
    session1 = _FakeSession([])
    svc._aiohttp_session = session1
    path, is_gif = asyncio.run(svc.download_to_temp("http://127.0.0.1/secret.png"))
    assert (path, is_gif) == (None, False)
    assert session1.calls == []

    # 2. Redirect to metadata IP blocked
    session2 = _FakeSession(
        [
            _FakeResponse(
                status=302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
            )
        ]
    )
    svc._aiohttp_session = session2
    path, is_gif = asyncio.run(svc.download_to_temp("https://93.184.216.34/redir.png"))
    assert (path, is_gif) == (None, False)
    assert len(session2.calls) == 1
    assert session2.calls[0][1].get("allow_redirects") is False

    # 3. Streaming oversize aborts early & cleans up temp file
    oversized = _FakeResponse(
        status=200,
        headers={"Content-Type": "image/png"},
        chunks=[b"\x89PNG\r\n\x1a\n" + b"x" * 600, b"y" * 600, b"z" * 600],
    )
    session3 = _FakeSession([oversized])
    svc._aiohttp_session = session3
    path, is_gif = asyncio.run(
        svc.download_to_temp("https://93.184.216.34/big.png", max_bytes=1024)
    )
    assert (path, is_gif) == (None, False)
    assert oversized.content.chunks_yielded == 2

    # 4. Valid bounded PNG writes temp file cleanly
    png_data = _make_png_bytes()
    ok_resp = _FakeResponse(
        status=200,
        headers={"Content-Type": "image/png"},
        chunks=[png_data[:16], png_data[16:]],
    )
    session4 = _FakeSession([ok_resp])
    svc._aiohttp_session = session4
    path, is_gif = asyncio.run(
        svc.download_to_temp("https://93.184.216.34/ok.png", max_bytes=64 * 1024)
    )
    assert path is not None and Path(path).is_file()
    assert is_gif is False
    try:
        assert Path(path).read_bytes() == png_data
    finally:
        Path(path).unlink(missing_ok=True)


def test_openai_compat_blocks_ssrf_reference_urls() -> None:
    from tl.api.openai_compat import OpenAICompatProvider
    from tl.api_types import APIError, ApiRequestConfig

    provider = OpenAICompatProvider()
    cfg = ApiRequestConfig(
        prompt="draw cat",
        model="gpt-image-1",
        api_key="test-key",
        api_type="openai_compat",
        image_input_mode="auto",
        reference_images=["http://169.254.169.254/latest/meta-data/"],
    )
    with pytest.raises(APIError):
        asyncio.run(provider._prepare_payload(client=SimpleNamespace(), config=cfg))

    cfg_mixed = ApiRequestConfig(
        prompt="draw cat",
        model="gpt-image-1",
        api_key="test-key",
        api_type="openai_compat",
        image_input_mode="auto",
        reference_images=[
            "http://127.0.0.1:8080/internal.png",
            "https://93.184.216.34/public.png",
        ],
    )
    payload = asyncio.run(
        provider._prepare_payload(client=SimpleNamespace(), config=cfg_mixed)
    )
    content = payload["messages"][0]["content"]
    urls = [
        item["image_url"]["url"]
        for item in content
        if isinstance(item, dict) and item.get("type") == "image_url"
    ]
    assert urls == ["https://93.184.216.34/public.png"]


# ---------------------------------------------------------------------------
# 4. Unified require_series_admin + CSRF coverage across PluginAPI (39 routes),
#    WebStudioAPI (27 routes), and MediaAPI (11 routes)
# ---------------------------------------------------------------------------


def _install_dashboard_auth_stub(monkeypatch, *, verified_user: str | None = "admin"):
    auth_mod = types.ModuleType("astrbot.dashboard.api.auth")

    async def require_dashboard_user(_raw):
        if not verified_user:
            raise PermissionError("unauthenticated")
        return verified_user

    auth_mod.require_dashboard_user = require_dashboard_user
    dash_mod = sys.modules.get("astrbot.dashboard") or types.ModuleType(
        "astrbot.dashboard"
    )
    api_mod = sys.modules.get("astrbot.dashboard.api") or types.ModuleType(
        "astrbot.dashboard.api"
    )
    api_mod.auth = auth_mod
    dash_mod.api = api_mod
    monkeypatch.setitem(sys.modules, "astrbot.dashboard", dash_mod)
    monkeypatch.setitem(sys.modules, "astrbot.dashboard.api", api_mod)
    monkeypatch.setitem(sys.modules, "astrbot.dashboard.api.auth", auth_mod)


def _make_web_request(
    *,
    method: str,
    username: str | None = "admin",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    path: str = "/astrbot_plugin_visual_media/test",
):
    hdrs = dict(headers or {})
    cks = dict(cookies or {})
    raw = SimpleNamespace(
        headers=hdrs,
        cookies=cks,
        method=method,
        url=SimpleNamespace(scheme="https"),
        _body=b"",
        _receive=None,
    )
    return SimpleNamespace(
        method=method,
        username=username,
        headers=hdrs,
        cookies=cks,
        query={},
        path=path,
        _request=raw,
    )


def _load_plugin_api_module(monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    pkg_name = "astrbot_plugin_visual_media"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(repo_root)]
        monkeypatch.setitem(sys.modules, pkg_name, pkg)

    fake_quart = types.ModuleType("quart")
    fake_quart.request = SimpleNamespace(args={})
    fake_quart.jsonify = lambda payload: payload

    async def _fake_send_file(path: str):
        return {"served": path}, 200

    fake_quart.send_file = _fake_send_file
    monkeypatch.setitem(sys.modules, "quart", fake_quart)

    web_mod = types.ModuleType("astrbot.api.web")
    web_mod.json_response = lambda data, status_code=200, headers=None: SimpleNamespace(
        data=data, status_code=status_code, headers=headers or {}
    )
    web_mod.error_response = (
        lambda message, status_code=400, data=None, headers=None: SimpleNamespace(
            data={"status": "error", "message": message, "data": data},
            status_code=status_code,
            headers=headers or {},
        )
    )
    web_mod.file_response = lambda *a, **kw: SimpleNamespace(status_code=200)
    web_mod.stream_response = lambda *a, **kw: SimpleNamespace(status_code=200)
    web_mod.request = None
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web_mod)

    # Stub astrbot.api.star when running offline without full AstrBot install
    if "astrbot.api.star" not in sys.modules:
        star_mod = types.ModuleType("astrbot.api.star")
        star_mod.StarTools = SimpleNamespace(get_data_dir=lambda *_a, **_kw: str(repo_root))
        monkeypatch.setitem(sys.modules, "astrbot.api.star", star_mod)
        if "astrbot.api" in sys.modules:
            monkeypatch.setattr(sys.modules["astrbot.api"], "star", star_mod, raising=False)

    # Stub heavy optional dependency rank_bm25 if not installed in unit env
    if "rank_bm25" not in sys.modules:
        bm25_mod = types.ModuleType("rank_bm25")
        bm25_mod.BM25Plus = object
        bm25_mod.BM25Okapi = object
        monkeypatch.setitem(sys.modules, "rank_bm25", bm25_mod)

    for key in list(sys.modules):
        if key == f"{pkg_name}.plugin_api" or key.startswith(f"{pkg_name}.api."):
            sys.modules.pop(key, None)

    plugin_api_mod = importlib.import_module(f"{pkg_name}.plugin_api")
    series_panels_mod = importlib.import_module(f"{pkg_name}.series_panels")
    return plugin_api_mod, series_panels_mod, web_mod


def test_plugin_api_all_39_routes_enforce_series_admin_and_csrf(monkeypatch) -> None:
    plugin_api_mod, series_panels_mod, web_mod = _load_plugin_api_module(monkeypatch)

    class _Ctx:
        def __init__(self):
            self.registered_web_apis = []

        def get_config(self):
            return {"dashboard": {"username": "admin"}}

        def register_web_api(self, route, handler, methods, desc):
            self.registered_web_apis.append((route, handler, methods, desc))

    ctx = _Ctx()
    plugin = SimpleNamespace(context=ctx)
    api = plugin_api_mod.PluginAPI(plugin)

    # Replace underlying route handlers with lightweight probes BEFORE register()
    # so we test the real registration + _guarded_handler wrapping on ALL 39 routes!
    for attr in dir(api):
        if attr.startswith("handle_"):

            async def _ok_handler(*_a, _name=attr, **_kw):
                return SimpleNamespace(status_code=200, data={"ok": _name})

            monkeypatch.setattr(api, attr, _ok_handler)

    api.register(ctx)
    assert len(ctx.registered_web_apis) == 42

    same_origin_headers = {
        "Host": "dashboard.local",
        "Origin": "https://dashboard.local",
        "Sec-Fetch-Site": "same-origin",
    }
    cross_site_headers = {
        "Host": "dashboard.local",
        "Origin": "https://evil.example",
        "Sec-Fetch-Site": "cross-site",
    }

    for route, handler, methods, _desc in ctx.registered_web_apis:
        primary_method = methods[0]

        # 1. Unauthenticated -> 403
        _install_dashboard_auth_stub(monkeypatch, verified_user=None)
        req = _make_web_request(
            method=primary_method, username="", headers=same_origin_headers, path=route
        )
        monkeypatch.setattr(series_panels_mod, "request", req)
        monkeypatch.setattr(web_mod, "request", req)
        resp = asyncio.run(handler())
        assert resp.status_code == 403, f"Unauthenticated allowed on {route}"

        # 2. api_key principal -> 403
        _install_dashboard_auth_stub(monkeypatch, verified_user="api_key:secret")
        req = _make_web_request(
            method=primary_method,
            username="api_key:secret",
            headers=same_origin_headers,
            path=route,
        )
        monkeypatch.setattr(series_panels_mod, "request", req)
        monkeypatch.setattr(web_mod, "request", req)
        resp = asyncio.run(handler())
        assert resp.status_code == 403, f"api_key allowed on {route}"

        # 3. Non-admin user -> 403
        _install_dashboard_auth_stub(monkeypatch, verified_user="other_user")
        req = _make_web_request(
            method=primary_method,
            username="other_user",
            headers=same_origin_headers,
            path=route,
        )
        monkeypatch.setattr(series_panels_mod, "request", req)
        monkeypatch.setattr(web_mod, "request", req)
        resp = asyncio.run(handler())
        assert resp.status_code == 403, f"non-admin allowed on {route}"

        # 4. Write routes reject cross-site & missing-origin CSRF -> 403
        for m in methods:
            if m.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                _install_dashboard_auth_stub(monkeypatch, verified_user="admin")
                bad_req = _make_web_request(
                    method=m, username="admin", headers=cross_site_headers, path=route
                )
                monkeypatch.setattr(series_panels_mod, "request", bad_req)
                monkeypatch.setattr(web_mod, "request", bad_req)
                bad_resp = asyncio.run(handler())
                assert bad_resp.status_code == 403, f"cross-site {m} allowed on {route}"

                # Missing Origin/Referer without Bearer/CSRF/Sec-Fetch-Site -> fail closed 403
                bare_req = _make_web_request(
                    method=m,
                    username="admin",
                    headers={"Host": "dashboard.local"},
                    path=route,
                )
                monkeypatch.setattr(series_panels_mod, "request", bare_req)
                monkeypatch.setattr(web_mod, "request", bare_req)
                bare_resp = asyncio.run(handler())
                assert bare_resp.status_code == 403, f"bare {m} allowed on {route}"

        # 5. Valid Dashboard admin with same-origin headers -> 200
        _install_dashboard_auth_stub(monkeypatch, verified_user="admin")
        for m in methods:
            ok_req = _make_web_request(
                method=m,
                username="admin",
                headers=same_origin_headers,
                path=route,
            )
            monkeypatch.setattr(series_panels_mod, "request", ok_req)
            monkeypatch.setattr(web_mod, "request", ok_req)
            ok_resp = asyncio.run(handler())
            assert ok_resp.status_code == 200, f"valid admin {m} rejected on {route}"


def test_web_studio_api_all_routes_use_require_series_admin_and_delete_csrf(
    monkeypatch,
) -> None:
    module = _load_web_api_module(monkeypatch)
    monkeypatch.setattr(module, "WEB_API_AVAILABLE", True)

    class _Ctx:
        def __init__(self):
            self.registered_web_apis = []

        def get_config(self):
            return {"dashboard": {"username": "admin"}}

        def register_web_api(self, route, handler, methods, desc):
            self.registered_web_apis.append((route, handler, methods, desc))

    ctx = _Ctx()
    api = module.WebStudioAPI(SimpleNamespace(), SimpleNamespace(), context=ctx)
    for _suffix, method_name, _methods, _desc in module.WebStudioAPI.ROUTES:

        async def _ok(*_a, _m=method_name, **_kw):
            return SimpleNamespace(status_code=200, data={"ok": _m})

        monkeypatch.setattr(api, method_name, _ok)

    entries = api.register(ctx)
    assert len(entries) == len(module.WebStudioAPI.ROUTES)

    same_origin = {
        "Host": "dashboard.local",
        "Origin": "https://dashboard.local",
        "Sec-Fetch-Site": "same-origin",
    }
    cross_site = {
        "Host": "dashboard.local",
        "Origin": "https://evil.example",
    }

    for route, handler, methods, _desc in entries:
        # Unauthenticated -> 403
        _install_dashboard_auth_stub(monkeypatch, verified_user=None)
        monkeypatch.setattr(
            module,
            "request",
            _make_web_request(method=methods[0], username="", headers=same_origin, path=route),
        )
        assert asyncio.run(handler()).status_code == 403

        # api_key -> 403
        _install_dashboard_auth_stub(monkeypatch, verified_user="api_key:k1")
        monkeypatch.setattr(
            module,
            "request",
            _make_web_request(
                method=methods[0], username="api_key:k1", headers=same_origin, path=route
            ),
        )
        assert asyncio.run(handler()).status_code == 403

        # Write / DELETE methods reject cross-site CSRF -> 403
        for m in methods:
            if m.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                _install_dashboard_auth_stub(monkeypatch, verified_user="admin")
                monkeypatch.setattr(
                    module,
                    "request",
                    _make_web_request(
                        method=m, username="admin", headers=cross_site, path=route
                    ),
                )
                assert (
                    asyncio.run(handler()).status_code == 403
                ), f"cross-site {m} allowed on {route}"

        # Valid admin passes -> 200
        _install_dashboard_auth_stub(monkeypatch, verified_user="admin")
        for m in methods:
            monkeypatch.setattr(
                module,
                "request",
                _make_web_request(
                    method=m, username="admin", headers=same_origin, path=route
                ),
            )
            assert asyncio.run(handler()).status_code == 200


def test_media_api_enforces_series_admin_and_delete_csrf(monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pkg_name = "astrbot_plugin_visual_media"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(repo_root)]
        monkeypatch.setitem(sys.modules, pkg_name, pkg)

    web_mod = types.ModuleType("astrbot.api.web")
    web_mod.json_response = lambda data, status_code=200, headers=None: SimpleNamespace(
        data=data, status_code=status_code, headers=headers or {}
    )
    web_mod.request = None
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web_mod)

    sys.modules.pop(f"{pkg_name}.media_api", None)
    media_api_mod = importlib.import_module(f"{pkg_name}.media_api")

    ctx = SimpleNamespace(get_config=lambda: {"dashboard": {"username": "admin"}})
    owner = SimpleNamespace(context=ctx, _closed=False)
    api = media_api_mod.MediaAPI(owner)

    # Cross-site DELETE /profiles -> 403
    _install_dashboard_auth_stub(monkeypatch, verified_user="admin")
    bad_req = _make_web_request(
        method="DELETE",
        username="admin",
        headers={"Host": "dashboard.local", "Origin": "https://evil.example"},
        path="/astrbot_plugin_visual_media/media/profiles",
    )
    monkeypatch.setattr(media_api_mod, "request", bad_req)
    assert asyncio.run(api.profiles()).status_code == 403

    # api_key GET /overview -> 403
    _install_dashboard_auth_stub(monkeypatch, verified_user="api_key:k1")
    api_key_req = _make_web_request(
        method="GET",
        username="api_key:k1",
        headers={"Host": "dashboard.local"},
        path="/astrbot_plugin_visual_media/media/overview",
    )
    monkeypatch.setattr(media_api_mod, "request", api_key_req)
    assert asyncio.run(api.overview()).status_code == 403

    # Same-origin admin DELETE /profiles -> passes guard!
    _install_dashboard_auth_stub(monkeypatch, verified_user="admin")
    ok_req = _make_web_request(
        method="DELETE",
        username="admin",
        headers={
            "Host": "dashboard.local",
            "Origin": "https://dashboard.local",
            "Sec-Fetch-Site": "same-origin",
        },
        path="/astrbot_plugin_visual_media/media/profiles",
    )
    monkeypatch.setattr(media_api_mod, "request", ok_req)
    assert asyncio.run(api.guard(check_csrf=True)) is None


# ---------------------------------------------------------------------------
# 5. Rework regressions (#43): _download_image redirect SSRF + hostname proxy
#    compatibility vs direct _SSRFGuardedResolver DNS-rebinding protection
# ---------------------------------------------------------------------------


def test_tl_api_download_image_blocks_redirect_ssrf_and_honors_proxy_dns_mode(
    tmp_path: Path, monkeypatch
) -> None:
    import tl.tl_api as tl_api_mod

    client = object.__new__(tl_api_mod.GeminiAPIClient)
    client.proxy = None

    # 1. Public URL redirecting to cloud metadata IP must be blocked before hop 2
    session_meta = _FakeSession(
        [
            _FakeResponse(
                status=302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
            )
        ]
    )
    res_url, res_path = asyncio.run(
        client._download_image("https://93.184.216.34/start.png", session_meta)
    )
    assert (res_url, res_path) == (None, None)
    assert len(session_meta.calls) == 1
    assert session_meta.calls[0][1].get("allow_redirects") is False

    # 2. Public URL redirecting to loopback IP must be blocked before hop 2
    session_loop = _FakeSession(
        [
            _FakeResponse(
                status=301,
                headers={"Location": "http://127.0.0.1:8080/internal.png"},
            )
        ]
    )
    res_url, res_path = asyncio.run(
        client._download_image("https://93.184.216.34/start.png", session_loop)
    )
    assert (res_url, res_path) == (None, None)
    assert len(session_loop.calls) == 1

    # 3. With explicit hostname proxy, resolve_dns=False is passed so local DNS
    #    isn't used for target host, while static SSRF target is still blocked!
    seen_calls: list[tuple[str, bool]] = []
    real_validate = validate_remote_url_async

    async def spy_validate(url: str, *, allow_private: bool = False, resolve_dns: bool = True):
        seen_calls.append((url, resolve_dns))
        return await real_validate(
            url, allow_private=allow_private, resolve_dns=resolve_dns
        )

    monkeypatch.setattr("tl.core.path_policy.validate_remote_url_async", spy_validate)

    # Static SSRF target still rejected even with proxy
    session_ssrf_proxy = _FakeSession([])
    assert asyncio.run(
        client._download_image(
            "http://127.0.0.1:8080/secret.png",
            session_ssrf_proxy,
            proxy="http://localhost:7890",
        )
    ) == (None, None)
    assert session_ssrf_proxy.calls == []
    assert seen_calls[-1] == ("http://127.0.0.1:8080/secret.png", False)

    # Valid public domain succeeds through hostname proxy with resolve_dns=False
    saved_file = tmp_path / "downloaded.png"
    png_bytes = _make_png_bytes()

    async def fake_save_stream(stream_reader, image_format="png", target_path=None):
        saved_file.write_bytes(png_bytes)
        return str(saved_file)

    monkeypatch.setattr(tl_api_mod, "save_image_stream", fake_save_stream)
    session_ok = _FakeSession(
        [
            _FakeResponse(
                status=200,
                headers={"Content-Type": "image/png"},
                chunks=[png_bytes],
            )
        ]
    )
    url_out, path_out = asyncio.run(
        client._download_image(
            "https://public.example/img.png",
            session_ok,
            proxy="http://clash:7890",
        )
    )
    assert url_out == str(saved_file)
    assert path_out == str(saved_file)
    assert seen_calls[-1] == ("https://public.example/img.png", False)
    assert session_ok.calls[0][1].get("proxy") == "http://clash:7890"
    assert session_ok.calls[0][1].get("allow_redirects") is False


def test_hostname_proxy_disables_ssrf_resolver_while_direct_keeps_it(
    tmp_path: Path, monkeypatch
) -> None:
    import tl.core.remote_fetch as rf_mod
    import tl.reference_image as ref_mod
    from tl.core.errors import RemoteFetchRejectedError

    # Clear env proxy variables for deterministic direct-mode testing
    for k in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(k, raising=False)

    created_resolvers: list[object] = []

    class _FakeConnector:
        def __init__(self, *args, resolver=None, **kwargs):
            created_resolvers.append(resolver)

    class _FakeClientSession:
        def __init__(self, *args, **kwargs):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url, **kwargs):
            png = _make_png_bytes()
            return _FakeResponse(
                status=200,
                headers={"Content-Type": "image/png", "Content-Length": str(len(png))},
                chunks=[png],
            )

    monkeypatch.setattr(rf_mod.aiohttp, "TCPConnector", _FakeConnector)
    monkeypatch.setattr(rf_mod.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ref_mod.aiohttp, "TCPConnector", _FakeConnector)
    monkeypatch.setattr(ref_mod.aiohttp, "ClientSession", _FakeClientSession)

    # 1. RemoteFetchService with hostname proxy -> resolver=None (won't block localhost/clash proxy)
    created_resolvers.clear()
    svc = rf_mod.RemoteFetchService()
    data = asyncio.run(
        svc.fetch("https://93.184.216.34/img.png", proxy="http://localhost:7890")
    )
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert created_resolvers == [None]

    # 2. RemoteFetchService with proxy STILL blocks SSRF targets statically
    with pytest.raises(RemoteFetchRejectedError):
        asyncio.run(
            svc.fetch(
                "http://169.254.169.254/latest/meta-data/",
                proxy="http://host.docker.internal:7890",
            )
        )

    # 3. RemoteFetchService direct (no proxy) -> attaches _SSRFGuardedResolver
    created_resolvers.clear()
    asyncio.run(svc.fetch("https://93.184.216.34/img.png"))
    assert len(created_resolvers) == 1
    assert isinstance(created_resolvers[0], rf_mod._SSRFGuardedResolver)

    # 4. normalize_reference_image_input with hostname proxy -> resolver=None
    created_resolvers.clear()
    mime, b64 = asyncio.run(
        ref_mod.normalize_reference_image_input(
            "https://93.184.216.34/ref-proxy.png",
            image_cache_dir=tmp_path / "cache_proxy",
            proxy="http://clash:7890",
        )
    )
    assert mime == "image/png" and b64 is not None
    assert created_resolvers == [None]

    # 5. normalize_reference_image_input direct (no proxy) -> attaches _SSRFGuardedResolver
    created_resolvers.clear()
    mime, b64 = asyncio.run(
        ref_mod.normalize_reference_image_input(
            "https://93.184.216.34/ref-direct.png",
            image_cache_dir=tmp_path / "cache_direct",
        )
    )
    assert mime == "image/png" and b64 is not None
    assert len(created_resolvers) == 1
    assert isinstance(created_resolvers[0], rf_mod._SSRFGuardedResolver)
