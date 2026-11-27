"""Image delivery, library listing, and statistics routes."""

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image
from quart import jsonify, request

try:
    from quart import send_file
except ImportError:
    send_file = None  # type: ignore[assignment]

from astrbot.api import logger

from ..core.maintenance.retention import library_counts, library_group

try:
    from .. import series_panels as _series_panels
except Exception:  # pragma: no cover - standalone fallback
    try:
        import series_panels as _series_panels  # type: ignore[no-redef]
    except Exception:
        _series_panels = None  # type: ignore[assignment]


class LibraryRoutes:
    # ── Image serving ─────────────────────────────────────────
    SERVE_IMAGE_ALLOWED_SUBDIRS = ("categories", "pending", "thumb_cache", "raw")
    _SERVE_IMAGE_ERROR = "图片不存在或无权访问"

    def _resolve_serve_image_path(self, raw_path: Any) -> Path | None:
        """先做媒体子目录与图片扩展名白名单校验，再检查文件是否存在。"""
        text = str(raw_path or "").strip()
        if not text or "\x00" in text:
            return None
        try:
            data_dir = self._data_dir.resolve()
            allowed_roots = tuple(
                (data_dir / subdir).resolve()
                for subdir in self.SERVE_IMAGE_ALLOWED_SUBDIRS
            )
            candidate = Path(text)
            if not candidate.is_absolute():
                candidate = data_dir / candidate
            resolved = candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            return None

        if not any(
            resolved != root and resolved.is_relative_to(root)
            for root in allowed_roots
        ):
            return None

        allowed_exts = getattr(
            self,
            "ALLOWED_IMAGE_EXTS",
            (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"),
        )
        if resolved.suffix.lower() not in allowed_exts:
            return None

        try:
            if not resolved.is_file():
                return None
        except OSError:
            return None
        return resolved

    async def handle_serve_image(self):
        """直接服务图片文件（用于页面展示）。"""
        if send_file is None:
            return jsonify({"success": False, "error": "send_file 不可用"}), 500
        file_path = request.args.get("path", "")
        resolved = self._resolve_serve_image_path(file_path)
        if resolved is None:
            return jsonify({"success": False, "error": self._SERVE_IMAGE_ERROR}), 404
        return await send_file(str(resolved))

    def _max_base64_image_bytes(self) -> int:
        if _series_panels is not None:
            getter = getattr(_series_panels, "get_series_resource_limits", None)
            if callable(getter):
                try:
                    ctx = getattr(getattr(self, "plugin", None), "context", None)
                    limits = getter(ctx)
                    if isinstance(limits, dict):
                        return max(
                            2 * 1024 * 1024,
                            int(limits.get("visual_max_image_bytes", 16 * 1024 * 1024)),
                        )
                except Exception:
                    pass
        return 16 * 1024 * 1024

    async def handle_image_data(self):
        """返回图片的 base64 data URL；当客户端请求 raw=1/binary=1 时直接二进制流式返回。"""
        image_hash = request.args.get("hash", "").strip()
        if not image_hash:
            return jsonify({"success": False, "error": "缺少 hash"})
        found = await asyncio.to_thread(self._find_index_entry_by_hash, image_hash)
        file_path = found[0] if found else None

        if not file_path:
            db = self._db
            if db and hasattr(db, "get_pending_by_hash"):
                try:
                    pending_row = await asyncio.to_thread(
                        db.get_pending_by_hash, image_hash
                    )
                except Exception:
                    pending_row = None
                if pending_row:
                    file_path = pending_row.get("path")

        if file_path and await asyncio.to_thread(os.path.isfile, file_path):
            prefer_binary = str(
                request.args.get("raw") or request.args.get("binary") or ""
            ).strip().lower() in ("1", "true", "yes")
            if prefer_binary and send_file is not None:
                resolved = self._resolve_serve_image_path(file_path)
                if resolved is not None:
                    return await send_file(str(resolved))
            try:
                max_bytes = self._max_base64_image_bytes()
                file_size = await asyncio.to_thread(os.path.getsize, file_path)
                if file_size > max_bytes:
                    return (
                        jsonify(
                            {
                                "success": False,
                                "error": "原图过大，请改用二进制流接口读取",
                            }
                        ),
                        413,
                    )
                data_url = await asyncio.to_thread(self._file_base64, file_path)
                return jsonify({"success": True, "hash": image_hash, "url": data_url})
            except Exception as e:
                logger.warning(f"读取图片失败: {e}")
        return jsonify({"success": False, "error": "图片未找到"})

    async def _get_or_create_thumbnail(
        self, img_hash: str, file_path: str, max_size: int = 300
    ) -> str:
        """生成或返回缓存的缩略图路径。"""
        thumb_dir = self._data_dir / "thumb_cache"
        thumb_path = thumb_dir / f"{img_hash}_{max_size}.jpg"

        def _get_or_create() -> str:
            if thumb_path.exists():
                return str(thumb_path)

            if not os.path.isfile(file_path):
                raise FileNotFoundError(file_path)

            thumb_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(file_path) as img:
                try:
                    img.seek(0)
                except EOFError:
                    pass
                if getattr(img, "format", "") == "JPEG":
                    try:
                        img.draft("RGB", (max_size, max_size))
                    except Exception:
                        pass
                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
                if img.mode in ("RGBA", "LA"):
                    canvas = Image.new("RGB", img.size, (16, 16, 16))
                    canvas.paste(img, mask=img.split()[-1])
                    img = canvas
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(thumb_path, "JPEG", quality=82, optimize=True)
            return str(thumb_path)

        return await asyncio.to_thread(_get_or_create)

    async def handle_thumbnail(self):
        """返回缩略图的 base64 data URL，用于列表展示。"""
        img_hash = request.args.get("hash", "").strip()
        max_size = request.args.get("size", 300, type=int)
        try:
            max_size = max(32, min(int(max_size or 300), 400))
        except (TypeError, ValueError):
            max_size = 300

        if not img_hash:
            return jsonify({"success": False, "error": "缺少 hash"}), 400

        found = await asyncio.to_thread(self._find_index_entry_by_hash, img_hash)
        file_path = found[0] if found else None

        # 回退：正式库未命中时查待审核池（审核区缩略图走此路径）
        if not file_path:
            db = self._db
            if db and hasattr(db, "get_pending_by_hash"):
                try:
                    pending_row = await asyncio.to_thread(
                        db.get_pending_by_hash, img_hash
                    )
                except Exception:
                    pending_row = None
                if pending_row:
                    file_path = pending_row.get("path")

        if not file_path or not await asyncio.to_thread(os.path.isfile, file_path):
            return jsonify({"success": False, "error": "图片未找到"}), 404

        try:
            thumb_path = await self._get_or_create_thumbnail(
                img_hash, file_path, max_size
            )
            data_url = await asyncio.to_thread(self._file_base64, thumb_path)
            return jsonify({"success": True, "hash": img_hash, "url": data_url})
        except Exception as e:
            logger.warning(f"生成缩略图失败: {e}")
            return jsonify({"success": False, "error": "缩略图生成失败"}), 500

    # ── List / Stats / Health ─────────────────────────────────

    def _build_image_list_payload(
        self,
        *,
        total: int,
        page: int,
        page_size: int,
        images: list[dict],
        category_counts: dict[str, int],
        character_counts: dict[str, int],
        libraries: dict[str, int],
    ) -> dict:
        return {
            "success": True,
            "total": total,
            "page": page,
            "size": page_size,
            "images": images,
            "categories": self._build_categories_list(category_counts),
            "characters": self._build_characters_list(character_counts),
            "unassigned_character_count": int(character_counts.get("", 0) or 0),
            "favorite_count": self._count_favorites(),
            "libraries": libraries,
            "automatic_limit": self.plugin.plugin_config.max_reg_num,
        }

    def _list_images_from_database(
        self,
        *,
        db: Any,
        page: int,
        page_size: int,
        category: str | None,
        search: str,
        sort_order: str,
        favorite_only: bool,
        character: str,
        library: str,
    ) -> dict | None:
        """Query the paginated SQLite view when it contains live rows."""
        get_paginated = getattr(db, "get_emojis_paginated", None) if db else None
        if not db or not callable(get_paginated) or db.count_total() <= 0:
            return None

        raw, total, category_counts = get_paginated(
            page=page,
            page_size=page_size,
            category=category,
            sort_order=sort_order,
            search_query=search or None,
            favorite_only=favorite_only,
            character=character or None,
            **({"library": library} if library else {}),
        )
        images = [
            item for item in (self._build_image_item(row["path"], row) for row in raw) if item
        ]
        character_counts = (
            db.get_character_counts(exclude_favorites=True)
            if library == "characters"
            else db.get_character_counts()
        )
        return self._build_image_list_payload(
            total=total,
            page=page,
            page_size=page_size,
            images=images,
            category_counts=category_counts,
            character_counts=character_counts,
            libraries=db.get_library_counts(),
        )

    def _list_images_from_index(
        self,
        *,
        page: int,
        page_size: int,
        category: str | None,
        search: str,
        sort_order: str,
        favorite_only: bool,
        character: str,
        library: str,
        scope: str = "",
    ) -> dict:
        """Build the same response shape from legacy index-only data (and scope-filtered views)."""
        from ..core.config.config import PluginConfig

        index = self._get_index()
        images: list[dict] = []
        category_counts: dict[str, int] = {}
        clean_scope = str(scope or "").strip()

        for path_str, metadata in index.items():
            if not Path(path_str).exists():
                continue
            item = self._build_image_item(path_str, metadata)
            if not item:
                continue
            if clean_scope:
                item_scope_mode = str(item.get("scope_mode") or "public").strip().lower()
                if item_scope_mode != "public" and not PluginConfig.origin_target_matches_scope(
                    item.get("origin_target"), clean_scope
                ):
                    continue
            if search and not (
                any(search in str(tag).lower() for tag in item["tags"])
                or search in item["desc"].lower()
                or any(search in str(scene).lower() for scene in item.get("scenes", []))
            ):
                continue
            if library in {"general", "favorites", "characters"} and library_group(item) != library:
                continue

            category_key = item["category"]
            category_counts[category_key] = category_counts.get(category_key, 0) + 1
            if category and item["category"] != category:
                continue
            if favorite_only and not item.get("is_favorite"):
                continue
            item_character = str(item.get("character", "") or "")
            if character == "__none__" and item_character:
                continue
            if character and character != "__none__" and item_character != character:
                continue
            images.append(item)

        sort_fields = {
            "least_used": ("use_count", "created_at"),
            "most_used": ("use_count", "last_used_at"),
            "last_used": ("last_used_at", "use_count"),
        }.get(sort_order, ("created_at",))
        images.sort(
            key=lambda item: tuple(int(item.get(field, 0) or 0) for field in sort_fields)
            + (str(item.get("hash", "")),),
            reverse=sort_order not in {"oldest", "least_used"},
        )

        total = len(images)
        start = (page - 1) * page_size
        character_counts: dict[str, int] = {}
        for metadata in index.values():
            if not isinstance(metadata, dict):
                continue
            if library == "characters" and metadata.get("is_favorite"):
                continue
            key = str(metadata.get("character", "") or "")
            character_counts[key] = character_counts.get(key, 0) + 1

        return self._build_image_list_payload(
            total=total,
            page=page,
            page_size=page_size,
            images=images[start : start + page_size],
            category_counts=category_counts,
            character_counts=character_counts,
            libraries=library_counts(index),
        )

    async def handle_list_images(self):
        """Return a paginated image list and category counts."""
        try:
            page = request.args.get("page", 1, type=int)
            page_size = request.args.get("size", 50, type=int)
            category = request.args.get("category", None)
            search = str(request.args.get("q", "")).lower()
            sort_order = request.args.get("sort", "newest")
            favorite_only = request.args.get("favorite_only", "false").lower() == "true"
            character = str(request.args.get("character", "") or "")
            library = str(request.args.get("library", "") or "")
            scope = str(request.args.get("scope", "") or "").strip()

            payload = None
            if not scope:
                payload = await asyncio.to_thread(
                    self._list_images_from_database,
                    db=self._db,
                    page=page,
                    page_size=page_size,
                    category=category,
                    search=search,
                    sort_order=sort_order,
                    favorite_only=favorite_only,
                    character=character,
                    library=library,
                )
            if payload is None:
                payload = await asyncio.to_thread(
                    self._list_images_from_index,
                    page=page,
                    page_size=page_size,
                    category=category,
                    search=search,
                    sort_order=sort_order,
                    favorite_only=favorite_only,
                    character=character,
                    library=library,
                    scope=scope,
                )
            return jsonify(payload)
        except Exception as e:
            logger.error(f"Error listing images: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_get_stats(self):
        try:
            index = self._get_index()
            today_start = (
                datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            )
            today_count = sum(
                1
                for m in index.values()
                if isinstance(m, dict) and m.get("created_at", 0) >= today_start
            )
            cat_count = len(self._cfg.categories) if hasattr(self.plugin, "plugin_config") else 0
            return jsonify(
                {
                    "success": True,
                    "stats": {"total": len(index), "categories": cat_count, "today": today_count},
                }
            )
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            return jsonify({"success": False, "error": str(e)})

    async def handle_health_check(self):
        cfg = getattr(self.plugin, "plugin_config", None)
        setup = None if cfg is None else {
            "capture_enabled": bool(getattr(cfg, "steal_meme", False)),
            "auto_send_enabled": bool(getattr(cfg, "auto_send_meme", False)),
            "context_enabled": bool(getattr(cfg, "enable_natural_emotion_analysis", False)),
            "embedding_enabled": bool(getattr(cfg, "enable_embedding_search", False)),
        }
        return jsonify({"success": True, "status": "ok", "service": "emoji-manager-webui", "setup": setup})
