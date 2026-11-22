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
    version="0.5.0-rc.8",
)

import asyncio
import threading
from datetime import date as _date_cls
from datetime import datetime, timezone
from types import SimpleNamespace
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

from .core.tools.joint_reply import joint_reply_owned
from .generation_engine import GeminiImageGenerationPlugin as GenerationEngine
from .media_api import MediaAPI
from .media_library import MediaLibrary
from .media_receipts import image_receipt
from .media_runtime import MediaRuntime
from .media_tasks import MediaTasks
from .memes_engine import Main as MemeEngine
from .runtime import ConfigSection, EngineContext
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
    author = "ysyhlly"
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

    @staticmethod
    def _parse_series_ts(value: Any) -> datetime | None:
        """Parse an ISO-8601 string or numeric unix timestamp into an aware UTC datetime."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if value <= 0:
                return None
            try:
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _extract_group_id_from_umo(umo: str) -> str:
        clean = str(umo or "").strip()
        if not clean:
            return ""
        parts = clean.split(":", 2)
        if len(parts) == 3 and "group" in parts[1].lower():
            return parts[2].strip()
        return ""

    @staticmethod
    def _origin_matches_scope(origin_target: Any, target_scope: str) -> bool:
        """Match a meme's ``origin_target`` (``umo:...`` or legacy ``group:/user:``) against ``target_scope``."""
        from .core.config.config import PluginConfig

        return PluginConfig.origin_target_matches_scope(origin_target, target_scope)

    def _safe_tracker_rows(self) -> list[dict[str, Any]]:
        try:
            tracker = getattr(
                getattr(self, "generation", None), "generation_tracker", None
            )
            if tracker is None or not callable(
                getattr(tracker, "records_snapshot", None)
            ):
                return []
            rows = tracker.records_snapshot()
            return [r for r in rows if isinstance(r, dict)] if rows else []
        except Exception:
            return []

    def _safe_meme_index(self) -> dict[str, dict[str, Any]]:
        try:
            memes = getattr(self, "memes", None)
            if memes is None:
                return {}
            api = getattr(memes, "plugin_api", None)
            if api is not None and callable(getattr(api, "_get_index", None)):
                idx = api._get_index()
                if isinstance(idx, dict):
                    return idx
            db = getattr(memes, "db_service", None)
            if db is not None and callable(
                getattr(db, "get_index_cache_readonly", None)
            ):
                idx = db.get_index_cache_readonly()
                if isinstance(idx, dict):
                    return idx
        except Exception:
            pass
        return {}

    def _safe_character_profiles(self) -> list[dict[str, Any]]:
        try:
            lib = getattr(self, "library", None)
            if lib is not None and callable(getattr(lib, "profiles", None)):
                return [p for p in lib.profiles() if isinstance(p, dict)]
        except Exception:
            pass
        try:
            memes = getattr(self, "memes", None)
            cfg = getattr(memes, "plugin_config", None)
            if cfg is not None and callable(getattr(cfg, "get_characters", None)):
                info = (
                    cfg.get_character_info()
                    if callable(getattr(cfg, "get_character_info", None))
                    else {}
                )
                out = []
                for key in cfg.get_characters():
                    meta = info.get(key) if isinstance(info, dict) else {}
                    out.append(
                        {
                            "key": str(key),
                            "name": str((meta or {}).get("name") or key),
                            "appearance": str((meta or {}).get("desc") or ""),
                            "style": "",
                            "references": [],
                        }
                    )
                return out
        except Exception:
            pass
        return []

    def _series_panels_mod(self):
        try:
            if __package__:
                from . import series_panels

                return series_panels
            import series_panels

            return series_panels
        except Exception:
            return None

    def _series_tz(self):
        mod = self._series_panels_mod()
        if mod is not None and callable(getattr(mod, "resolve_series_timezone", None)):
            return mod.resolve_series_timezone(getattr(self, "context", None))
        return datetime.now(timezone.utc).astimezone().tzinfo

    def _is_excluded_by_core(self, umo: str = "", group_id: str = "") -> bool:
        clean_umo = str(umo or "").strip()
        clean_gid = str(group_id or "").strip()
        if not clean_umo and not clean_gid:
            return False
        mod = self._series_panels_mod()
        if mod is not None and callable(getattr(mod, "is_series_group_excluded", None)):
            return bool(
                mod.is_series_group_excluded(
                    getattr(self, "context", None), umo=clean_umo, group_id=clean_gid
                )
            )
        return False

    def _is_event_excluded_by_core(self, event: AstrMessageEvent | None) -> bool:
        if event is None:
            return False
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        gid = ""
        get_gid = getattr(event, "get_group_id", None)
        if callable(get_gid):
            try:
                gid = str(get_gid() or "").strip()
            except Exception:
                gid = ""
        if not gid:
            gid = str(getattr(event, "group_id", "") or "").strip()
        return self._is_excluded_by_core(umo=umo, group_id=gid)

    def get_series_summary(self) -> dict[str, Any]:
        """Return compact live badge and pulse summary for the v2 series matrix."""
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()
        try:
            if getattr(self, "_closed", False):
                return {
                    "badge": {"count": 0, "tone": "muted", "label": "已停用"},
                    "pulse": {"headline": "视觉工坊已关闭", "updated_at": now_iso},
                }
            if self.memes is None and self.generation is None:
                return {
                    "badge": {"count": 0, "tone": "muted", "label": "未就绪"},
                    "pulse": {"headline": "等待引擎初始化", "updated_at": now_iso},
                }

            tz = self._series_tz()
            today_local = now_utc.astimezone(tz).date()
            running = 0
            failed = 0
            succeeded_today = 0
            latest_dt: datetime | None = None
            latest_iso = ""

            for row in self._safe_tracker_rows():
                status = str(row.get("status") or "").strip().lower()
                raw_ts = row.get("finished_at") or row.get("created_at")
                parsed_ts = self._parse_series_ts(raw_ts)
                if parsed_ts is not None and (
                    latest_dt is None or parsed_ts > latest_dt
                ):
                    latest_dt = parsed_ts
                    latest_iso = parsed_ts.isoformat()

                if status in {"queued", "running"}:
                    running += 1
                elif status in {"failed", "interrupted"}:
                    if (
                        parsed_ts is None
                        or (now_utc - parsed_ts).total_seconds() <= 86400
                    ):
                        failed += 1
                elif status in {"succeeded", "partial_success"}:
                    if (
                        parsed_ts is not None
                        and parsed_ts.astimezone(tz).date() == today_local
                    ):
                        succeeded_today += 1

            updated_at = latest_iso or now_iso
            if failed > 0:
                count = failed + running
                label = (
                    f"{failed} 项失败 · {running} 生成中"
                    if running > 0
                    else f"{failed} 项失败"
                )
                headline = (
                    f"{failed} 项近期任务异常 · {running} 项执行中"
                    if running > 0
                    else f"近期 {failed} 项任务失败 · 需排查"
                )
                badge = {"count": count, "tone": "danger", "label": label}
            elif running > 0:
                badge = {
                    "count": running,
                    "tone": "info",
                    "label": f"{running} 项生成中",
                }
                headline = f"{running} 项图像任务执行中"
            else:
                meme_count = len(self._safe_meme_index())
                char_count = len(self._safe_character_profiles())
                badge = {"count": 0, "tone": "accent", "label": "已就绪"}
                if succeeded_today > 0:
                    headline = f"今日完成 {succeeded_today} 次创作 · 队列空闲"
                else:
                    headline = f"队列空闲 · 表情 {meme_count} 张 · 角色 {char_count} 个"

            return {
                "badge": badge,
                "pulse": {"headline": headline, "updated_at": updated_at},
            }
        except Exception:
            return {
                "badge": {"count": 0, "tone": "muted", "label": "状态暂不可用"},
                "pulse": {"headline": "视觉工坊状态暂不可用", "updated_at": now_iso},
            }

    def get_series_scope_status(self, umo: str) -> dict[str, Any]:
        """Return read-only per-scope availability and today's activity for the matrix."""
        try:
            target_umo = str(umo or "").strip()
            ready = (
                not getattr(self, "_closed", False)
                and (self.memes is not None or self.generation is not None)
            )
            if not ready:
                return {
                    "enabled": False,
                    "supported": False,
                    "persona_id": "",
                    "activity_today": 0,
                    "summary": "视觉工坊未就绪",
                    "version": "0",
                }

            group_id = self._extract_group_id_from_umo(target_umo)
            gen_allowed = self.generation is not None
            if gen_allowed and target_umo:
                limiter = getattr(self.generation, "rate_limiter", None)
                if limiter is not None and callable(
                    getattr(limiter, "allows_group", None)
                ):
                    gen_allowed = bool(
                        limiter.allows_group(
                            SimpleNamespace(
                                group_id=group_id, unified_msg_origin=target_umo
                            )
                        )
                    )

            meme_allowed = self.memes is not None
            if meme_allowed and target_umo:
                cfg = getattr(self.memes, "plugin_config", None)
                if cfg is not None and callable(
                    getattr(cfg, "is_targets_allowed", None)
                ):
                    targets = [t for t in (target_umo, group_id) if t]
                    if targets:
                        meme_allowed = bool(cfg.is_targets_allowed("send", targets))

            tz = self._series_tz()
            today_local = datetime.now(timezone.utc).astimezone(tz).date()
            activity_today = 0
            for row in self._safe_tracker_rows():
                req_umo = str((row.get("requester") or {}).get("umo") or "").strip()
                if target_umo and req_umo != target_umo:
                    continue
                parsed_ts = self._parse_series_ts(
                    row.get("finished_at") or row.get("created_at")
                )
                if (
                    parsed_ts is not None
                    and parsed_ts.astimezone(tz).date() == today_local
                ):
                    activity_today += 1

            for meta in self._safe_meme_index().values():
                if not isinstance(meta, dict):
                    continue
                if target_umo and not self._origin_matches_scope(
                    meta.get("origin_target"), target_umo
                ):
                    continue
                parsed_ts = self._parse_series_ts(meta.get("created_at"))
                if (
                    parsed_ts is not None
                    and parsed_ts.astimezone(tz).date() == today_local
                ):
                    activity_today += 1

            excluded_by_core = bool(
                target_umo
                and self._is_excluded_by_core(umo=target_umo, group_id=group_id)
            )
            if excluded_by_core:
                return {
                    "enabled": False,
                    "supported": False,
                    "excluded_by_core": True,
                    "persona_id": "",
                    "activity_today": activity_today,
                    "summary": "主控排除（表情采集与被动视觉已静默）",
                    "version": "1:0:0",
                }

            enabled = bool(gen_allowed or meme_allowed)
            if gen_allowed and meme_allowed:
                mode_summary = "生图与表情可用"
            elif gen_allowed:
                mode_summary = "仅生图可用 · 表情已禁用"
            elif meme_allowed:
                mode_summary = "仅表情可用 · 生图已禁用"
            else:
                mode_summary = "当前会话已禁用生图与表情"
            summary = (
                f"{mode_summary} · 今日 {activity_today} 次"
                if activity_today > 0
                else mode_summary
            )
            return {
                "enabled": enabled,
                "supported": True,
                "excluded_by_core": False,
                "persona_id": "",
                "activity_today": activity_today,
                "summary": summary,
                "version": f"1:{int(gen_allowed)}:{int(meme_allowed)}",
            }
        except Exception:
            return {
                "enabled": None,
                "supported": False,
                "persona_id": "",
                "activity_today": None,
                "summary": "状态暂不可用",
                "version": "0",
            }

    def series_search_items(
        self, query: str, scope: str = "", limit: int = 8
    ) -> list[dict[str, Any]]:
        """Search character profiles, recent generation tasks, and visible memes."""
        try:
            q = str(query or "").strip().casefold()
            if not q or getattr(self, "_closed", False):
                return []
            try:
                max_items = max(1, min(50, int(limit)))
            except (TypeError, ValueError):
                max_items = 8
            target_scope = str(scope or "").strip()
            results: list[dict[str, Any]] = []

            # 1. Character profiles
            for profile in self._safe_character_profiles():
                if len(results) >= max_items:
                    return results
                key = str(profile.get("key") or "").strip()
                name = str(profile.get("name") or key).strip()
                appearance = str(profile.get("appearance") or "").strip()
                style = str(profile.get("style") or "").strip()
                haystack = f"{key} {name} {appearance} {style}".casefold()
                if q in haystack:
                    subtitle = (appearance or style or f"角色标识：{key}")[:120]
                    results.append(
                        {
                            "kind": "character",
                            "title": name or key,
                            "subtitle": subtitle,
                            "plugin_id": "astrbot_plugin_visual_media",
                            "page": "workshop",
                            "deep_link": {"section": "characters", "character": key},
                        }
                    )

            # 2. Recent generation tasks / prompts
            status_labels = {
                "queued": "排队中",
                "running": "生成中",
                "succeeded": "已完成",
                "partial_success": "部分完成",
                "failed": "失败",
                "interrupted": "已中断",
            }
            rows = sorted(
                self._safe_tracker_rows(),
                key=lambda r: str(r.get("created_at") or ""),
                reverse=True,
            )
            for row in rows:
                if len(results) >= max_items:
                    return results
                req_umo = str((row.get("requester") or {}).get("umo") or "").strip()
                if target_scope and req_umo and req_umo != target_scope:
                    continue
                job_id = str(row.get("job_id") or "").strip()
                prompt = str(row.get("prompt") or "").strip()
                status = str(row.get("status") or "").strip().lower()
                source = str(row.get("source") or "").strip()
                haystack = f"{prompt} {job_id} {status} {source}".casefold()
                if q in haystack:
                    results.append(
                        {
                            "kind": "task",
                            "title": (prompt[:60] or f"任务 #{job_id[:8]}"),
                            "subtitle": f"{status_labels.get(status, status or '任务')} · {source or '创作'}",
                            "plugin_id": "astrbot_plugin_visual_media",
                            "page": "workshop",
                            "deep_link": {"subpage": "tasks", "job_id": job_id},
                        }
                    )

            # 3. Meme descriptions (in-memory read-only index; respect scope privacy)
            for meta in self._safe_meme_index().values():
                if len(results) >= max_items:
                    return results
                if not isinstance(meta, dict):
                    continue
                scope_mode = str(meta.get("scope_mode") or "public")
                origin_target = str(meta.get("origin_target") or "")
                if scope_mode != "public" and (
                    not target_scope
                    or not self._origin_matches_scope(origin_target, target_scope)
                ):
                    continue
                desc = str(meta.get("desc") or "").strip()
                overlay = str(meta.get("overlay_text") or "").strip()
                category = str(meta.get("category") or "").strip()
                character = str(meta.get("character") or "").strip()
                tags = meta.get("tags") or []
                tag_text = (
                    " ".join(str(t) for t in tags)
                    if isinstance(tags, (list, tuple))
                    else str(tags)
                )
                haystack = f"{desc} {overlay} {category} {character} {tag_text}".casefold()
                if q in haystack:
                    title = (desc or overlay or category or "表情")[:60]
                    subtitle_parts = [p for p in ("表情", category, character) if p]
                    results.append(
                        {
                            "kind": "meme",
                            "title": title,
                            "subtitle": " · ".join(subtitle_parts)[:120],
                            "plugin_id": "astrbot_plugin_visual_media",
                            "page": "workshop",
                            "deep_link": {
                                "subpage": "memes",
                                "hash": str(meta.get("hash") or ""),
                            },
                        }
                    )

            return results
        except Exception:
            return []

    def get_journal_events(
        self,
        persona: str = "",
        scope: str = "",
        date: str = "today",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return today's completed creations and saved memes for Today's Living Journal."""
        try:
            if getattr(self, "_closed", False):
                return []
            try:
                max_items = max(1, min(100, int(limit)))
            except (TypeError, ValueError):
                max_items = 20

            tz = self._series_tz()
            date_key = str(date or "today").strip().lower()
            if date_key in {"", "today"}:
                target_date = datetime.now(timezone.utc).astimezone(tz).date()
            else:
                try:
                    target_date = _date_cls.fromisoformat(date_key)
                except ValueError:
                    return []

            target_scope = str(scope or "").strip()
            events: list[tuple[float, dict[str, Any]]] = []

            # 1. Completed image creations
            for row in self._safe_tracker_rows():
                status = str(row.get("status") or "").strip().lower()
                if status not in {"succeeded", "partial_success"}:
                    continue
                req_umo = str((row.get("requester") or {}).get("umo") or "").strip()
                if target_scope and req_umo != target_scope:
                    continue
                parsed_ts = self._parse_series_ts(
                    row.get("finished_at") or row.get("created_at")
                )
                if (
                    parsed_ts is None
                    or parsed_ts.astimezone(tz).date() != target_date
                ):
                    continue
                job_id = str(row.get("job_id") or "").strip()
                prompt = str(row.get("prompt") or "").strip()
                img_count = len(row.get("images") or []) or int(
                    row.get("generated_images") or 0
                )
                title = (
                    f"完成图像创作：{prompt[:48]}"
                    if prompt
                    else f"完成图像创作（{max(1, img_count)} 张）"
                )
                source = str(row.get("source") or "工坊").strip()
                subtitle = f"生成 {max(1, img_count)} 张图片 · 来源：{source}"
                events.append(
                    (
                        parsed_ts.timestamp(),
                        {
                            "id": f"gen:{job_id or int(parsed_ts.timestamp())}",
                            "category": "visual_media",
                            "source_label": "视觉工坊",
                            "tone": "accent" if status == "succeeded" else "warm",
                            "occurred_at": parsed_ts.isoformat(),
                            "title": title,
                            "subtitle": subtitle[:140],
                            "target_plugin": "astrbot_plugin_visual_media",
                            "target_route": "workshop",
                            "target_id": job_id,
                        },
                    )
                )

            # 2. Saved memes
            for path, meta in self._safe_meme_index().items():
                if not isinstance(meta, dict):
                    continue
                origin_target = str(meta.get("origin_target") or "")
                scope_mode = str(meta.get("scope_mode") or "public")
                if target_scope:
                    if not self._origin_matches_scope(origin_target, target_scope):
                        continue
                elif scope_mode != "public" and not meta.get("is_favorite"):
                    continue
                parsed_ts = self._parse_series_ts(meta.get("created_at"))
                if (
                    parsed_ts is None
                    or parsed_ts.astimezone(tz).date() != target_date
                ):
                    continue
                img_hash = str(meta.get("hash") or "").strip()
                desc = str(meta.get("desc") or meta.get("overlay_text") or "").strip()
                category = str(meta.get("category") or "未分类").strip()
                character = str(meta.get("character") or "").strip()
                title = f"收藏新表情：{desc[:48]}" if desc else "收藏了新的表情图"
                sub_parts = [f"分类：{category}"]
                if character:
                    sub_parts.append(f"角色：{character}")
                events.append(
                    (
                        parsed_ts.timestamp(),
                        {
                            "id": f"meme:{img_hash or path}",
                            "category": "visual_media",
                            "source_label": "视觉工坊",
                            "tone": "warm" if meta.get("is_favorite") else "info",
                            "occurred_at": parsed_ts.isoformat(),
                            "title": title,
                            "subtitle": " · ".join(sub_parts)[:140],
                            "target_plugin": "astrbot_plugin_visual_media",
                            "target_route": "workshop",
                            "target_id": img_hash,
                        },
                    )
                )

            events.sort(key=lambda item: item[0], reverse=True)
            return [item[1] for item in events[:max_items]]
        except Exception:
            return []

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
        if self._is_event_excluded_by_core(event):
            return None
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
        if (
            self._is_event_excluded_by_core(event)
            or event.get_extra("_narrative_internal_v1", False)
            or event.get_extra("visual_media_generation_requested", False)
        ):
            return
        return await self.memes._start_joint_meme_prefetch(event, req)

    @staticmethod
    def _attach_temp_user_prompt(req: Any, extra_prompt: str) -> None:
        """Append per-speaker/per-turn visual preferences to temp user parts to keep system_prompt static."""
        extra_parts = getattr(req, "extra_user_content_parts", None)
        if isinstance(extra_parts, list):
            try:
                from astrbot.core.agent.message import TextPart
            except ImportError:
                TextPart = None
            if TextPart is not None:
                part = TextPart(text=extra_prompt.strip())
                if hasattr(part, "mark_as_temp"):
                    part = part.mark_as_temp()
                extra_parts.append(part)
                return
        req.system_prompt = (
            str(getattr(req, "system_prompt", "") or "") + extra_prompt
        )

    @filter.on_llm_request(priority=1)
    async def _prepare_joint_meme_reply(self, event: AstrMessageEvent, req):
        if self._is_event_excluded_by_core(event) or event.get_extra(
            "_narrative_internal_v1", False
        ):
            return
        attach_prompt = not getattr(req, "_visual_preferences_attached", False)
        extra_prompt = await asyncio.to_thread(
            self.behavior.remember_and_prompt, event, attach_prompt
        )
        if attach_prompt:
            if extra_prompt:
                self._attach_temp_user_prompt(req, extra_prompt)
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
        if (
            self._is_event_excluded_by_core(event)
            or event.get_extra("visual_media_generation_requested", False)
            or joint_reply_owned(event)
        ):
            return
        return await self.memes._prepare_emoji_after_llm(event, response)

    @filter.on_decorating_result(priority=100)
    async def _prepare_emoji_response(self, event: AstrMessageEvent):
        """LLM 回复完成后异步发送表情包（不阻塞回复）。"""
        if (
            self._is_event_excluded_by_core(event)
            or event.get_extra("visual_media_generation_requested", False)
            or joint_reply_owned(event)
        ):
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
