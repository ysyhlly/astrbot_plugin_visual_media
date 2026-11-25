"""Reference image normalization helpers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger

from .core.errors import PathPolicyError, redact_url
from .core.path_policy import validate_remote_url_async
from .core.remote_fetch import _SSRFGuardedResolver

try:
    from PIL import Image as PILImage
except ImportError:  # pragma: no cover - depends on runtime optional dependency
    PILImage = None


SUPPORTED_REFERENCE_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
)

QQ_REFERENCE_IMAGE_HOSTS = frozenset(
    {"qpic.cn", "qq.com", "nt.qq.com", "gchat.qpic.cn"}
)

MAX_REFERENCE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_REFERENCE_IMAGE_PIXELS = 40_000_000
MAX_REFERENCE_IMAGE_REDIRECTS = 5
_REFERENCE_READ_CHUNK_BYTES = 64 * 1024
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SENSITIVE_REDIRECT_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "cookie2"}
)


def _url_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port)


async def _read_bounded_response(
    resp: aiohttp.ClientResponse,
    max_bytes: int,
) -> bytes:
    """流式读取 HTTP 响应体并严格执行字节上限，防止超大响应导致 OOM。"""
    raw_len = (resp.headers.get("Content-Length", "") if getattr(resp, "headers", None) else "").strip()
    if raw_len.isdigit() and int(raw_len) > max_bytes:
        raise ValueError(f"参考图响应体超过大小上限 ({max_bytes} bytes)")

    content = getattr(resp, "content", None)
    iter_chunked = getattr(content, "iter_chunked", None)
    if callable(iter_chunked):
        chunks: list[bytes] = []
        total = 0
        async for chunk in iter_chunked(_REFERENCE_READ_CHUNK_BYTES):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"参考图响应体超过大小上限 ({max_bytes} bytes)")
            chunks.append(chunk)
        return b"".join(chunks)

    data = await resp.read()
    if len(data) > max_bytes:
        raise ValueError(f"参考图响应体超过大小上限 ({max_bytes} bytes)")
    return data


def get_reference_image_cache_dir() -> Path:
    """Return the cache directory used for downloaded reference images.

    缓存放 AstrBot 共享临时目录下的插件专属子目录，由 AstrBot 统一清理。
    本函数在模块导入时即被调用（tl_utils 依赖本模块），不能直接 import
    tl_utils，否则循环导入会静默走回退分支；这里自行解析临时目录。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        base = Path(get_astrbot_temp_path())
    except Exception:
        import tempfile

        base = Path(tempfile.gettempdir())
    return base / "astrbot_plugin_visual_media" / "download_cache"


REFERENCE_IMAGE_CACHE_DIR = get_reference_image_cache_dir()


def extract_reference_image_source(image_input: Any) -> str:
    """Normalize light text wrappers around a reference image source."""
    image_str = str(image_input or "").strip()
    md_match = re.search(r"!\[[^\]]*\]\(\s*([^)]+)\s*\)", image_str)
    if md_match:
        image_str = md_match.group(1).strip()
    if "&amp;" in image_str:
        image_str = image_str.replace("&amp;", "&")
    return image_str


def host_matches_domain(host: str | None, domain: str) -> bool:
    """Return whether host equals domain or is a subdomain of it."""
    if not host:
        return False
    host_lower = host.lower().rstrip(".")
    return host_lower == domain or host_lower.endswith("." + domain)


def is_qq_image_host(host: str) -> bool:
    """Return whether host belongs to QQ image delivery."""
    if not host:
        return False
    return any(
        host_matches_domain(host, qq_host) for qq_host in QQ_REFERENCE_IMAGE_HOSTS
    )


# 开头即可判定的格式；表驱动，风格同 _TRANSCODE_MAGIC_PREFIXES。
# WEBP/HEIC/HEIF 需按偏移判定，不在此表内。
_PREFIX_MAGIC_MIMES: tuple[tuple[str, tuple[bytes, ...]], ...] = (
    ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ("image/jpeg", (b"\xff\xd8",)),
)

