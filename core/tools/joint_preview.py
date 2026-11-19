"""Content-addressed compact previews for the reply provider."""

import base64
import hashlib
import os
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from io import BytesIO
from tempfile import SpooledTemporaryFile
from threading import BoundedSemaphore, Lock

from PIL import Image

DEFAULT_PREVIEW_MAX_SIZE = 320
DEFAULT_PREVIEW_FORMAT = "webp"
DEFAULT_PREVIEW_QUALITY = 75


def _file_version(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def is_preview_source_current(path, source_version):
    try:
        return _file_version(os.stat(path)) == source_version
    except OSError:
        return False


def _webp_supported() -> bool:
    try:
        from PIL import features as pil_features

        return bool(pil_features.check("webp"))
    except Exception:
        return False


def resolve_preview_format(fmt: str | None) -> str:
    normalized = str(fmt or DEFAULT_PREVIEW_FORMAT).strip().lower()
    if normalized == "jpg":
        normalized = "jpeg"
    if normalized not in ("webp", "jpeg", "png"):
        normalized = DEFAULT_PREVIEW_FORMAT
    if normalized == "webp" and not _webp_supported():
        return "jpeg"
    return normalized


def _bounded_int(value, minimum: int, maximum: int, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _to_rgb_preview(source, max_size: int, resample):
    if source.mode in ("RGBA", "LA") or (
        source.mode == "P" and "transparency" in getattr(source, "info", {})
    ):
        rgba = source.convert("RGBA")
        preview = Image.new("RGB", rgba.size, (255, 255, 255))
        preview.paste(rgba, mask=rgba.split()[3])
    else:
        preview = source.convert("RGB")
    if resample is not None:
        preview.thumbnail((max_size, max_size), resample)
    else:
        preview.thumbnail((max_size, max_size))
    return preview


def _encode_jpeg_preview(source, max_size: int, quality: int, resample) -> bytes:
    preview = _to_rgb_preview(source, max_size, resample)
    output = BytesIO()
    preview.save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue()


@dataclass(frozen=True)
class JointPreview:
    url: str
    digest: str
    source_version: tuple
    cache_reason: str


class PreviewSourceChanged(OSError):
    """The image changed while its preview source was being read."""


class JointPreviewCache:
    """Bound retained previews and coalesce concurrent renders of the same bytes."""

    def __init__(self, *, max_bytes=16 * 1024**2, max_entries=256, concurrency=4):
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._bytes = 0
        self._items = OrderedDict()
        self._pending = {}
        self._lock = Lock()
        self._slots = BoundedSemaphore(concurrency)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def get(
        self,
        path,
        *,
        max_size=DEFAULT_PREVIEW_MAX_SIZE,
        fmt=DEFAULT_PREVIEW_FORMAT,
        quality=DEFAULT_PREVIEW_QUALITY,
    ):
        # Hash the source on every access. A replacement can retain path, size,
        # and mtime. Decode the same snapshot that supplied the fingerprint.
        max_dim = _bounded_int(max_size, 128, 768, DEFAULT_PREVIEW_MAX_SIZE)
        q = _bounded_int(quality, 40, 95, DEFAULT_PREVIEW_QUALITY)
        effective_fmt = resolve_preview_format(fmt)
        resample = getattr(getattr(Image, "Resampling", None), "LANCZOS", None)
        with self._slots, SpooledTemporaryFile(max_size=2 * 1024**2) as snapshot:
            digest = hashlib.sha256()
            with open(path, "rb") as source:
                version = _file_version(os.fstat(source.fileno()))
                for chunk in iter(lambda: source.read(256 * 1024), b""):
                    digest.update(chunk)
                    snapshot.write(chunk)
                if _file_version(os.fstat(source.fileno())) != version:
                    raise PreviewSourceChanged
            fingerprint = digest.hexdigest()
            mode = "RGBA" if effective_fmt == "png" else "RGB"
            transform = (max_dim, 0, mode, resample, effective_fmt.upper(), q)
            url, reason = self._get_or_render(
                (fingerprint, transform), snapshot, transform
            )
            return JointPreview(url, fingerprint, version, reason)

    def _get_or_render(self, key, snapshot, transform):
        with self._lock:
            url = self._items.get(key)
            if url is not None:
                self._items.move_to_end(key)
                return url, "hit"
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = self._pending[key] = Future()
        if not owner:
            return future.result(), "coalesced"

        try:
            snapshot.seek(0)
            url = self._render(snapshot, transform)
            reason = "oversize"
            if len(url) <= self._max_bytes and self._max_entries > 0:
                with self._lock:
                    self._items[key] = url
                    self._bytes += len(url)
                    while (
                        self._bytes > self._max_bytes
                        or len(self._items) > self._max_entries
                    ):
                        _, evicted = self._items.popitem(last=False)
                        self._bytes -= len(evicted)
                reason = "miss"
            future.set_result(url)
            return url, reason
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._pending.pop(key)

    @staticmethod
    def _render(snapshot, transform):
        max_size, frame, mode, resample, image_format, quality = transform
        fmt_upper = str(image_format).upper()
        with Image.open(snapshot) as source:
            source.seek(frame)
            if fmt_upper == "JPEG":
                raw = _encode_jpeg_preview(source, max_size, quality, resample)
                mime = "image/jpeg"
            elif fmt_upper == "PNG":
                preview = source.convert(mode)
                if resample is not None:
                    preview.thumbnail((max_size, max_size), resample)
                else:
                    preview.thumbnail((max_size, max_size))
                output = BytesIO()
                preview.save(output, format="PNG", optimize=True)
                raw = output.getvalue()
                mime = "image/png"
            else:
                try:
                    preview = _to_rgb_preview(source, max_size, resample)
                    output = BytesIO()
                    preview.save(output, format="WEBP", quality=quality, method=4)
                    raw = output.getvalue()
                    mime = "image/webp"
                except (KeyError, OSError, ValueError):
                    source.seek(frame)
                    raw = _encode_jpeg_preview(source, max_size, quality, resample)
                    mime = "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
