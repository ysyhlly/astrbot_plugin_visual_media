"""One AstrBot plugin for meme collection, image creation and visual expression."""
# ruff: noqa: E402

from __future__ import annotations

from .reload_bootstrap import ensure_current

ensure_current(
    __package__,
    __name__,
    __file__,
    checks=[
        ("media_library", "MediaLibrary", "_rebuild_public_ref_cache"),
        ("media_behavior", "MediaBehavior", "records"),
    ],
    version="0.5.0-rc.2",
)

import threading
import asyncio
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import (
    EventMessageType,
    PermissionType,
    PlatformAdapterType,
)
from astrbot.api.star import Context, Star
from astrbot.core.star.star import star_registry

from .runtime import ConfigSection, EngineContext
from .memes_engine import Main as MemeEngine
from .generation_engine import GeminiImageGenerationPlugin as GenerationEngine
from .media_library import MediaLibrary
from .media_api import MediaAPI
from .media_tasks import MediaTasks
from .media_runtime import MediaRuntime
from .media_receipts import image_receipt
from .tl.delivery_evidence import note_confirmed_images

# Decorators register tools at import time, before the host constructs the plugin.
# Reject a competing installation before those decorators can replace its tools.
for _existing in star_registry:
    if (
        getattr(_existing, "name", None)
        in {"astrbot_plugin_stealer", "astrbot_plugin_gemini_image_generation"}
        and getattr(_existing, "activated", False)
        and getattr(_existing, "star_cls", None) is not None
    ):
        raise RuntimeError(f"Disable {_existing.name} before loading Visual Media")


