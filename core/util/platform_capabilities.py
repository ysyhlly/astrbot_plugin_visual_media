"""Unified cross-platform capability matrix & graceful degradation helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PlatformCapabilities:
    """Normalized media/interaction capabilities for a chat platform adapter."""

    platform_family: str
    raw_name: str
    supports_qq_sticker: bool = False
    supports_telegram_sticker: bool = False
    supports_forward_node: bool = False
    supports_avatar_lookup: bool = False
    supports_interactive_buttons: bool = False
    supports_direct_file_image: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def degradation_hint(self, feature: str) -> str:
        """Return a concise, human-friendly fallback notice for unsupported features."""
        name_map = {
            "onebot": "QQ (OneBot)",
            "qq_official": "QQ 官方机器人",
            "discord": "Discord",
            "telegram": "Telegram",
            "wechat": "微信",
            "feishu": "飞书",
            "dingtalk": "钉钉",
            "generic": "当前平台",
        }
        label = name_map.get(self.platform_family, self.raw_name or "当前平台")
        if feature == "qq_sticker" and not self.supports_qq_sticker:
            return f"{label}不支持 QQ 自定义表情格式，已自动降级为普通图片发送。"
        if feature == "forward_node" and not self.supports_forward_node:
            return f"{label}不支持合并转发节点，已自动改为分条直接发送。"
        if feature == "avatar_lookup" and not self.supports_avatar_lookup:
            return f"{label}暂不支持按用户 ID 直接抓取头像，将仅依据文案/引用图片生成。"
        if feature == "interactive_buttons" and not self.supports_interactive_buttons:
            return f"{label}不支持原生交互按钮，请直接回复文本指令完成操作。"
        return ""


_FAMILY_PROFILES: dict[str, dict[str, Any]] = {
    "onebot": {
        "supports_qq_sticker": True,
        "supports_telegram_sticker": False,
        "supports_forward_node": True,
        "supports_avatar_lookup": True,
        "supports_interactive_buttons": False,
        "supports_direct_file_image": False,
    },
    "qq_official": {
        "supports_qq_sticker": False,
        "supports_telegram_sticker": False,
        "supports_forward_node": False,
        "supports_avatar_lookup": False,
        "supports_interactive_buttons": True,
        "supports_direct_file_image": True,
    },
    "discord": {
        "supports_qq_sticker": False,
        "supports_telegram_sticker": False,
        "supports_forward_node": False,
        "supports_avatar_lookup": True,
        "supports_interactive_buttons": True,
        "supports_direct_file_image": True,
    },
    "telegram": {
        "supports_qq_sticker": False,
        "supports_telegram_sticker": True,
        "supports_forward_node": False,
        "supports_avatar_lookup": False,
        "supports_interactive_buttons": True,
        "supports_direct_file_image": True,
    },
    "wechat": {
        "supports_qq_sticker": False,
        "supports_telegram_sticker": False,
        "supports_forward_node": False,
        "supports_avatar_lookup": False,
        "supports_interactive_buttons": False,
        "supports_direct_file_image": True,
    },
    "feishu": {
        "supports_qq_sticker": False,
        "supports_telegram_sticker": False,
        "supports_forward_node": False,
        "supports_avatar_lookup": False,
        "supports_interactive_buttons": True,
        "supports_direct_file_image": True,
    },
    "dingtalk": {
        "supports_qq_sticker": False,
        "supports_telegram_sticker": False,
        "supports_forward_node": False,
        "supports_avatar_lookup": False,
        "supports_interactive_buttons": False,
        "supports_direct_file_image": True,
    },
    "generic": {
        "supports_qq_sticker": False,
        "supports_telegram_sticker": False,
        "supports_forward_node": False,
        "supports_avatar_lookup": False,
        "supports_interactive_buttons": False,
        "supports_direct_file_image": True,
    },
}


def resolve_platform_name(event_or_name: Any) -> str:
    """Extract normalized lowercase platform adapter name from an event or string."""
    if isinstance(event_or_name, str):
        return event_or_name.strip().lower()
    if event_or_name is None:
        return ""
    getter = getattr(event_or_name, "get_platform_name", None)
    if callable(getter):
        try:
            name = str(getter() or "").strip().lower()
            if name:
                return name
        except Exception:
            pass
    meta = getattr(event_or_name, "platform_meta", None)
    if meta is not None:
        name = str(getattr(meta, "name", "") or "").strip().lower()
        if name:
            return name
    umo = str(getattr(event_or_name, "unified_msg_origin", "") or "").strip()
    if ":" in umo:
        return umo.split(":", 1)[0].strip().lower()
    return ""


def classify_platform_family(raw_name: str) -> str:
    """Map an adapter name / UMO prefix onto a canonical platform family."""
    name = str(raw_name or "").strip().lower()
    if not name:
        return "generic"
    if any(tok in name for tok in ("qq_official", "qqofficial", "qq-official")):
        return "qq_official"
    if any(tok in name for tok in ("aiocqhttp", "onebot", "napcat", "lagrange", "gocq")):
        return "onebot"
    if "discord" in name:
        return "discord"
    if "telegram" in name or name == "tg":
        return "telegram"
    if any(tok in name for tok in ("wechat", "gewechat", "wx", "wecom", "wxpad", "itchat")):
        return "wechat"
    if any(tok in name for tok in ("lark", "feishu")):
        return "feishu"
    if "dingtalk" in name:
        return "dingtalk"
    return "generic"


def get_platform_capabilities(event_or_name: Any) -> PlatformCapabilities:
    """Return the normalized capability matrix for ``event_or_name``."""
    raw = resolve_platform_name(event_or_name)
    family = classify_platform_family(raw)
    profile = _FAMILY_PROFILES.get(family, _FAMILY_PROFILES["generic"])
    return PlatformCapabilities(
        platform_family=family,
        raw_name=raw or family,
        **profile,
    )


def list_platform_capability_matrix() -> dict[str, dict[str, Any]]:
    """Return the complete capability matrix (useful for diagnostics/UI)."""
    return {
        family: PlatformCapabilities(
            platform_family=family, raw_name=family, **profile
        ).to_dict()
        for family, profile in _FAMILY_PROFILES.items()
    }
