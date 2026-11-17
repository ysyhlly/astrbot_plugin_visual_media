"""平台相关的表情包投递优化。"""

import os
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image


async def send_qq_image_as_sticker(
    event: AstrMessageEvent,
    file_path: str,
    summary: str = "[动画表情]",
    plugin: Any = None,
    *,
    before_dispatch: Callable[[], Awaitable[bool]] | None = None,
) -> bool | None:
    """Send a custom QQ sticker, or return None if dispatch became stale."""
    if not getattr(plugin, "send_meme_as_qq_sticker", True):
        return False
    try:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )
        from aiocqhttp.exceptions import ActionFailed
    except ImportError:
        return False
    if not isinstance(event, AiocqhttpMessageEvent):
        return False
    if not file_path or not os.path.exists(file_path):
        return False

    # 这里使用适配器内部接口做 QQ 专属优化；任何失败都交给调用方的标准
    # event.send 路径回退，避免平台版本变化中断回复。
    dispatch_started = False
    try:
        file_source: str = file_path
        if plugin and getattr(plugin, "send_meme_as_gif", False):
            renderer = getattr(plugin, "image_render_service", None)
            if renderer:
                encoded = await renderer.file_to_gif_base64(file_path)
                if encoded:
                    file_source = f"base64://{encoded}"
        chain = MessageChain([Image(file=file_source)])
        onebot_message = await event._parse_onebot_json(chain)
        onebot_message[0]["data"]["summary"] = summary
        # NapCat: 0 为普通图片，1 为自定义表情；summary 只修改摘要。
        onebot_message[0]["data"]["sub_type"] = 1
        if before_dispatch is not None and not await before_dispatch():
            return None
        dispatch_started = True
        await event.bot.send(event.message_obj.raw_message, onebot_message)
        return True
    except Exception as e:
        if dispatch_started and not isinstance(e, ActionFailed):
            # A timeout/connection loss cannot prove that QQ rejected the first
            # image. Do not turn an uncertain send into a second image attempt.
            logger.warning("[Stealer] QQ 表情发送结果不确定，不重复发送: %s", type(e).__name__)
            raise
        logger.warning(f"[Stealer] QQ 表情发送失败，回退为普通图片: {e}")
        return False
