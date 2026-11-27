"""MaintenanceRoutes methods for PluginAPI."""

import os
from pathlib import Path

from astrbot.api import logger
from quart import jsonify, request

from ..core.db.index_manager import delete_index_paths
from ..core.processing.phash_dedup_service import PHashDedupService
from ..core.util.safe_io import safe_remove_file


class MaintenanceRoutes:
    # ── Repair / Storage maintenance ─────────────────────────

    async def handle_scope_repair(self):
        try:
            data = await request.get_json() or {}
            origin_target = str(data.get("origin_target", "") or "").strip()
            if not origin_target:
                return jsonify({"success": False, "error": "缺少 origin_target"})

            hashes_raw = data.get("hashes", [])
            hashes = {str(h).strip() for h in hashes_raw if str(h).strip()}
            scope_mode = self._norm_scope(data.get("scope_mode", "local"))
            only_missing = str(data.get("only_missing", "true")).lower() != "false"

            updated = 0
            skipped = 0
            index = self._get_index()
            for path, meta in index.items():
                if not isinstance(meta, dict):
                    continue
                img_hash = str(meta.get("hash", "") or "")
                if hashes and img_hash not in hashes:
                    continue
                if only_missing and str(meta.get("origin_target", "") or "").strip():
                    skipped += 1
                    continue
                updates = {"origin_target": origin_target}
                if scope_mode:
                    updates["scope_mode"] = scope_mode
                if await self._update_index_path(path, updates):
                    updated += 1

            return jsonify({"success": True, "count": updated, "skipped": skipped})
        except Exception as e:
            logger.error(f"作用域来源修复失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_storage_scan(self):
        try:
            return jsonify(self._build_storage_report())
        except Exception as e:
            logger.error(f"存储扫描失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_storage_cleanup(self):
        try:
            data = await request.get_json() or {}
            report = self._build_storage_report(include_items=True)
            sections_raw = data.get("sections")
            if isinstance(sections_raw, list):
                sections = {str(section) for section in sections_raw}
            else:
                sections = {
                    key
                    for key in ("stale_index", "orphan_files", "thumb_cache", "temp_files", "raw_files")
                    if str(data.get(key, "false")).lower() == "true"
                }

            if not sections:
                strategy = str(
                    data.get(
                        "strategy",
                        getattr(self.plugin, "storage_cleanup_strategy", "balanced"),
                    )
                    or "balanced"
                )
                if strategy == "conservative":
                    sections = {"stale_index", "temp_files"}
                elif strategy == "aggressive":
                    sections = {
                        "stale_index",
                        "orphan_files",
                        "thumb_cache",
                        "temp_files",
                        "raw_files",
                    }
                else:
                    sections = {"stale_index", "orphan_files", "thumb_cache", "temp_files"}

            removed: dict[str, int] = {}
            if "stale_index" in sections:
                index = self._get_index()
                stale_paths: list[str] = []
                for path in index.keys():
                    if not isinstance(path, str):
                        continue
                    # stale_index 语义：物理文件已丢失但索引条目仍存在。
                    # 删除文件已不存在的索引条目。
                    try:
                        file_exists = Path(path).resolve().is_file()
                    except Exception:
                        file_exists = Path(path).is_file()
                    if not file_exists:
                        stale_paths.append(path)
                if stale_paths:
                    await delete_index_paths(self.plugin, stale_paths)
                removed["stale_index"] = len(stale_paths)

            for key in ("orphan_files", "thumb_cache", "temp_files", "raw_files"):
                if key not in sections:
                    continue
                removed[key] = await self._remove_report_files(
                    report.get(key, {}).get("items", [])
                )

            return jsonify(
                {
                    "success": True,
                    "removed": removed,
                    "report": self._build_storage_report(),
                }
            )
        except Exception as e:
            logger.error(f"存储清理失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    # ── VLM Analyze ───────────────────────────────────────────

    async def handle_analyze_image(self):
        tmp_file_to_cleanup = None
        try:
            proc = getattr(self.plugin, "image_processor_service", None)
            if not proc:
                return jsonify({"success": False, "error": "图片处理服务不可用"})

            data = await request.get_json() or {}
            img_hash = (data.get("hash", "") or "").strip()
            img_base64 = (data.get("base64", "") or "").strip()

            file_path = None

            # 优先通过 hash 从索引查找文件路径
            if img_hash:
                found = self._find_index_entry_by_hash(img_hash)
                file_path = found[0] if found else None
                if not file_path or not os.path.isfile(file_path):
                    file_path = None

            # hash 查不到或未提供 hash 时，回退到 base64 方式
            if not file_path and img_base64:
                import tempfile

                file_content = self._decode_base64_payload(img_base64)
                if file_content is None:
                    return jsonify({"success": False, "error": "图片数据无效"})
                ext = ".png"
                if img_base64.startswith("data:image/jpeg") or img_base64.startswith("data:image/jpg"):
                    ext = ".jpg"
                elif img_base64.startswith("data:image/gif"):
                    ext = ".gif"
                elif img_base64.startswith("data:image/webp"):
                    ext = ".webp"

                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                tmp.write(file_content)
                tmp.close()
                file_path = tmp.name
                tmp_file_to_cleanup = file_path

            if not file_path:
                return jsonify({"success": False, "error": "缺少 hash 或 base64 图片数据"})

            cat, tags, desc, _, scenes, overlay_text, emotions = await proc.classify_image(
                event=None,
                file_path=file_path,
                categories=list(self._cfg.categories or []),
                content_filtration=False,
            )
            if cat == getattr(proc, "CATEGORY_FILTERED", None):
                return jsonify({"success": False, "error": "图片内容审核不通过"})
            if not cat:
                return jsonify({"success": False, "error": "无法识别图片分类"})

            return jsonify(
                {
                    "success": True,
                    "category": cat,
                    "tags": tags,
                    "description": desc,
                    "scenes": scenes or [],
                    "overlay_text": overlay_text or "",
                    "emotions": emotions or [],
                }
            )
        except Exception as e:
            logger.error(f"VLM分析失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": f"分析失败: {e}"})
        finally:
            if tmp_file_to_cleanup:
                try:
                    os.unlink(tmp_file_to_cleanup)
                except Exception:
                    pass

    # ── pHash Similar-Meme Dedup ──────────────────────────────

    async def handle_dedup_clusters(self):
        """GET /dedup/clusters —— 扫描图库相似图并返回聚类结果。"""
        try:
            raw_threshold = request.args.get(
                "threshold", str(PHashDedupService.PHASH_HAMMING_THRESHOLD)
            )
            try:
                threshold = max(0, min(64, int(raw_threshold)))
            except (TypeError, ValueError):
                threshold = PHashDedupService.PHASH_HAMMING_THRESHOLD
            compute_missing = str(request.args.get("compute_missing", "0")).lower() in {
                "1",
                "true",
                "yes",
            }

            index = self._get_index()
            phash_svc = getattr(self.plugin, "phash_dedup_service", None) or PHashDedupService(
                self.plugin
            )
            entries: list[dict] = []
            backfilled = 0
            for path, meta in list(index.items()):
                if not isinstance(meta, dict) or not meta.get("hash"):
                    continue
                item = dict(meta)
                if not item.get("phash") and compute_missing and backfilled < 200 and os.path.isfile(path):
                    computed = await phash_svc.compute_phash(path)
                    if computed:
                        item["phash"] = computed
                        backfilled += 1
                        await self._update_index_path(path, {"phash": computed})
                entries.append(item)

            clusters = PHashDedupService.cluster_similar_images(entries, threshold=threshold)
            duplicate_count = sum(max(0, c["size"] - 1) for c in clusters)
            return jsonify(
                {
                    "success": True,
                    "threshold": threshold,
                    "total_scanned": len(entries),
                    "backfilled_phash": backfilled,
                    "cluster_count": len(clusters),
                    "duplicate_count": duplicate_count,
                    "clusters": clusters,
                }
            )
        except Exception as e:
            logger.error(f"相似图聚类扫描失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_dedup_merge(self):
        """POST /dedup/merge —— 保留主图并合并元数据/删除重复图。"""
        try:
            data = await request.get_json() or {}
            keep_hash = str(data.get("keep_hash") or "").strip()
            raw_delete = data.get("delete_hashes") or []
            merge_metadata = str(data.get("merge_metadata", "true")).lower() != "false"
            if not keep_hash or not isinstance(raw_delete, list):
                return jsonify({"success": False, "error": "缺少 keep_hash 或 delete_hashes"})

            delete_hashes = {
                str(h).strip()
                for h in raw_delete
                if str(h or "").strip() and str(h).strip() != keep_hash
            }
            if not delete_hashes:
                return jsonify({"success": False, "error": "没有需要删除的重复项"})

            found_keep = self._find_index_entry_by_hash(keep_hash)
            if not found_keep:
                return jsonify({"success": False, "error": "要保留的主图不存在"})
            keep_path, keep_meta = found_keep

            index = self._get_index()
            delete_paths: list[str] = []
            merged_tags = list(keep_meta.get("tags") or [])
            merged_scenes = list(keep_meta.get("scenes") or [])
            merged_fav = bool(keep_meta.get("is_favorite"))
            extra_uses = 0

            for path, meta in list(index.items()):
                if not isinstance(meta, dict) or meta.get("hash") not in delete_hashes:
                    continue
                if path == keep_path:
                    continue
                delete_paths.append(path)
                if merge_metadata:
                    for tag in meta.get("tags") or []:
                        if tag and tag not in merged_tags:
                            merged_tags.append(tag)
                    for scene in meta.get("scenes") or []:
                        if scene and scene not in merged_scenes:
                            merged_scenes.append(scene)
                    if meta.get("is_favorite"):
                        merged_fav = True
                    try:
                        extra_uses += max(0, int(meta.get("use_count") or 0))
                    except (TypeError, ValueError):
                        pass

            if not delete_paths:
                return jsonify({"success": False, "error": "未找到可删除的重复图"})

            if merge_metadata:
                updates: dict = {
                    "tags": merged_tags,
                    "scenes": merged_scenes,
                }
                if merged_fav and not keep_meta.get("is_favorite"):
                    updates["is_favorite"] = 1
                if extra_uses > 0:
                    try:
                        base_uses = int(keep_meta.get("use_count") or 0)
                    except (TypeError, ValueError):
                        base_uses = 0
                    updates["use_count"] = base_uses + extra_uses
                await self._update_index_path(keep_path, updates)

            removed_paths: list[str] = []
            for target in delete_paths:
                try:
                    if not os.path.exists(target) or await safe_remove_file(target):
                        removed_paths.append(target)
                except Exception as exc:
                    logger.warning(f"去重删除文件失败 {target}: {exc}")

            if removed_paths:
                await delete_index_paths(self.plugin, removed_paths)
                proc = getattr(self.plugin, "image_processor_service", None)
                if proc is not None and callable(getattr(proc, "invalidate_cache", None)):
                    for h in delete_hashes:
                        proc.invalidate_cache(h)

            return jsonify(
                {
                    "success": True,
                    "kept": keep_hash,
                    "deleted": len(removed_paths),
                    "merged_metadata": merge_metadata,
                }
            )
        except Exception as e:
            logger.error(f"相似图合并去重失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})
