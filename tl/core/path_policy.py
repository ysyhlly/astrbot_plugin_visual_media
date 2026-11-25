"""参考图来源校验：本地根目录约束与远端 URL SSRF 防护。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .errors import PathPolicyError, redact_url

_REMOTE_URL_SCHEMES = frozenset({"http", "https"})
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.goog",
        "metadata.google.internal",
        "instance-data",
        "host.docker.internal",
        "gateway.docker.internal",
    }
)
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
)


def _parse_int_label(label: str) -> int | None:
    if not label:
        return None
    low = label.lower()
    try:
        if low.startswith("0x"):
            if len(low) <= 2:
                return None
            val = int(low, 16)
        elif len(low) > 1 and low.startswith("0"):
            if any(ch not in "01234567" for ch in low):
                return None
            val = int(low, 8)
        elif low.isdigit():
            val = int(low, 10)
        else:
            return None
    except ValueError:
        return None
    return val if val >= 0 else None


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """解析标准或非标准数值形态 IP 字面量（含十进制/十六进制/八进制/简写 IPv4）。"""
    cleaned = host.strip().strip("[]")
    if not cleaned:
        return None
    # IPv6 zone id (如 fe80::1%eth0)
    if "%" in cleaned:
        cleaned = cleaned.split("%", 1)[0]
    try:
        return ipaddress.ip_address(cleaned)
    except ValueError:
        pass

    parts = cleaned.split(".")
    if 1 <= len(parts) <= 4:
        nums = [_parse_int_label(p) for p in parts]
        if all(n is not None for n in nums):
            vals = [int(n) for n in nums if n is not None]
            if len(vals) == 1 and vals[0] <= 0xFFFFFFFF:
                return ipaddress.IPv4Address(vals[0])
            if len(vals) == 2 and vals[0] <= 0xFF and vals[1] <= 0xFFFFFF:
                return ipaddress.IPv4Address((vals[0] << 24) | vals[1])
            if (
                len(vals) == 3
                and vals[0] <= 0xFF
                and vals[1] <= 0xFF
                and vals[2] <= 0xFFFF
            ):
                return ipaddress.IPv4Address(
                    (vals[0] << 24) | (vals[1] << 16) | vals[2]
                )
            if len(vals) == 4 and all(v <= 0xFF for v in vals):
                return ipaddress.IPv4Address(
                    (vals[0] << 24) | (vals[1] << 16) | (vals[2] << 8) | vals[3]
                )
    return None


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断 IP 是否属于内网/回环/链路本地/云元数据/多播/保留/未指定地址。"""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_blocked_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None and is_blocked_ip(ip.sixtofour):
            return True
        if ip.teredo is not None:
            server_ip, client_ip = ip.teredo
            if is_blocked_ip(server_ip) or is_blocked_ip(client_ip):
                return True
    return bool(
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_blocked_hostname(host: str) -> bool:
    normalized = host.strip().rstrip(".").lower()
    if not normalized:
        return True
    if normalized in _BLOCKED_HOSTNAMES:
        return True
    return any(normalized.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES)


def check_remote_url(url: str, *, allow_private: bool = False) -> str:
    """校验 URL 可作为远端抓取目标，阻断内网/回环/链路本地/云元数据地址。"""

    text = str(url or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in _REMOTE_URL_SCHEMES or not parsed.hostname:
        raise PathPolicyError(f"仅允许 http/https URL: {redact_url(text)}")
    if parsed.username is not None or parsed.password is not None:
        raise PathPolicyError(f"出站 URL 不允许携带用户信息: {redact_url(text)}")

    if not allow_private:
        raw_host = parsed.hostname
        if "%" in raw_host:
            raise PathPolicyError(
                f"出站 URL 不允许携带作用域 IPv6 地址: {redact_url(text)}"
            )
        if _is_blocked_hostname(raw_host):
            raise PathPolicyError(f"出站 URL 命中受限主机名: {redact_url(text)}")
        ip_obj = _parse_ip_literal(raw_host)
        if ip_obj is not None and is_blocked_ip(ip_obj):
            raise PathPolicyError(
                f"出站 URL 命中受限内网/元数据地址: {redact_url(text)}"
            )
    return text


async def validate_remote_url_async(
    url: str,
    *,
    allow_private: bool = False,
    resolve_dns: bool = True,
) -> str:
    """异步校验远端 URL：先做静态 SSRF 校验，再异步解析 DNS 阻断指向内网的域名。"""

    cleaned = check_remote_url(url, allow_private=allow_private)
    if allow_private or not resolve_dns:
        return cleaned

    parsed = urlsplit(cleaned)
    host = (parsed.hostname or "").strip().rstrip(".")
    if _parse_ip_literal(host) is not None:
        return cleaned

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return cleaned

    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_text = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if is_blocked_ip(ip_obj):
            raise PathPolicyError(
                f"出站 URL 解析到受限内网/元数据地址: {redact_url(cleaned)}"
            )
    return cleaned


class PathPolicy:
    """校验本地路径落在允许根内。"""

    def __init__(self, allowed_local_roots: tuple[Path, ...]) -> None:
        self.allowed_local_roots = tuple(
            Path(root).expanduser().resolve() for root in allowed_local_roots
        )

    def validate_local_path(self, path: str | Path) -> Path:
        """校验 file:// URI 或裸绝对路径，返回 resolve 后的路径。"""

        text = str(path or "").strip()
        if not text:
            raise PathPolicyError("本地路径为空")
        if text.lower().startswith("file:"):
            candidate = self._path_from_file_uri(text)
        else:
            candidate = Path(text)
        if not candidate.is_absolute():
            raise PathPolicyError(f"本地路径必须是绝对路径: {text}")
        # resolve 展开符号链接与 ..，之后再次校验以阻止逃逸。
        resolved = candidate.resolve()
        self._ensure_within_roots(resolved)
        return resolved

    def _path_from_file_uri(self, text: str) -> Path:
        parsed = urlsplit(text)
        if parsed.scheme.lower() != "file":
            raise PathPolicyError(f"仅支持 file:// URI: {text}")
        if parsed.query or parsed.fragment:
            raise PathPolicyError(f"file:// URI 不允许携带 query/fragment: {text}")
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        if netloc not in ("", "localhost"):
            raise PathPolicyError(f"file:// URI 不允许远端 host: {text}")
        return Path(url2pathname(parsed.path))

    def _ensure_within_roots(self, resolved: Path) -> None:
        for root in self.allowed_local_roots:
            if resolved.is_relative_to(root):
                return
        raise PathPolicyError(f"本地路径越出允许目录: {resolved}")