class VisualMediaPlugin(Star):
    """Own both engines, their tools, settings, data and lifecycle."""

    name = "astrbot_plugin_visual_media"
    author = "visual_media"
    plugin_id = "visual_media/astrbot_plugin_visual_media"
    SEARCH_MEME_TOOL_NAME = "search_meme"
    SEND_MEME_TOOL_NAME = "send_meme"

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        # Fail before registering competing commands or tools.
        for name in (
            "astrbot_plugin_stealer",
            "astrbot_plugin_gemini_image_generation",
        ):
            existing = context.get_registered_star(name)
            if existing is not None and getattr(existing, "activated", False):
                raise RuntimeError(f"Disable {name} before enabling Visual Media")
        self.raw_config = config
        self.engine_context = EngineContext(self)
        self.memes = None
        self.generation = None
        self._closed = False
        self._config_save_lock = threading.RLock()

    async def initialize(self):
        try:
            self.memes = MemeEngine(
                self.engine_context,
                ConfigSection(self.raw_config, "memes", self._config_save_lock),
            )
            await self.memes.initialize()
            self.generation = GenerationEngine(
                self.engine_context,
                ConfigSection(self.raw_config, "generation", self._config_save_lock),
            )
            self.library = MediaLibrary(self)
            self.behavior = await asyncio.to_thread(
                MediaRuntime,
                self.generation._plugin_data_dir,
                lambda: self.raw_config.get("media", {}),
            )
            self.memes.media_behavior = self.behavior
            self.generation.media_behavior = self.behavior
            self.tasks = await asyncio.to_thread(MediaTasks, self)
            self.media_api = MediaAPI(self)
            self.media_api.register(self.engine_context)
            from .series_panels import register_series_panels

            register_series_panels(self, "astrbot_plugin_visual_media")
            logger.info("[VisualMedia] Meme and generation engines initialized")
        except BaseException:
            await self.terminate()
            raise

    async def terminate(self):
        panel = getattr(self, "_series_panels", None)
        if panel:
            panel.close()
        if self._closed:
            return
        self._closed = True
        if getattr(self, "library", None) is not None:
            await self.library.close()
        if getattr(self, "tasks", None) is not None:
            await self.tasks.close()
        if getattr(self, "behavior", None) is not None:
            self.behavior.stop_sends = True
        # Stop generation before closing the library that may receive its output.
        try:
            for engine in (self.generation, self.memes):
                if engine is not None:
                    try:
                        await engine.terminate()
                    except Exception:
                        logger.exception("[VisualMedia] Engine shutdown failed")
        finally:
            if getattr(self, "behavior", None) is not None:
                await self.behavior.close()
            self.engine_context.release()

    def get_service(self, api_version: int = 1):
        return self.generation.get_service(api_version)

    def get_world_reference_access(self, directory):
        from .world_reference_access import status

        return status(self, directory)

    def grant_world_reference_access(self, directory):
        from .world_reference_access import grant

        return grant(self, directory)

    def add_reference_allowed_dir(self, directory: str) -> bool:
        """Persist an allowed local reference directory using public plugin locking."""
        import copy

        target = str(directory or "").strip()
        if not target:
            raise ValueError("Directory cannot be empty")
        with self._config_save_lock:
            save = getattr(self.raw_config, "save_config", None)
            if not callable(save):
                raise ValueError("Host configuration cannot be saved")
            generation = self.raw_config.setdefault("generation", {})
            previous = copy.deepcopy(generation.get("image_generation_settings", {}))
            updated = copy.deepcopy(previous)
            dirs = list(
                dict.fromkeys(
                    [*updated.get("llm_tool_reference_allowed_dirs", []), target]
                )
            )
            updated["llm_tool_reference_allowed_dirs"] = dirs
            try:
                generation["image_generation_settings"] = updated
                save()
            except Exception:
                generation["image_generation_settings"] = previous
                raise
            if getattr(self, "generation", None) is not None:
                self.generation.cfg.llm_tool_reference_allowed_dirs = list(dirs)
            return True

    def get_dynamics_summary(self, umo: str = "") -> dict[str, Any]:
        """Return non-secret media summary for companion plugins without exposing internals."""
        target_umo = str(umo or "").strip()
        characters: list[dict[str, str]] = []
        lib = getattr(self, "library", None)
        if lib is not None and callable(getattr(lib, "profiles", None)):
            characters = [
                {
                    "key": str(p.get("key") or ""),
                    "name": str(p.get("name") or ""),
                    "appearance": str(p.get("appearance") or ""),
                    "style": str(p.get("style") or ""),
                }
                for p in lib.profiles()
            ]
        elif getattr(self, "memes", None) is not None:
            cfg = getattr(self.memes, "plugin_config", None)
            if cfg is not None and callable(getattr(cfg, "get_characters", None)):
                info = (
                    cfg.get_character_info()
                    if callable(getattr(cfg, "get_character_info", None))
                    else {}
                )
                for key in cfg.get_characters():
                    meta = info.get(key) if isinstance(info, dict) else {}
                    characters.append(
                        {
                            "key": str(key),
                            "name": str((meta or {}).get("name") or key),
                            "appearance": str((meta or {}).get("desc") or ""),
                            "style": "",
                        }
                    )
        running = 0
        recent = 0
        latest_output_path = ""
        latest_prompt = ""
        tracker = getattr(getattr(self, "generation", None), "generation_tracker", None)
        if tracker is not None and callable(getattr(tracker, "records_snapshot", None)):
            rows = tracker.records_snapshot()
            for row in rows:
                req_umo = str((row.get("requester") or {}).get("umo") or "")
                if target_umo and req_umo and req_umo != target_umo:
                    continue
                if row.get("status") in {"queued", "running"}:
                    running += 1
                else:
                    recent += 1
                    if not latest_prompt:
                        latest_prompt = str(row.get("prompt") or "")[:200]
                    imgs = row.get("images") or []
                    if (
                        not latest_output_path
                        and imgs
                        and callable(getattr(tracker, "gallery_image_path", None))
                    ):
                        resolved = tracker.gallery_image_path(imgs[0])
                        if resolved is not None:
                            latest_output_path = str(resolved)
        return {
            "schema_version": 1,
            "ready": not self._closed
            and self.memes is not None
            and self.generation is not None,
            "characters": characters,
            "running": running,
            "recent": recent,
            "latest_output_path": latest_output_path,
            "latest_prompt": latest_prompt,
        }

    def get_dynamics_capabilities(self):
        """Declare the current media owner without exposing configuration or secrets."""
        return {
            "schema_version": 1,
            "display_name": "视觉工坊",
            "enabled": not self._closed,
            "ready": not self._closed
            and self.memes is not None
            and self.generation is not None,
            "capabilities": {"media": 1},
            "media_policy_version": 1,
        }

    def start_joint_reply_prefetch(self, event):
        if event.get_extra("visual_media_generation_requested", False):
            return
        return self.memes.start_joint_reply_prefetch(event)

    @filter.llm_tool(name="list_visual_characters")
    async def list_visual_characters(self, event: AstrMessageEvent):
        """List saved character keys and appearance descriptions for image generation."""
        return [
            {key: profile[key] for key in ("key", "name", "appearance", "style")}
            for profile in self.library.profiles()
        ]

    @filter.llm_tool(name="save_generated_meme")
    async def save_generated_meme(
        self,
        event: AstrMessageEvent,
        generation_id: str,
        image_index: int = 0,
        character: str = "",
        description: str = "",
    ):
        """Save a generated image as a reusable meme in this conversation.

        Args:
            generation_id (string): Generation reference ID, optionally prefixed with gen:.
            image_index (number): Zero-based image index in the generation result.
            character (string): Optional existing character key.
            description (string): Short description used to find the meme later.
        """
        try:
            image = await self.library.save_generation(
                generation_id,
                image_index,
                character=character,
                description=description,
                event=event,
            )
            return {"saved": True, "hash": image["hash"], "scope": "local"}
        except (ValueError, PermissionError) as exc:
            return {"saved": False, "error": str(exc)}

    @filter.llm_tool(name="generate_character_image")
    async def generate_character_image(
        self, event: AstrMessageEvent, character: str, prompt: str
    ):
        """Generate an image using a character's saved appearance and reference images.

        Args:
            character (string): Existing character key from the visual library.
            prompt (string): Desired scene, action or expression.
        """
        return await self.library.generate_character(character, prompt, event)

    @filter.command("视觉帮助")
    async def visual_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "视觉工坊：/meme menu 管理表情；/生图、/改图、/快速 表情包、/切图；管理面板可保存生成图为表情，并设置角色参考形象。"
        )

    # Original command and event contracts are forwarded to the internal engines.
    @filter.command_group("meme")
    def meme(self):
        """表情包管理指令，功能面板入口：/meme menu"""
        return self.memes.meme()

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("on")
    async def meme_on(self, event: AstrMessageEvent):
        """开启表情包偷取功能，自动收集群聊中的表情包。"""
        async for result in self.memes.meme_on(event):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("off")
    async def meme_off(self, event: AstrMessageEvent):
        """关闭表情包偷取功能，停止收集新表情包。"""
        async for result in self.memes.meme_off(event):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("auto_on")
    async def auto_on(self, event: AstrMessageEvent):
        """开启自动发送表情包，聊天时根据情绪自动发送。"""
        async for result in self.memes.auto_on(event):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("auto_off")
    async def auto_off(self, event: AstrMessageEvent):
        """关闭自动发送表情包。"""
        async for result in self.memes.auto_off(event):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("group")
    async def group_filter(
        self,
        event: AstrMessageEvent,
        scope: str = "",
        list_name: str = "",
        action: str = "",
        target: str = "",
        target_id: str = "",
    ):
        """管理发送与收集目标名单。用法: /meme group <send|steal> <wl|bl> <add|del|clear|show> [目标]"""
        async for result in self.memes.group_filter(
            event, scope, list_name, action, target, target_id
        ):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("偷")
    async def capture(self, event: AstrMessageEvent):
        """进入强制接收模式，30秒内发送的图片将直接入库。"""
        async for result in self.memes.capture(event):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("natural_analysis")
    async def toggle_natural_analysis(self, event: AstrMessageEvent, action: str = ""):
        """切换情绪识别模式。用法: /meme natural_analysis <on|off>"""
        async for result in self.memes.toggle_natural_analysis(event, action):
            yield result

    @meme.command("emotion_stats")
    async def emotion_analysis_stats(self, event: AstrMessageEvent):
        """查看情绪分析统计信息和当前模式。"""
        async for result in self.memes.emotion_analysis_stats(event):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("clear_emotion_cache")
    async def clear_emotion_cache(self, event: AstrMessageEvent):
        """清空情绪分析缓存，释放内存。"""
        async for result in self.memes.clear_emotion_cache(event):
            yield result

    @meme.command("status")
    async def status(self, event: AstrMessageEvent):
        """查看插件运行状态和表情包统计信息。"""
        async for result in self.memes.status(event):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("tag_stats")
    async def tag_stats(self, event: AstrMessageEvent, limit: str = ""):
        """标签/场景统计：高频标签、低频噪声标签、零标签条目（打标质量体检）。用法: /meme tag_stats [N]"""
        async for result in self.memes.tag_stats(event, limit):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("clean", priority=-100)
    async def clean(self, event: AstrMessageEvent, mode: str = ""):
        """清理原始图片缓存（不影响已分类的表情包）。"""
        async for result in self.memes.clean(event, mode):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("capacity")
    async def enforce_capacity(self, event: AstrMessageEvent):
        """立即执行容量控制，清理超出上限的旧表情包。"""
        async for result in self.memes.enforce_capacity(event):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("list")
    async def list_images(
        self,
        event: AstrMessageEvent,
        category: str = "",
        limit: str = "10",
        page: str = "1",
    ):
        """列出已收集的表情包。用法: /meme list [分类] [每页数量] [页码]"""
        async for result in self.memes.list_images(event, category, limit, page):
            yield result

    @filter.on_platform_loaded()
    async def register_menu_interactions(self):
        """在 QQ 客户端连接前注册面板互动事件。"""
        return await self.memes.register_menu_interactions()

    @meme.command("menu", alias={"help"})
    async def menu_panel(self, event: AstrMessageEvent, section: str = "overview"):
        """查看功能面板。用法: /meme menu [overview|settings|maintenance]"""
        return await self.memes.menu_panel(event, section)

    @meme.command("random")
    async def random_menu_image(self, event: AstrMessageEvent, token: str = ""):
        """随机试发当前会话可用的表情。用法: /meme random"""
        return await self.memes.random_menu_image(event, token)

    @meme.command("close")
    async def close_menu(self, event: AstrMessageEvent, token: str = ""):
        """使当前面板按钮失效。用法: /meme close <面板码>"""
        return await self.memes.close_menu(event, token)

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("delete")
    async def delete_image(self, event: AstrMessageEvent, identifier: str = ""):
        """删除指定表情包。用法: /meme delete <序号|文件名>"""
        async for result in self.memes.delete_image(event, identifier):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("blacklist")
    async def blacklist_image(self, event: AstrMessageEvent, identifier: str = ""):
        """拉黑指定表情包。用法: /meme blacklist <序号|文件名>"""
        async for result in self.memes.blacklist_image(event, identifier):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("scope")
    async def set_image_scope(
        self, event: AstrMessageEvent, identifier: str = "", scope_mode: str = ""
    ):
        """设置表情包作用域。用法: /meme scope <序号|文件名> <public|local>"""
        async for result in self.memes.set_image_scope(event, identifier, scope_mode):
            yield result

    @filter.permission_type(PermissionType.ADMIN)
    @meme.command("rebuild_index")
    async def rebuild_index(self, event: AstrMessageEvent):
        """重建表情包索引，用于修复索引异常或版本迁移。"""
        async for result in self.memes.rebuild_index(event):
            yield result

    @filter.llm_tool(name=SEARCH_MEME_TOOL_NAME)
    async def search_meme(
        self, event: AstrMessageEvent, query: str, reply: str = "", character: str = ""
    ):
        """搜索表情包并返回编号、图上文字、角色和适合回应的场景。

        聊天中想表达情绪、接梗或自然回应时可主动使用。
        表情可单独回应，也可配合文字；按语境和交流节奏使用。
        核对候选与回复立场，合适时调用 send_meme(emoji_id=编号)。编号仅本次搜索有效。
        候选不合适可补充台词重搜或用文字回复。通过 search_meme 选图，再用 send_meme 发送候选编号。

        Args:
            query(string): 想表达的台词、语气或使用场景，支持自然句，例如“安慰刚加班的朋友”。
            reply(string): 可选，准备说的文字；单独用表情回应时可留空。
            character(string): 可选，指定角色键或显示名；只在该角色中找图。"""
        async for result in self.memes.search_meme(event, query, reply, character):
            yield result

    @filter.llm_tool(name=SEND_MEME_TOOL_NAME)
    async def send_meme(self, event: AstrMessageEvent, emoji_id: int):
        """按本次 search_meme 返回的候选编号发送表情包。

        选择原则：核对图上文字和适用场景，保持与当前回复的立场一致；候选不合适时可以不发送。

        Args:
            emoji_id(number): 表情包编号（从 search_meme 返回的候选列表中选择）"""
        async for result in self.memes.send_meme(event, emoji_id):
            yield result

    @filter.llm_tool(name="steal_meme")
    async def steal_sticker(self, event: AstrMessageEvent, image_ref: str):
        """偷取图片入库。VLM 视觉模型会自动分析图片，打上分类、标签、描述和场景。

        使用时机：
        - 用户说"偷一下"/"收了这张图"时直接调用本工具。
        - 你看到当前消息里有适合作为表情包的图片时，也可以调用本工具补充素材库。

        注意：
        - image_ref 必须从当前消息中已有的图片 URL 或文件路径中选择，必填。
        - 工具返回的 VLM 分析结果可用于向用户说明偷到了什么。
        - 当插件的表情包偷取总开关关闭，或当前会话被偷取黑白名单禁用时，本工具会拒绝入库。

        Args:
            image_ref(string): 图片 URL 或文件路径，从当前消息已有的 Image URL 中选择。"""
        async for result in self.memes.steal_sticker(event, image_ref):
            yield result

    @filter.event_message_type(EventMessageType.ALL)
    @filter.platform_adapter_type(PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息监听：偷取消息中的图片并分类存储。"""
        return await self.memes.on_message(event)

    @filter.on_llm_tool_respond()
    async def _track_external_image_delivery(
        self, event: AstrMessageEvent, tool, tool_args: dict | None, tool_result
    ) -> None:
        """记录 LLM 通过 AstrBot 通用消息工具发送的图片，避免同轮再被动发表情。"""
        if str(getattr(tool, "name", "")) == "send_message_to_user":
            self.behavior.bind(event)
            receipt = image_receipt(event, tool_args, tool_result)
            if receipt:
                matched = (
                    note_confirmed_images(
                        event, receipt["images"], same_session=receipt["same_session"]
                    )
                    if receipt["confirmed"]
                    else 0
                )
                mode = (
                    "generate"
                    if getattr(event, "_visual_generation_task", None)
                    or event.get_extra("visual_media_generation_requested", False)
                    else "image"
                )
                if (
                    getattr(event, "_visual_expected_image_keys", None) is not None
                    and not matched
                ):
                    mode = "image"
                destination = receipt["event"]
                task_id = (
                    (getattr(event, "_visual_generation_task", "") or "")
                    if mode == "generate"
                    else ""
                )
                await self.behavior.arecord(
                    destination,
                    mode,
                    receipt["phase"],
                    receipt["reason"],
                    {
                        "image_count": len(receipt["images"]),
                        "matched_image_count": matched,
                        "task_id": task_id,
                        "same_session": receipt["same_session"],
                    },
                )
                if receipt["confirmed"]:
                    index = self.memes.plugin_api._get_index()
                    for item in receipt["images"]:
                        path = str(item.get("path", "") or "")
                        if path in index:
                            await self.behavior.adelivered(
                                destination, {**index[path], "path": path}
                            )
                # Sending elsewhere must not consume this conversation's media turn.
                if not receipt["same_session"] or not receipt["confirmed"]:
                    return
        return await self.memes._track_external_image_delivery(
            event, tool, tool_args, tool_result
        )

    @filter.on_llm_request(priority=5)
    async def _start_joint_meme_prefetch(self, event: AstrMessageEvent, req):
        if event.get_extra("_narrative_internal_v1", False) or event.get_extra(
            "visual_media_generation_requested", False
        ):
            return
        return await self.memes._start_joint_meme_prefetch(event, req)

    @filter.on_llm_request(priority=1)
    async def _prepare_joint_meme_reply(self, event: AstrMessageEvent, req):
        if event.get_extra("_narrative_internal_v1", False):
            return
        attach_prompt = not getattr(req, "_visual_preferences_attached", False)
        extra_prompt = await asyncio.to_thread(
            self.behavior.remember_and_prompt, event, attach_prompt
        )
        if attach_prompt:
            if extra_prompt:
                req.system_prompt = (
                    str(getattr(req, "system_prompt", "") or "") + extra_prompt
                )
            req._visual_preferences_attached = True
        if event.get_extra("visual_media_generation_requested", False):
            return
        return await self.memes._prepare_joint_meme_reply(event, req)

    @filter.on_llm_response()
    async def _prepare_emoji_after_llm(self, event: AstrMessageEvent, response: Any):
        """Capture final LLM text before the streaming result is decorated.

        AstrBot skips ``on_decorating_result`` while a result is still a
        streaming result.  The LLM response hook runs after the agent has its
        final response and gives passive emoji scheduling a stable entry point."""
        if event.get_extra("visual_media_generation_requested", False):
            return
        return await self.memes._prepare_emoji_after_llm(event, response)

    @filter.on_decorating_result(priority=100)
    async def _prepare_emoji_response(self, event: AstrMessageEvent):
        """LLM 回复完成后异步发送表情包（不阻塞回复）。"""
        if event.get_extra("visual_media_generation_requested", False):
            return
        return await self.memes._prepare_emoji_response(event)

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 完成初始化后加载插件 API 配置"""
        return await self.generation.on_astrbot_loaded()

    @filter.command("生图")
    async def generate_image(self, event: AstrMessageEvent, prompt: str):
        """生图指令"""
        async for result in self.generation.generate_image(event, prompt):
            yield result

    @filter.command_group("快速")
    def quick_mode_group(self):
        """快速模式指令组"""
        return self.generation.quick_mode_group()

    @quick_mode_group.command("头像")
    async def quick_avatar(self, event: AstrMessageEvent, prompt: str = ""):
        """头像快速模式 - 1K分辨率，1:1比例"""
        async for result in self.generation.quick_avatar(event, prompt):
            yield result

    @quick_mode_group.command("海报")
    async def quick_poster(self, event: AstrMessageEvent, prompt: str = ""):
        """海报快速模式 - 2K分辨率，16:9比例"""
        async for result in self.generation.quick_poster(event, prompt):
            yield result

    @quick_mode_group.command("壁纸")
    async def quick_wallpaper(self, event: AstrMessageEvent, prompt: str = ""):
        """壁纸快速模式 - 4K分辨率，16:9比例"""
        async for result in self.generation.quick_wallpaper(event, prompt):
            yield result

    @quick_mode_group.command("卡片")
    async def quick_card(self, event: AstrMessageEvent, prompt: str = ""):
        """卡片快速模式 - 1K分辨率，3:2比例"""
        async for result in self.generation.quick_card(event, prompt):
            yield result

    @quick_mode_group.command("手机")
    async def quick_mobile(self, event: AstrMessageEvent, prompt: str = ""):
        """手机快速模式 - 2K分辨率，9:16比例"""
        async for result in self.generation.quick_mobile(event, prompt):
            yield result

    @quick_mode_group.command("手办化")
    async def quick_figure(self, event: AstrMessageEvent, prompt: str = ""):
        """手办化快速模式 - 树脂收藏级手办效果"""
        async for result in self.generation.quick_figure(event, prompt):
            yield result

    @quick_mode_group.command("表情包")
    async def quick_sticker(self, event: AstrMessageEvent, prompt: str = ""):
        """表情包快速模式 - 4K分辨率，16:9比例，Q版LINE风格

        功能受配置文件控制：
        - enable_sticker_split: 是否自动切割图片
        - enable_sticker_zip: 是否打包发送（如果发送失败则使用合并转发）"""
        async for result in self.generation.quick_sticker(event, prompt):
            yield result

    @filter.command("切图")
    async def split_image_command(
        self, event: AstrMessageEvent, grid: str | None = None
    ):
        """对消息中的图片进行切割"""
        async for result in self.generation.split_image_command(event, grid):
            yield result

    @filter.command("生图帮助")
    async def show_help(self, event: AstrMessageEvent):
        """显示插件使用帮助"""
        async for result in self.generation.show_help(event):
            yield result

    @filter.command("改图")
    async def modify_image(self, event: AstrMessageEvent, prompt: str):
        """根据提示词修改或重做图像"""
        async for result in self.generation.modify_image(event, prompt):
            yield result

    @filter.command("换风格")
    async def change_style(self, event: AstrMessageEvent, style: str, prompt: str = ""):
        """改变图像风格"""
        async for result in self.generation.change_style(event, style, prompt):
            yield result