# ISO-BMFF 的品牌盒在偏移 4 处，不是文件开头，故单独按偏移判定。
_HEIC_BRANDS = frozenset({b"ftypheic", b"ftyphevc"})
_HEIF_BRANDS = frozenset({b"ftypheif", b"ftypmif1", b"ftypmsf1"})


def detect_reference_image_mime(raw: bytes) -> str | None:
    """Detect MIME for image formats providers can receive directly."""

    if not raw or len(raw) < 4:
        return None
    for mime_type, prefixes in _PREFIX_MAGIC_MIMES:
        if raw.startswith(prefixes):
            return mime_type
    if len(raw) >= 12:
        if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            return "image/webp"
        brand = raw[4:12]
        if brand in _HEIC_BRANDS:
            return "image/heic"
        if brand in _HEIF_BRANDS:
            return "image/heif"
    return None


def build_reference_image_headers(
    host: str = "", for_qq: bool = False
) -> dict[str, str]:
    """Build HTTP headers suitable for fetching reference images."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip, deflate, br",
    }
    if host:
        scheme = "https" if not for_qq else "http"
        headers["Referer"] = f"{scheme}://{host}"

    if for_qq or is_qq_image_host(host):
        headers["Referer"] = "https://qun.qq.com"
        headers["Origin"] = "https://qun.qq.com"
        if ",image/png" not in headers["Accept"]:
            headers["Accept"] += ",image/png"

    return headers


def check_reference_image_cache(url: str, cache_dir: Path) -> Path | None:
    """Return a cached reference image path for url when present."""
    try:
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = next(
            (
                p
                for p in cache_dir.glob(f"{cache_key}.*")
                if p.exists() and p.stat().st_size > 0
            ),
            None,
        )
        return cached
    except Exception as e:
        logger.debug(f"检查参考图缓存失败: {e}")
        return None


def save_reference_image_cache(
    url: str, data: bytes, mime_type: str, cache_dir: Path
) -> Path | None:
    """Save downloaded reference image bytes to cache."""
    try:
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        suffix = mime_type.split("/")[-1] if "/" in mime_type else "png"
        cache_file = cache_dir / f"{cache_key}.{suffix}"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(data)
        return cache_file
    except Exception as e:
        logger.debug(f"写入参考图缓存失败: {e}")
        return None


def _encode_file_to_base64(file_path: Path, chunk_size: int = 65536) -> str:
    chunk_size = (chunk_size // 3) * 3
    if chunk_size == 0:
        chunk_size = 3

    encoded_parts: list[str] = []
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            encoded_parts.append(base64.b64encode(chunk).decode("utf-8"))
    return "".join(encoded_parts)


def coerce_reference_image_bytes(
    mime_type: str | None,
    raw_bytes: bytes,
    *,
    max_bytes: int = MAX_REFERENCE_IMAGE_BYTES,
) -> tuple[str | None, str | None]:
    """
    Convert image bytes to a provider-supported MIME/base64 pair.

    PNG/JPEG/WEBP/HEIC/HEIF bytes pass through unchanged. Unsupported formats are
    re-encoded with Pillow when it is available.
    """
    if len(raw_bytes) > max_bytes:
        logger.warning(
            f"参考图超过单图字节上限: size={len(raw_bytes)}, limit={max_bytes}"
        )
        return None, None

    normalized_mime = (mime_type or "").lower()
    detected_mime = detect_reference_image_mime(raw_bytes)
    if detected_mime:
        if normalized_mime and normalized_mime != detected_mime:
            logger.debug(
                "参考图 MIME 与文件头不一致，使用文件头识别结果: "
                f"{normalized_mime} -> {detected_mime}"
            )
        return detected_mime, base64.b64encode(raw_bytes).decode("utf-8")

    target_mime = (
        normalized_mime
        if normalized_mime in SUPPORTED_REFERENCE_IMAGE_MIME_TYPES
        else "image/png"
    )
    if PILImage is None:
        logger.warning(f"参考图格式不受支持且 Pillow 不可用: mime={mime_type}")
        return None, None

    try:
        with PILImage.open(io.BytesIO(raw_bytes)) as img:
            if img.width * img.height > MAX_REFERENCE_IMAGE_PIXELS:
                logger.warning(
                    f"参考图像素尺寸超过上限: {img.width}x{img.height} > {MAX_REFERENCE_IMAGE_PIXELS}"
                )
                return None, None
            if target_mime == "image/png":
                save_format = "PNG"
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
            elif target_mime == "image/jpeg":
                save_format = "JPEG"
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
            elif target_mime == "image/webp":
                save_format = "WEBP"
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
            else:
                save_format = "PNG"
                target_mime = "image/png"
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")

            buffer = io.BytesIO()
            img.save(buffer, format=save_format)
            buffer.seek(0)
            out_bytes = buffer.read()
            if len(out_bytes) > max_bytes:
                logger.warning(
                    f"参考图转码后超过单图字节上限: size={len(out_bytes)}, limit={max_bytes}"
                )
                return None, None
            encoded = base64.b64encode(out_bytes).decode("utf-8")
            return target_mime, encoded
    except Exception as e:
        logger.warning(f"参考图格式不受支持且转换失败: mime={mime_type}, err={e}")
        return None, None


def coerce_reference_image(
    mime_type: str | None,
    base64_data: str,
    *,
    max_bytes: int = MAX_REFERENCE_IMAGE_BYTES,
) -> tuple[str | None, str | None]:
    """Decode base64 image data and coerce it to a supported reference image."""
    if len(base64_data) > ((max_bytes * 4) // 3 + 16):
        logger.warning(
            f"参考图 base64 超过字节上限: len={len(base64_data)}, limit={max_bytes}"
        )
        return None, None
    try:
        raw = base64.b64decode(base64_data, validate=False)
    except Exception as e:
        logger.warning(f"base64 解码失败，无法转换为参考图: {e}")
        return None, None
    return coerce_reference_image_bytes(mime_type, raw, max_bytes=max_bytes)


async def normalize_reference_image_input(
    image_input: Any,
    *,
    image_cache_dir: Path | None = None,
    image_input_mode: str = "force_base64",
    session: aiohttp.ClientSession | None = None,
    proxy: str | None = None,
    max_bytes: int = MAX_REFERENCE_IMAGE_BYTES,
) -> tuple[str | None, str | None]:
    """
    Normalize a reference image input to (mime_type, base64_data).

    Supports data URI, raw/relaxed base64, file://, bare filesystem paths and
    http(s) URLs.
    """
    try:
        if image_input is None:
            return None, None

        image_str = extract_reference_image_source(image_input)
        if not image_str:
            return None, None

        cache_dir = image_cache_dir or REFERENCE_IMAGE_CACHE_DIR
        logger.debug(
            f"规范化参考图输入: len={len(image_str)} "
            f"type={type(image_input)} mode={image_input_mode}"
        )

        if image_str.startswith("data:image/") and ";base64," in image_str:
            header, data = image_str.split(";base64,", 1)
            mime_type = header.replace("data:", "")
            logger.debug(f"检测到参考图 data URI，mime={mime_type}")
            if len(data) > ((max_bytes * 4) // 3 + 16):
                logger.warning("参考图 data URL 超过单图字节上限")
                return None, None

            def _decode_and_coerce_data_uri() -> tuple[str | None, str | None]:
                try:
                    raw = base64.b64decode(data, validate=False)
                except Exception:
                    logger.warning("参考图 data URL base64 解码失败")
                    return None, None
                return coerce_reference_image_bytes(
                    mime_type, raw, max_bytes=max_bytes
                )

            return await asyncio.to_thread(_decode_and_coerce_data_uri)

        if image_str.startswith("file://"):
            parsed = urllib.parse.urlparse(image_str)
            image_path = Path(urllib.request.url2pathname(parsed.path))

            def _read_file_uri() -> tuple[str, tuple[str | None, str | None]]:
                if not (image_path.exists() and image_path.is_file()):
                    return "missing", (None, None)
                suffix = image_path.suffix.lower().lstrip(".") or "png"
                mime_type = f"image/{suffix}"
                try:
                    if image_path.stat().st_size > max_bytes:
                        logger.warning(f"参考图 file:// 超过大小上限: {image_path}")
                        return "too_large", (None, None)
                    data_bytes = image_path.read_bytes()
                    return "ok", coerce_reference_image_bytes(
                        mime_type, data_bytes, max_bytes=max_bytes
                    )
                except Exception as e:
                    logger.warning(f"读取参考图 file:// 路径失败: {e}")
                    return "error", (None, None)

            status, coerced = await asyncio.to_thread(_read_file_uri)
            if status == "ok":
                logger.debug(f"使用参考图 file:// 路径: {image_path}")
                return coerced
            if status == "too_large":
                return None, None
            if status == "missing":
                logger.warning(f"参考图 file:// 路径不存在: {image_str}")

        def _read_local_path() -> tuple[str, tuple[str | None, str | None]]:
            try:
                is_local_file = os.path.isfile(image_str)
            except (OSError, ValueError):
                is_local_file = False
            if not is_local_file:
                return "not_file", (None, None)
            image_path = Path(image_str)
            suffix = image_path.suffix.lower().lstrip(".") or "png"
            mime_type = f"image/{suffix}"
            try:
                if image_path.stat().st_size > max_bytes:
                    logger.warning(f"参考图本地路径超过大小上限: {image_path}")
                    return "too_large", (None, None)
                data_bytes = image_path.read_bytes()
                return "ok", coerce_reference_image_bytes(
                    mime_type, data_bytes, max_bytes=max_bytes
                )
            except Exception as e:
                logger.warning(f"读取参考图本地路径失败: {e}")
                return "error", (None, None)

        local_status, local_coerced = await asyncio.to_thread(_read_local_path)
        if local_status == "ok":
            logger.debug(f"使用参考图本地路径: {image_str}")
            return local_coerced
        if local_status == "too_large":
            return None, None

        if image_str.startswith(("http://", "https://")):
            cleaned_url = image_str.replace("&amp;", "&")
            parsed_url = urllib.parse.urlparse(cleaned_url)
            parsed_host = parsed_url.netloc or ""
            is_qq = is_qq_image_host(parsed_host)
            request_proxy = None if is_qq else proxy
            trust_env = not is_qq
            has_env_proxy = trust_env and any(
                os.environ.get(key)
                for key in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                )
            )
            use_proxy = bool(request_proxy) or has_env_proxy
            try:
                cleaned_url = await validate_remote_url_async(
                    cleaned_url, resolve_dns=not use_proxy
                )
            except PathPolicyError as e:
                logger.warning(f"参考图 URL 被安全策略拒绝: {e}")
                return None, None

            logger.debug(f"下载 http(s) 参考图: {redact_url(cleaned_url)}")

            def _read_cached_reference() -> tuple[str, tuple[str | None, str | None]]:
                cached = check_reference_image_cache(cleaned_url, cache_dir)
                if not cached:
                    return "miss", (None, None)
                mime_guess = f"image/{cached.suffix.lstrip('.') or 'png'}"
                try:
                    if cached.stat().st_size > max_bytes:
                        logger.warning(f"参考图缓存文件超过大小上限: {cached}")
                        return "too_large", (None, None)
                    data_bytes = cached.read_bytes()
                except Exception as e:
                    logger.warning(f"读取参考图缓存失败: {e}")
                    return "error", (None, None)
                return "hit", coerce_reference_image_bytes(
                    mime_guess, data_bytes, max_bytes=max_bytes
                )

            cache_status, cached_coerced = await asyncio.to_thread(
                _read_cached_reference
            )
            if cache_status == "hit":
                logger.debug(f"参考图命中缓存: {redact_url(cleaned_url)}")
                return cached_coerced
            if cache_status == "too_large":
                return None, None

            headers = build_reference_image_headers(parsed_host, for_qq=is_qq)

            timeout = aiohttp.ClientTimeout(total=20, connect=10)

            async def fetch_with_session(
                active_session: aiohttp.ClientSession,
            ) -> tuple[str | None, str | None]:
                fallback_reason = None
                current_url = cleaned_url
                req_headers = dict(headers)

                try:
                    for hop in range(MAX_REFERENCE_IMAGE_REDIRECTS + 1):
                        current_url = await validate_remote_url_async(
                            current_url, resolve_dns=not use_proxy
                        )
                        async with active_session.get(
                            current_url,
                            headers=req_headers,
                            timeout=timeout,
                            proxy=request_proxy,
                            allow_redirects=False,
                        ) as resp:
                            if resp.status in _REDIRECT_STATUSES:
                                if hop >= MAX_REFERENCE_IMAGE_REDIRECTS:
                                    fallback_reason = (
                                        f"重定向超过 {MAX_REFERENCE_IMAGE_REDIRECTS} 次"
                                    )
                                    break
                                location = resp.headers.get("Location", "")
                                if not location:
                                    fallback_reason = "重定向缺少 Location 头"
                                    break
                                next_url = urllib.parse.urljoin(current_url, location)
                                next_url = await validate_remote_url_async(
                                    next_url, resolve_dns=not use_proxy
                                )
                                if _url_origin(next_url) != _url_origin(current_url):
                                    req_headers = {
                                        k: v
                                        for k, v in req_headers.items()
                                        if k.lower() not in _SENSITIVE_REDIRECT_HEADERS
                                    }
                                current_url = next_url
                                continue

                            if resp.status == 200:
                                content_type = resp.headers.get(
                                    "Content-Type", "image/png"
                                )
                                mime_type = (
                                    content_type.split(";")[0]
                                    if content_type
                                    else "image/png"
                                )
                                try:
                                    data_bytes = await _read_bounded_response(
                                        resp, max_bytes
                                    )
                                except Exception as e:
                                    fallback_reason = f"读取响应体失败: {e}"
                                else:
                                    def _cache_and_coerce() -> tuple[str | None, str | None]:
                                        save_reference_image_cache(
                                            cleaned_url, data_bytes, mime_type, cache_dir
                                        )
                                        return coerce_reference_image_bytes(
                                            mime_type, data_bytes, max_bytes=max_bytes
                                        )

                                    return await asyncio.to_thread(_cache_and_coerce)
                                break
                            fallback_reason = f"HTTP {resp.status} {resp.reason}"
                            break
                except PathPolicyError as e:
                    fallback_reason = f"安全策略拒绝: {e}"
                except Exception as e:
                    fallback_reason = str(e)
                if fallback_reason:
                    logger.warning(
                        f"下载参考图失败: {redact_url(cleaned_url)}，原因: {fallback_reason}"
                    )
                return None, None

            if session is not None and not session.closed:
                return await fetch_with_session(session)

            connector = aiohttp.TCPConnector(
                resolver=None if use_proxy else _SSRFGuardedResolver()
            )
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=trust_env, connector=connector
            ) as new_session:
                return await fetch_with_session(new_session)

        logger.debug("尝试将参考图输入视为纯 base64")

        def _decode_raw_base64() -> tuple[str | None, str | None]:
            try:
                if len(image_str) > ((max_bytes * 4) // 3 + 16):
                    return None, None
                base64.b64decode(image_str, validate=False)
                return coerce_reference_image(None, image_str, max_bytes=max_bytes)
            except binascii.Error:
                cleaned = re.sub(r"[^A-Za-z0-9+/=_-]", "", image_str)
                if len(cleaned) > ((max_bytes * 4) // 3 + 16):
                    return None, None
                pad_len = (-len(cleaned)) % 4
                if pad_len:
                    cleaned += "=" * pad_len
                try:
                    base64.b64decode(cleaned, validate=False)
                    return coerce_reference_image(None, cleaned, max_bytes=max_bytes)
                except Exception:
                    return None, None

        return await asyncio.to_thread(_decode_raw_base64)

    except Exception as e:
        logger.error(f"规范化参考图输入失败: {e}")
        return None, None
