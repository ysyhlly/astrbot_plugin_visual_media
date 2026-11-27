"""One-click 9-grid character sticker pack pipeline: generate -> split -> auto-tag -> import."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from PIL import Image as PILImage

from astrbot.api import logger


DEFAULT_PACK_EMOTIONS: tuple[tuple[str, str], ...] = (
    ("happy", "开心大笑"),
    ("angry", "生气炸毛"),
    ("sad", "委屈哭哭"),
    ("surprised", "震惊呆住"),
    ("awkward", "无语流汗"),
    ("love", "比心贴贴"),
    ("confused", "疑惑问号"),
    ("tired", "困倦摸鱼"),
    ("cheer", "点赞好耶"),
)


class CharacterPackService:
    """Generate a 3x3 character emotion sticker sheet, split slices, auto-tag, and import."""

    STAGED_TTL_SECONDS = 30 * 60
    MAX_STAGED_PACKS = 24

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self._staged: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def _work_root(self) -> Path:
        base = getattr(getattr(self.owner, "memes", None), "base_dir", None)
        if base is None:
            base = Path(self.owner.generation._plugin_data_dir)
        root = Path(base) / "temp" / "character_packs"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _prune_staged(self) -> None:
        now = time.time()
        expired = [
            pid
            for pid, item in self._staged.items()
            if now - float(item.get("created_at", 0) or 0) > self.STAGED_TTL_SECONDS
        ]
        for pid in expired:
            entry = self._staged.pop(pid, None)
            work_dir = entry.get("work_dir") if isinstance(entry, dict) else None
            if work_dir:
                shutil.rmtree(work_dir, ignore_errors=True)
        while len(self._staged) > self.MAX_STAGED_PACKS:
            oldest_key = min(
                self._staged,
                key=lambda k: float(self._staged[k].get("created_at", 0) or 0),
            )
            entry = self._staged.pop(oldest_key, None)
            work_dir = entry.get("work_dir") if isinstance(entry, dict) else None
            if work_dir:
                shutil.rmtree(work_dir, ignore_errors=True)

    @staticmethod
    def normalize_emotions(raw_emotions: Any) -> list[tuple[str, str]]:
        """Normalize up to 9 emotions into (key, label) pairs."""
        if not raw_emotions:
            return list(DEFAULT_PACK_EMOTIONS)
        items: list[str] = []
        if isinstance(raw_emotions, str):
            for sep in ("，", ",", "、", "\n", ";", "；"):
                raw_emotions = raw_emotions.replace(sep, ",")
            items = [x.strip() for x in raw_emotions.split(",") if x.strip()]
        elif isinstance(raw_emotions, (list, tuple)):
            items = [str(x).strip() for x in raw_emotions if str(x or "").strip()]
        if not items:
            return list(DEFAULT_PACK_EMOTIONS)

        result: list[tuple[str, str]] = []
        for idx, label in enumerate(items[:9]):
            default_key = (
                DEFAULT_PACK_EMOTIONS[idx][0]
                if idx < len(DEFAULT_PACK_EMOTIONS)
                else "happy"
            )
            result.append((default_key, label[:24]))
        while len(result) < 9:
            result.append(DEFAULT_PACK_EMOTIONS[len(result)])
        return result

    @staticmethod
    def build_sheet_prompt(
        profile: dict[str, Any],
        emotions: list[tuple[str, str]],
        *,
        style: str = "",
    ) -> str:
        """Build a clean 3x3 character sticker pack prompt."""
        name = str(profile.get("name") or profile.get("key") or "角色").strip()
        appearance = str(profile.get("appearance") or "").strip()
        profile_style = str(profile.get("style") or "").strip()
        custom_style = str(style or "").strip()

        grid_lines = []
        for idx, (_, label) in enumerate(emotions[:9], 1):
            row = (idx - 1) // 3 + 1
            col = (idx - 1) % 3 + 1
            grid_lines.append(f"第{row}行第{col}格：{label}")

        parts = [
            f"请为角色「{name}」绘制一套 3x3 九宫格 Q 版聊天表情包贴纸套图（共 9 个独立表情）。",
            "排版要求：严格 3 行 × 3 列均匀排列，纯白色干净背景，每个表情之间留出充足空白间距、不要互相重叠，不要画边框线或网格线，高辨识度粗线条描边，表情夸张可爱、适合聊天贴纸。",
        ]
        if appearance:
            parts.append(f"角色固定外观（所有 9 格必须严格保持一致）：{appearance}")
        merged_style = "；".join(s for s in (profile_style, custom_style) if s)
        if merged_style:
            parts.append(f"画风要求：{merged_style}")
        else:
            parts.append("画风要求：精致日系 Q 版 LINE 贴纸风格，明快色彩，清晰描边。")
        parts.append("9 格情绪内容：\n" + "\n".join(grid_lines))
        return "\n".join(parts)

    @staticmethod
    def _fallback_pillow_split(sheet_path: str, output_dir: Path, rows: int = 3, cols: int = 3) -> list[str]:
        """Pure-Pillow 3x3 grid splitter fallback when cv2/adaptive splitter is unavailable."""
        output_dir.mkdir(parents=True, exist_ok=True)
        out_files: list[str] = []
        with PILImage.open(sheet_path) as im:
            rgb = im.convert("RGBA")
            w, h = rgb.size
            cell_w = max(1, w // cols)
            cell_h = max(1, h // rows)
            idx = 0
            for r in range(rows):
                for c in range(cols):
                    idx += 1
                    x1 = c * cell_w
                    y1 = r * cell_h
                    x2 = w if c == cols - 1 else (c + 1) * cell_w
                    y2 = h if r == rows - 1 else (r + 1) * cell_h
                    crop = rgb.crop((x1, y1, x2, y2))
                    # Trim a tiny margin if large enough to avoid neighbor bleed
                    if crop.width > 40 and crop.height > 40:
                        margin_x = max(1, int(crop.width * 0.02))
                        margin_y = max(1, int(crop.height * 0.02))
                        crop = crop.crop(
                            (margin_x, margin_y, crop.width - margin_x, crop.height - margin_y)
                        )
                    dest = output_dir / f"slice_{idx:03d}.png"
                    crop.save(dest, format="PNG")
                    out_files.append(str(dest))
        return out_files

    async def _split_sheet(self, sheet_path: str, output_dir: Path) -> list[str]:
        """Split 3x3 sticker sheet using adaptive splitter first, falling back to 3x3 grid."""
        def _run_split() -> list[str]:
            try:
                from ...tl.image_splitter import split_image

                files = split_image(
                    sheet_path,
                    rows=3,
                    cols=3,
                    output_dir=str(output_dir),
                    manual_rows=3,
                    manual_cols=3,
                    use_sticker_cutter=True,
                )
                valid = [f for f in (files or []) if Path(f).is_file()]
                if len(valid) >= 2:
                    return valid
            except Exception as exc:
                logger.debug("[CharacterPack] adaptive splitter unavailable/fallback: %s", exc)
            return self._fallback_pillow_split(sheet_path, output_dir, rows=3, cols=3)

        return await asyncio.to_thread(_run_split)

    @staticmethod
    def _encode_thumb_data_url(image_path: str, max_size: int = 220) -> str:
        try:
            with PILImage.open(image_path) as im:
                im.seek(0)
                copy = im.convert("RGBA")
                copy.thumbnail((max_size, max_size))
                buf = io.BytesIO()
                copy.save(buf, format="PNG")
                return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            return ""

    def _resolve_fallback_category(self, preferred_key: str) -> str:
        cfg = getattr(getattr(self.owner, "memes", None), "plugin_config", None)
        categories = list(getattr(cfg, "categories", None) or [])
        if preferred_key and preferred_key in categories:
            return preferred_key
        if cfg is not None and callable(getattr(cfg, "closest_category", None)):
            try:
                mapped = cfg.closest_category(preferred_key)
                if mapped:
                    return mapped
            except Exception:
                pass
        return categories[0] if categories else (preferred_key or "happy")

    async def _tag_slice(
        self,
        slice_path: str,
        *,
        profile: dict[str, Any],
        emotion_slot: tuple[str, str],
    ) -> dict[str, Any]:
        """Classify a sticker slice via VLM, degrading cleanly to planned emotion metadata."""
        char_key = str(profile.get("key") or "").strip()
        char_name = str(profile.get("name") or char_key).strip()
        slot_key, slot_label = emotion_slot
        fallback_cat = self._resolve_fallback_category(slot_key)
        fallback = {
            "category": fallback_cat,
            "tags": [t for t in (char_name, slot_label, "角色套图") if t],
            "desc": f"{char_name} {slot_label}表情",
            "scenes": [slot_label],
            "overlay_text": "",
            "emotions": [fallback_cat],
            "character": char_key,
            "tagged_by": "preset_fallback",
        }
        memes = getattr(self.owner, "memes", None)
        proc = getattr(memes, "image_processor_service", None) if memes else None
        cfg = getattr(memes, "plugin_config", None) if memes else None
        categories = list(getattr(cfg, "categories", None) or [])
        if proc is None or not callable(getattr(proc, "classify_image", None)):
            return fallback
        try:
            classified = await proc.classify_image(
                event=None,
                file_path=slice_path,
                categories=categories,
                content_filtration=False,
            )
            cat, tags, desc, _, scenes, overlay_text, emotions = classified
            filtered_sentinel = getattr(proc, "CATEGORY_FILTERED", None)
            if not cat or cat == filtered_sentinel:
                return fallback
            merged_tags = list(dict.fromkeys([*(tags or []), char_name, "角色套图"]))
            return {
                "category": str(cat).strip() or fallback_cat,
                "tags": [t for t in merged_tags if t],
                "desc": str(desc or "").strip() or fallback["desc"],
                "scenes": list(scenes or []) or fallback["scenes"],
                "overlay_text": str(overlay_text or "").strip(),
                "emotions": list(emotions or []) or [str(cat).strip() or fallback_cat],
                "character": char_key,
                "tagged_by": "vlm",
            }
        except Exception as exc:
            logger.debug("[CharacterPack] VLM tag fallback for %s: %s", slice_path, exc)
            return fallback

    async def _import_slice(
        self,
        slice_item: dict[str, Any],
        *,
        scope_mode: str = "public",
        origin_target: str = "",
    ) -> dict[str, Any]:
        api = self.owner.memes.plugin_api
        file_path = Path(slice_item["path"])
        raw = await asyncio.to_thread(file_path.read_bytes)
        ext = file_path.suffix.lower() or ".png"
        digest = hashlib.sha256(raw).hexdigest()
        persisted = await api._persist_image(
            file_content=raw,
            file_ext=ext,
            category=slice_item["category"],
            file_hash=digest,
            tags=slice_item["tags"],
            desc=slice_item["desc"],
            scenes=slice_item["scenes"],
            overlay_text=slice_item.get("overlay_text", ""),
            emotions=slice_item.get("emotions") or [slice_item["category"]],
            character=slice_item["character"],
            scope_mode=scope_mode,
            origin_target=origin_target,
            add_method="character_pack",
        )
        return persisted

    async def generate_pack(
        self,
        *,
        character: str,
        emotions: Any = None,
        style: str = "",
        preview_only: bool = False,
        scope_mode: str = "public",
        origin_target: str = "",
        umo: str = "",
    ) -> dict[str, Any]:
        """Generate 9-grid sticker sheet, split, auto-tag, and optionally import into library."""
        char_key = str(character or "").strip()
        if not char_key:
            raise ValueError("缺少角色标识")
        profile = next(
            (p for p in self.owner.library.profiles() if p.get("key") == char_key),
            None,
        )
        if profile is None:
            raise ValueError(f"角色不存在: {char_key}")

        clean_scope = "local" if str(scope_mode or "").strip().lower() == "local" else "public"
        clean_origin = str(origin_target or "").strip()
        if clean_scope == "local" and not clean_origin:
            if umo:
                clean_origin = f"umo:{umo}"
            else:
                clean_scope = "public"

        refs: list[str] = []
        for ref_hash in profile.get("references") or []:
            try:
                refs.append(str(self.owner.library.reference_path(ref_hash)))
            except Exception:
                continue

        emotion_list = self.normalize_emotions(emotions)
        prompt = self.build_sheet_prompt(profile, emotion_list, style=style)

        service = self.owner.generation.get_service(1)
        submission = await service.submit(
            plugin_id=getattr(self.owner, "name", "astrbot_plugin_visual_media"),
            plugin_name="视觉工坊 · 角色表情套图",
            prompt=prompt,
            umo=umo if umo and umo.count(":") >= 2 else None,
            reference_images=refs or None,
            use_rate_limit=True,
            image_count=1,
            aspect_ratio="1:1",
        )
        task_id = str(submission.get("task_id") or "")
        task = await service.wait_task(
            task_id,
            plugin_id=getattr(self.owner, "name", "astrbot_plugin_visual_media"),
        )
        status = str(task.get("status") or "").lower()
        image_paths = [
            str(p) for p in (task.get("image_paths") or []) if isinstance(p, str) and Path(p).is_file()
        ]
        if status not in {"succeeded", "partial_success"} or not image_paths:
            err = str(task.get("error") or task.get("message") or f"套图生成失败 ({status})")
            raise RuntimeError(err)

        sheet_path = image_paths[0]
        pack_id = uuid.uuid4().hex[:16]
        work_dir = self._work_root() / pack_id
        slice_paths = await self._split_sheet(sheet_path, work_dir)
        if not slice_paths:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise RuntimeError("套图切分失败，未检测到有效表情切片")

        sheet_preview = await asyncio.to_thread(self._encode_thumb_data_url, sheet_path, 360)
        staged_items: list[dict[str, Any]] = []
        imported_count = 0
        failed_count = 0

        for idx, sp in enumerate(slice_paths):
            slot = emotion_list[idx] if idx < len(emotion_list) else DEFAULT_PACK_EMOTIONS[idx % len(DEFAULT_PACK_EMOTIONS)]
            meta = await self._tag_slice(sp, profile=profile, emotion_slot=slot)
            thumb = await asyncio.to_thread(self._encode_thumb_data_url, sp, 180)
            entry: dict[str, Any] = {
                "index": idx,
                "path": sp,
                "emotion_label": slot[1],
                "preview_url": thumb,
                **meta,
                "imported": False,
                "hash": "",
                "error": "",
            }
            if not preview_only:
                try:
                    persisted = await self._import_slice(
                        entry,
                        scope_mode=clean_scope,
                        origin_target=clean_origin,
                    )
                    entry["imported"] = True
                    entry["hash"] = str(persisted.get("hash") or "")
                    imported_count += 1
                except Exception as exc:
                    entry["error"] = str(exc)
                    failed_count += 1
            staged_items.append(entry)

        if preview_only:
            async with self._lock:
                self._prune_staged()
                self._staged[pack_id] = {
                    "pack_id": pack_id,
                    "character": char_key,
                    "scope_mode": clean_scope,
                    "origin_target": clean_origin,
                    "work_dir": str(work_dir),
                    "created_at": time.time(),
                    "items": staged_items,
                }
        else:
            await asyncio.to_thread(shutil.rmtree, work_dir, True)

        public_items = [
            {k: v for k, v in item.items() if k != "path"}
            for item in staged_items
        ]
        return {
            "pack_id": pack_id,
            "task_id": task_id,
            "character": char_key,
            "character_name": str(profile.get("name") or char_key),
            "preview_only": bool(preview_only),
            "sheet_preview_url": sheet_preview,
            "total_slices": len(public_items),
            "imported_count": imported_count,
            "failed_count": failed_count,
            "scope_mode": clean_scope,
            "items": public_items,
        }

    async def commit_pack(
        self,
        *,
        pack_id: str,
        indices: list[int] | None = None,
        scope_mode: str | None = None,
        origin_target: str | None = None,
    ) -> dict[str, Any]:
        """Commit a previously previewed character sticker pack into the meme library."""
        pid = str(pack_id or "").strip()
        async with self._lock:
            self._prune_staged()
            staged = self._staged.get(pid)
        if not staged:
            raise ValueError("套图预览不存在或已过期，请重新生成")

        chosen = set(int(i) for i in indices) if isinstance(indices, list) and indices else None
        final_scope = (
            "local"
            if str(scope_mode or staged.get("scope_mode") or "public").strip().lower() == "local"
            else "public"
        )
        final_origin = str(
            origin_target if origin_target is not None else staged.get("origin_target", "")
        ).strip()
        if final_scope == "local" and not final_origin:
            final_scope = "public"

        imported = 0
        failed = 0
        results: list[dict[str, Any]] = []
        for item in staged["items"]:
            if chosen is not None and item["index"] not in chosen:
                continue
            if item.get("imported"):
                imported += 1
                results.append({"index": item["index"], "hash": item.get("hash", ""), "imported": True})
                continue
            try:
                persisted = await self._import_slice(
                    item,
                    scope_mode=final_scope,
                    origin_target=final_origin,
                )
                item["imported"] = True
                item["hash"] = str(persisted.get("hash") or "")
                imported += 1
                results.append(
                    {
                        "index": item["index"],
                        "hash": item["hash"],
                        "category": persisted.get("category", item["category"]),
                        "imported": True,
                    }
                )
            except Exception as exc:
                failed += 1
                results.append(
                    {"index": item["index"], "imported": False, "error": str(exc)}
                )

        async with self._lock:
            entry = self._staged.pop(pid, None)
        if entry and entry.get("work_dir"):
            await asyncio.to_thread(shutil.rmtree, entry["work_dir"], True)

        return {
            "pack_id": pid,
            "character": staged["character"],
            "imported_count": imported,
            "failed_count": failed,
            "scope_mode": final_scope,
            "results": results,
        }
