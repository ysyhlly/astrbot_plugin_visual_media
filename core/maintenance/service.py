"""维护服务实现。"""

import asyncio
import os
import time
from typing import TYPE_CHECKING

from astrbot.api import logger

from ..db.index_manager import delete_index_paths
from ..util.normalization import canonicalize_path
from ..util.safe_io import safe_remove_file
from .retention import (
    DEFAULT_MAX_DISK_BYTES,
    DEFAULT_MAX_PENDING_DISK_BYTES,
    automatic_disk_bytes,
    library_counts,
    pending_eviction_candidates,
)

if TYPE_CHECKING:
    from ...main import Main  # noqa: F401


class MaintenanceService:
    """统一管理插件的维护任务：

    - 启动时：一次性孤儿扫描 + 遗留文件清理
    - 周期任务：raw 目录清理、容量控制
    """

    RAW_CLEANUP_INTERVAL_SECONDS = 30 * 60
    CAPACITY_CONTROL_INTERVAL_SECONDS = 60 * 60
    ORPHAN_GRACE_SECONDS = 5 * 60

    def __init__(self, plugin: "Main") -> None:
        self.plugin = plugin
        self._tasks: list[asyncio.Task] = []

    async def run_startup_cleanup(self) -> None:
        """启动阶段调用：执行一次性清理（遗留文件 + 孤儿扫描 + pending 容量控制）。"""
        await self._clean_legacy_files()
        await self._cleanup_orphans()
        await self._enforce_pending_disk_cap()

    def start_periodic_tasks(self) -> None:
        """注册并启动周期任务。"""
        scheduler = getattr(self.plugin, "task_scheduler", None)
        if scheduler is None:
            logger.warning("[Maintenance] task_scheduler 未初始化，跳过周期任务")
            return

        self._tasks.append(
            scheduler.create_task("raw_cleanup_loop", self._raw_cleanup_loop())
        )
        self._tasks.append(
            scheduler.create_task(
                "capacity_control_loop", self._capacity_control_loop()
            )
        )

    def cancel_all(self) -> None:
        """终止所有周期任务。"""
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    async def _raw_cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.RAW_CLEANUP_INTERVAL_SECONDS)
                handler = getattr(self.plugin, "event_handler", None)
                if handler:
                    await handler._clean_raw_directory()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"raw 清理循环出错: {e}")

    async def _capacity_control_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.CAPACITY_CONTROL_INTERVAL_SECONDS)
                idx = await self.plugin.index_manager.load_index()
                cfg = self.plugin.plugin_config
                max_disk_bytes = int(
                    getattr(cfg, "max_disk_bytes", DEFAULT_MAX_DISK_BYTES) or 0
                )
                over_count = library_counts(idx)["automatic"] > cfg.max_reg_num
                over_bytes = max_disk_bytes > 0 and (
                    await asyncio.to_thread(automatic_disk_bytes, idx) > max_disk_bytes
                )
                if over_count or over_bytes:
                    handler = getattr(self.plugin, "event_handler", None)
                    if handler:
                        await handler._enforce_capacity(idx)
                    await self.plugin.index_manager.save_index(idx)
                await self._enforce_pending_disk_cap()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"容量控制循环出错: {e}")

    def _collect_orphan_files_sync(
        self,
        categories_dir,
        db_paths: set[str],
        pending_dir,
        pending_paths: set[str],
    ) -> list[str]:
        """在工作线程中扫描 categories/ 与 pending/ 下的无索引孤儿文件。"""
        orphans: list[str] = []
        if categories_dir and os.path.isdir(categories_dir):
            for root, _dirs, files in os.walk(categories_dir):
                for name in files:
                    path = os.path.join(root, name)
                    if canonicalize_path(path) not in db_paths and not self._recent_publication(path):
                        orphans.append(path)
        if pending_dir and os.path.isdir(pending_dir):
            for name in os.listdir(pending_dir):
                path = os.path.join(pending_dir, name)
                if (
                    os.path.isfile(path)
                    and canonicalize_path(path) not in pending_paths
                    and not self._recent_publication(path)
                ):
                    orphans.append(path)
        return orphans

    async def _enforce_pending_disk_cap(self) -> int:
        """按字节上限淘汰最旧的 pending/ 待审核表情包，避免 pending 目录无界增长。"""
        try:
            db = getattr(self.plugin, "db_service", None)
            cfg = getattr(self.plugin, "plugin_config", None)
            if not db or not cfg:
                return 0
            raw_cap = getattr(cfg, "max_pending_disk_bytes", DEFAULT_MAX_PENDING_DISK_BYTES)
            try:
                max_pending_bytes = int(raw_cap) if raw_cap is not None else 0
            except (TypeError, ValueError):
                max_pending_bytes = DEFAULT_MAX_PENDING_DISK_BYTES
            if max_pending_bytes <= 0:
                return 0

            pending_rows = await asyncio.to_thread(db.get_pending_paginated, page=1, page_size=100000)
            pending = pending_rows[0] if pending_rows else []
            if not pending:
                return 0

            evict_rows = await asyncio.to_thread(
                pending_eviction_candidates, pending, max_pending_bytes
            )
            if not evict_rows:
                return 0

            evict_ids = [row["id"] for row in evict_rows if row.get("id") is not None]
            for row in evict_rows:
                path = str(row.get("path") or "")
                if path:
                    await safe_remove_file(path)
            if evict_ids:
                await asyncio.to_thread(db.delete_pending_batch, evict_ids)
            logger.info(
                f"[PendingCap] Evicted {len(evict_rows)} oldest pending items to stay under "
                f"{max_pending_bytes / (1024 * 1024):.1f} MB"
            )
            return len(evict_rows)
        except Exception as e:
            logger.debug(f"[PendingCap] pending 容量控制异常（不阻塞）: {e}")
            return 0

    async def _cleanup_orphans(self) -> None:
        """清理无文件索引 / 无索引文件（所有磁盘遍历均在工作线程中执行）。"""
        try:
            db = self.plugin.db_service
            if not db:
                return

            all_paths = await asyncio.to_thread(db.get_all_paths)
            stale_paths = await asyncio.to_thread(
                lambda: [path for path in all_paths if path and not os.path.isfile(str(path))]
            )
            if stale_paths:
                await delete_index_paths(self.plugin, stale_paths)
                logger.info(f"[Orphan] Removed {len(stale_paths)} missing library paths")

            pending_rows = await asyncio.to_thread(db.get_pending_paginated, page=1, page_size=100000)
            pending = pending_rows[0] if pending_rows else []
            stale_ids = await asyncio.to_thread(
                lambda: [
                    row["id"]
                    for row in pending
                    if row.get("path") and not os.path.isfile(str(row["path"]))
                ]
            )
            if stale_ids:
                await asyncio.to_thread(db.delete_pending_batch, stale_ids)
                logger.info(f"[Orphan] Removed {len(stale_ids)} missing pending paths")

            # Retain uncatalogued physical files when the main library is empty.
            if await asyncio.to_thread(db.count_total) == 0:
                return
            db_paths = {canonicalize_path(path) for path in all_paths}
            pending_paths = {canonicalize_path(row["path"]) for row in pending if row.get("path")}
            categories = self.plugin.plugin_config.categories_dir
            pending_dir = self.plugin.plugin_config.pending_dir
            orphan_files = await asyncio.to_thread(
                self._collect_orphan_files_sync,
                categories,
                db_paths,
                pending_dir,
                pending_paths,
            )
            for path in orphan_files:
                await safe_remove_file(path)
        except Exception as e:
            logger.debug(f"[Orphan] 孤儿扫描异常（不阻塞）: {e}")

    def _recent_publication(self, path: str) -> bool:
        try:
            return time.time() - os.stat(path).st_ctime < self.ORPHAN_GRACE_SECONDS
        except OSError:
            return False

    async def _clean_legacy_files(self) -> None:
        """删除迁移残留文件：.backup / .migrated / index.json 等。"""
        try:
            db_count = self.plugin.db_service.count_total()
            if db_count <= 0:
                return
            keep_cache_names = {
                "image_cache.json",
                "text_cache.json",
                "bm25_cache.json",
                "desc_cache.json",
                "blacklist_cache.json",
            }
            deleted = 0

            cache_dir = self.plugin.cache_dir
            if cache_dir.is_dir():
                for child in cache_dir.iterdir():
                    name = child.name
                    if name in keep_cache_names:
                        continue
                    if child.is_dir():
                        continue
                    if name.endswith(".wal") or name.endswith(".shm") or name == "emoji.db":
                        continue
                    if (
                        name.endswith(".backup")
                        or name.endswith(".migrated")
                        or name in {"index_cache.json", "index.json"}
                    ):
                        if await safe_remove_file(str(child)):
                            deleted += 1

            categories_dir = self.plugin.plugin_config.categories_dir
            if categories_dir.is_dir():
                for cat_dir in categories_dir.iterdir():
                    if not cat_dir.is_dir():
                        continue
                    legacy_idx = cat_dir / "index.json"
                    if legacy_idx.is_file() and await safe_remove_file(str(legacy_idx)):
                        deleted += 1

            for name in ("index.json", "image_index.json"):
                candidate = self.plugin.base_dir / name
                if candidate.is_file() and await safe_remove_file(str(candidate)):
                    deleted += 1

            if deleted > 0:
                logger.info(f"[清理] 已删除 {deleted} 个遗留文件")
        except Exception as e:
            logger.warning(f"[清理] 遗留文件删除失败: {e}")
