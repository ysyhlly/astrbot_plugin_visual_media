"""图片下载服务：负责 HTTP 下载、连接池管理和超时处理。"""

import asyncio
import os
import shutil
import tempfile
from typing import Any
from urllib.parse import urljoin

import aiohttp

from astrbot.api import logger

try:
    from ...tl.core.errors import PathPolicyError, redact_url
    from ...tl.core.path_policy import validate_remote_url_async
    from ...tl.core.remote_fetch import _SSRFGuardedResolver
except (ImportError, ValueError):
    from tl.core.errors import PathPolicyError, redact_url
    from tl.core.path_policy import validate_remote_url_async
    from tl.core.remote_fetch import _SSRFGuardedResolver


class ImageDownloadService:
    """基于 aiohttp 的图片下载服务，复用连接池。"""

    ALLOWED_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
    HTTP_TIMEOUT_SECONDS = 30
    HTTP_CONNECTOR_LIMIT = 10
    HTTP_CONNECTOR_LIMIT_PER_HOST = 5
    HTTP_DNS_CACHE_SECONDS = 300
    MAX_IMAGE_BYTES = 20 * 1024 * 1024
    MAX_REDIRECTS = 5
    READ_CHUNK_BYTES = 64 * 1024
    _REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

    def __init__(self, plugin_instance: Any = None):
        self.plugin = plugin_instance
        self._aiohttp_session: aiohttp.ClientSession | None = None

    async def get_session(self) -> aiohttp.ClientSession:
        """获取或创建共享的 aiohttp session。"""
        if self._aiohttp_session is None or self._aiohttp_session.closed:
            connector = aiohttp.TCPConnector(
                limit=self.HTTP_CONNECTOR_LIMIT,
                limit_per_host=self.HTTP_CONNECTOR_LIMIT_PER_HOST,
                ttl_dns_cache=self.HTTP_DNS_CACHE_SECONDS,
                use_dns_cache=True,
                resolver=_SSRFGuardedResolver(),
            )
            timeout = aiohttp.ClientTimeout(total=self.HTTP_TIMEOUT_SECONDS)
            self._aiohttp_session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            )
        return self._aiohttp_session

    async def close(self) -> None:
        """关闭共享的 aiohttp session。"""
        if self._aiohttp_session and not self._aiohttp_session.closed:
            await self._aiohttp_session.close()
            self._aiohttp_session = None

    def build_headers(self) -> dict[str, str]:
        """返回空的下载请求头，避免向图片地址发送平台认证信息。
        """
        return {}

    @staticmethod
    def detect_file_type(content_type: str, content: bytes) -> tuple[str, bool]:
        content_type = str(content_type or "").lower()
        is_gif = "gif" in content_type or content[:6] in (b"GIF89a", b"GIF87a")
        if is_gif:
            return ".gif", True
        if "png" in content_type or content[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png", False
        if "webp" in content_type or (
            content[:4] == b"RIFF" and content[8:12] == b"WEBP"
        ):
            return ".webp", False
        if "jpeg" in content_type or "jpg" in content_type or content[:2] == b"\xff\xd8":
            return ".jpg", False
        if "bmp" in content_type or content[:2] == b"BM":
            return ".bmp", False
        return ".jpg", False

    @classmethod
    def detect_local_file_type(cls, file_path: str) -> tuple[str, bool]:
        """按本地文件魔数检测扩展名，并保留未知格式的原始后缀。"""
        with open(file_path, "rb") as handle:
            header = handle.read(12)
        ext, is_gif = cls.detect_file_type("", header)
        known = (
            header[:6] in (b"GIF89a", b"GIF87a")
            or header[:8] == b"\x89PNG\r\n\x1a\n"
            or (header[:4] == b"RIFF" and header[8:12] == b"WEBP")
            or header[:2] == b"\xff\xd8"
            or header[:2] == b"BM"
        )
        if not known:
            original_ext = os.path.splitext(file_path)[1].lower()
            if original_ext in cls.ALLOWED_IMAGE_EXTS:
                ext = original_ext
        return ext, is_gif

    async def download_to_temp(
        self,
        url: str,
        *,
        log_download: bool = False,
        max_bytes: int | None = None,
    ) -> tuple[str | None, bool]:
        """从 URL 流式下载文件到临时文件，并执行 SSRF 与单图字节上限防护。

        Returns:
            tuple[str | None, bool]: (临时文件路径, 是否为GIF动图)，失败返回 (None, False)
        """
        if not url or not isinstance(url, str):
            return None, False

        limit = int(max_bytes) if max_bytes is not None else self.MAX_IMAGE_BYTES
        current_url = url.strip()

        try:
            current_url = await validate_remote_url_async(current_url)
            session = await self.get_session()
            timeout = aiohttp.ClientTimeout(total=self.HTTP_TIMEOUT_SECONDS)
            for hop in range(self.MAX_REDIRECTS + 1):
                current_url = await validate_remote_url_async(current_url)
                async with session.get(
                    current_url,
                    headers=self.build_headers(),
                    timeout=timeout,
                    allow_redirects=False,
                ) as resp:
                    if resp.status in self._REDIRECT_STATUSES:
                        if hop >= self.MAX_REDIRECTS:
                            logger.warning(
                                f"下载图片重定向超过 {self.MAX_REDIRECTS} 次: {redact_url(current_url)}"
                            )
                            return None, False
                        location = resp.headers.get("Location", "")
                        if not location:
                            logger.warning(
                                f"下载图片重定向缺少 Location: {redact_url(current_url)}"
                            )
                            return None, False
                        current_url = await validate_remote_url_async(
                            urljoin(current_url, location)
                        )
                        continue

                    if resp.status != 200:
                        logger.warning(f"下载图片失败: HTTP {resp.status}")
                        return None, False

                    raw_len = str(resp.headers.get("Content-Length", "") or "").strip()
                    if raw_len.isdigit() and int(raw_len) > limit:
                        logger.warning(
                            f"下载图片超过单图字节上限: content_length={raw_len} > {limit}"
                        )
                        return None, False

                    content_type = resp.headers.get("Content-Type", "").lower()
                    return await self._stream_response_to_temp(
                        resp,
                        content_type=content_type,
                        limit=limit,
                        log_download=log_download,
                    )
            return None, False
        except PathPolicyError as e:
            logger.warning(f"下载图片被 SSRF 策略拒绝: {e}")
            return None, False
        except asyncio.TimeoutError:
            logger.warning("下载图片超时")
            return None, False
        except Exception as e:
            logger.warning(f"下载图片失败: {e}")
            return None, False

    async def _stream_response_to_temp(
        self,
        resp: aiohttp.ClientResponse,
        *,
        content_type: str,
        limit: int,
        log_download: bool,
    ) -> tuple[str | None, bool]:
        """流式写入临时文件，边读边校验单图字节上限。"""
        content = getattr(resp, "content", None)
        iter_chunked = getattr(content, "iter_chunked", None)

        temp_fd: int | None = None
        temp_path: str | None = None
        total = 0
        header_buf = bytearray()
        ext = ".jpg"
        is_gif = False

        def _cleanup_temp() -> None:
            nonlocal temp_fd, temp_path
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
                temp_fd = None
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                temp_path = None

        try:
            if callable(iter_chunked):
                async for chunk in iter_chunked(self.READ_CHUNK_BYTES):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        logger.warning(
                            f"下载图片流式响应超过单图字节上限: {total} > {limit}"
                        )
                        _cleanup_temp()
                        return None, False
                    if temp_fd is None:
                        header_buf.extend(chunk)
                        if len(header_buf) >= 12:
                            ext, is_gif = self.detect_file_type(
                                content_type, bytes(header_buf[:12])
                            )
                            temp_fd, temp_path = tempfile.mkstemp(suffix=ext)
                            os.write(temp_fd, header_buf)
                            header_buf.clear()
                    else:
                        os.write(temp_fd, chunk)
                if temp_fd is None:
                    ext, is_gif = self.detect_file_type(
                        content_type, bytes(header_buf[:12])
                    )
                    temp_fd, temp_path = tempfile.mkstemp(suffix=ext)
                    if header_buf:
                        os.write(temp_fd, header_buf)
                        header_buf.clear()
            else:
                raw = await resp.read()
                total = len(raw)
                if total > limit:
                    logger.warning(
                        f"下载图片响应超过单图字节上限: {total} > {limit}"
                    )
                    return None, False
                ext, is_gif = self.detect_file_type(content_type, raw[:12])
                temp_fd, temp_path = tempfile.mkstemp(suffix=ext)
                os.write(temp_fd, raw)

            if temp_fd is not None:
                os.close(temp_fd)
                temp_fd = None
            if log_download:
                logger.debug(
                    f"已下载原始图片: {temp_path} ({total} bytes, "
                    f"type={content_type}, is_gif={is_gif})"
                )
            return temp_path, is_gif
        except BaseException:
            _cleanup_temp()
            raise

    async def download_original_image(self, img: Any) -> tuple[str | None, bool]:
        """下载原始图片文件。

        优先从图片组件的本地路径复制出插件自有临时文件，
        仅对远程 HTTP URL 发起下载请求。调用方会把返回路径作为临时文件
        移动或删除，因此不能直接返回由 AstrBot 事件生命周期管理的原路径。

        Args:
            img: 图片组件

        Returns:
            tuple[str | None, bool]: (临时文件路径, 是否为GIF动图)，失败返回 (None, False)
        """
        img_url = getattr(img, "url", "") or ""
        img_file = getattr(img, "file", "") or ""
        img_path = getattr(img, "path", "") or ""

        # 检查是否已经是本地文件路径
        for candidate in (img_path, img_file, img_url):
            if candidate and os.path.exists(candidate):
                try:
                    ext, is_gif = self.detect_local_file_type(candidate)
                except OSError:
                    logger.warning(f"无法读取本地图片文件: {candidate}")
                    continue
                temp_fd, temp_path = tempfile.mkstemp(suffix=ext)
                os.close(temp_fd)
                try:
                    await asyncio.to_thread(shutil.copyfile, candidate, temp_path)
                except asyncio.CancelledError:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                    raise
                except OSError as e:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                    logger.warning(f"复制本地图片失败: {candidate}, {e}")
                    continue
                logger.debug(
                    f"已复制本地图片到插件临时文件: {candidate} -> {temp_path} "
                    f"(is_gif={is_gif})"
                )
                return temp_path, is_gif

        # 回退到 HTTP 下载
        url = img_url or img_file
        return await self.download_to_temp(url, log_download=True)

    async def download_url_to_temp(self, url: str) -> tuple[str | None, bool]:
        """从 URL 下载文件到临时文件，返回 (temp_path, is_gif)。"""
        return await self.download_to_temp(url)
