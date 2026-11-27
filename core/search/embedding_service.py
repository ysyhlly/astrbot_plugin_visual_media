"""封装 AstrBot 嵌入提供商，管理向量存储、维度校验、语义检索和批量回填。

向量存储优先使用 FaissVecDB，不可用时使用 SQLite 和 numpy。
"""

import asyncio
import hashlib
import json
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

from astrbot.api import logger
from astrbot.api.star import StarTools

from ..processing.semantic_schema import EMBEDDING_TEXT_VERSION, build_meme_search_text
from ..util.series_config import resolve_shared_provider
from .embedding_provider_adapter import (
    EmbeddingProviderAdapter,
    interactive_embedding_lane,
    precomputed_embeddings,
)


def _load_series_panels():
    """加载同插件根下的 series_panels，兼容包内相对导入与单测顶层导入。"""
    try:
        if __package__ and __package__.count(".") >= 2:
            from ... import series_panels

            return series_panels
    except Exception:
        pass
    try:
        import series_panels

        return series_panels
    except Exception:
        return None


class EmbeddingService:
    """嵌入向量服务。

    向量存储分为两层：
    1. 优先 FaissVecDB（astrbot.core.db.vec_db.faiss_impl.vec_db）
    2. 降级 SQLite emoji_embedding 表 + numpy 内存矩阵
    """

    # 回填参数
    BACKFILL_BATCH_SIZE = 20
    # 供应商故障时同一类 WARN 的抑制窗口（秒），避免欠费/额度不足时刷屏
    DEGRADED_LOG_INTERVAL_SECONDS = 300.0
    # 实时查询向量内存 LRU/TTL 缓存参数
    QUERY_CACHE_MAX_ENTRIES = 512
    QUERY_CACHE_TTL_SECONDS = 600.0

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._provider: Any | None = None
        self._provider_dim: int = 0

        # 缓存当前配置下未找到提供商的结果。
        self._provider_not_found: bool = False
        self._last_enable_embedding_search: bool | None = None
        self._last_embedding_provider_id: str | None = None

        # FaissVecDB
        self._faiss_db: Any | None = None
        self._faiss_available: bool | None = None  # None=未检测
        self._initialized_faiss = None
        self._faiss_init_lock = asyncio.Lock()

        # numpy 降级
        self._fallback_matrix: np.ndarray | None = None
        self._fallback_paths: list[str] = []
        self._fallback_dim: int = 0
        self._fallback_loaded: bool = False
        self._fallback_model_sig: str = ""
        self._observed_identity = ""
        self._observed_dim = 0
        self._provider_ready_lock = asyncio.Lock()
        self._retired_faiss = []
        self._faiss_operation_lock = asyncio.Lock()
        self._warning_log_at: dict[str, float] = {}
        self._initialized: bool = False
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._query_embedding_cache: OrderedDict[tuple[str, str], tuple[float, list[float]]] = OrderedDict()

    # ═══════════════════════════════════════════════════
    #  嵌入提供商查找
    # ═══════════════════════════════════════════════════

    def _embedding_enabled(self) -> bool:
        """读取插件配置中的嵌入检索开关。"""
        return getattr(self.plugin, "enable_embedding_search", False)

    def _resolve_embedding_provider_id(self) -> str:
        explicit = str(getattr(self.plugin, "embedding_provider_id", "") or "").strip()
        if explicit:
            return explicit
        return resolve_shared_provider(
            getattr(self.plugin, "context", None),
            "",
            keys=("embedding_provider",),
        )

    def _reset_provider_state_if_changed(self) -> None:
        """配置开关或 provider ID 变化时重置 provider 探测状态。"""
        current_enable = self._embedding_enabled()
        current_id = self._resolve_embedding_provider_id()
        if (
            self._last_enable_embedding_search != current_enable
            or self._last_embedding_provider_id != current_id
        ):
            self._provider = None
            self._provider_dim = 0
            self._provider_not_found = False
            self._initialized = False
            self._retire_faiss()
            self.invalidate_cache()
            self._last_enable_embedding_search = current_enable
            self._last_embedding_provider_id = current_id

    def _get_provider(self) -> Any | None:
        """获取 EmbeddingProvider（优先配置 ID -> 系列共享 embedding_provider -> 框架首个）。"""
        # 开关关闭时直接跳过，不触发 provider 探测与日志
        if not self._embedding_enabled():
            return None

        self._reset_provider_state_if_changed()

        if self._provider is not None:
            return self._provider

        provider_id = self._resolve_embedding_provider_id()

        # 1. 按配置的 ID（或系列共享 ID）查找嵌入提供商。
        if provider_id:
            provider = self._find_provider_by_id(provider_id)
            if provider is not None:
                # 类型校验：get_provider_by_id 返回的可能是 chat/stt/tts/embedding
                if not self._is_embedding_provider(provider):
                    logger.warning(
                        f"[Embedding] Provider '{provider_id}' 不是 EmbeddingProvider 类型，已忽略"
                    )
                else:
                    self._provider = provider
                    self._provider_dim = self._get_provider_dim(provider)
                    if self._provider_not_found or self._faiss_available is False:
                        self._faiss_available = None
                    self._provider_not_found = False
                    logger.info(
                        f"[Embedding] 使用指定 Provider: {provider_id} (dim={self._provider_dim})"
                    )
                    return self._provider

        # 2. 取框架首个 Embedding Provider
        try:
            ctx = getattr(self.plugin, "context", None)
            getter = getattr(ctx, "get_all_embedding_providers", None)
            providers = getter() if callable(getter) else []
        except Exception:
            providers = []

        if providers:
            self._provider = providers[0]
            self._provider_dim = self._get_provider_dim(self._provider)
            if self._provider_not_found or self._faiss_available is False:
                self._faiss_available = None
            self._provider_not_found = False
            pid = self._extract_provider_id(self._provider)
            logger.info(f"[Embedding] 自动选择首个 Provider: {pid} (dim={self._provider_dim})")
            return self._provider

        if not self._provider_not_found:
            logger.info("[Embedding] 未找到 Embedding Provider，嵌入检索暂未就绪，将在 Provider 就绪后自动重试")
        self._provider_not_found = True
        return None

    def _find_provider_by_id(self, provider_id: str) -> Any | None:
        """按 ID 静默查找提供商，查找失败时返回 None（避免冷启动刷 WARN）。"""
        ctx = getattr(self.plugin, "context", None)
        if ctx is None:
            return None

        # 优先使用 series_panels.find_provider_silently，避免冷启动抢跑调用 get_provider_by_id 刷 WARN
        sp = _load_series_panels()
        if sp is not None and callable(getattr(sp, "find_provider_silently", None)):
            try:
                return sp.find_provider_silently(ctx, provider_id)
            except Exception:
                pass

        # 兜底静默读取 provider_manager.inst_map
        try:
            pm = getattr(ctx, "provider_manager", None)
            inst_map = getattr(pm, "inst_map", None)
            if isinstance(inst_map, dict):
                return inst_map.get(provider_id)
        except Exception:
            pass

        return None

    @staticmethod
    def _is_embedding_provider(provider: Any) -> bool:
        """检查提供商类型；类型定义不可导入时检查 get_embedding 方法。"""
        try:
            from astrbot.core.provider.provider import EmbeddingProvider
            return isinstance(provider, EmbeddingProvider)
        except ImportError:
            # 无法导入时退化为鸭子类型检查：有 get_embedding 方法即可
            return hasattr(provider, "get_embedding")

    @staticmethod
    def _extract_provider_id(provider: Any) -> str:
        """从 provider 实例提取 ID 字符串。"""
        cfg = getattr(provider, "provider_config", {})
        if isinstance(cfg, dict):
            return cfg.get("id", "unknown")
        return getattr(cfg, "id", "unknown")

    @staticmethod
    def _get_provider_dim(provider: Any) -> int:
        """获取 provider 的输出维度。"""
        if hasattr(provider, "get_dim"):
            try:
                return max(0, int(provider.get_dim()))
            except Exception:
                pass
        return 0

    def _provider_identity(self, provider) -> str:
        model = provider.get_model() if callable(getattr(provider, "get_model", None)) else ""
        cfg = getattr(provider, "provider_config", {})
        endpoint = cfg.get("embedding_api_base", "") if isinstance(cfg, dict) else ""
        return json.dumps([type(provider).__module__, type(provider).__qualname__,
                           self._extract_provider_id(provider), model, endpoint,
                           self._get_provider_dim(provider)], ensure_ascii=False)

    def _effective_dim(self, provider) -> int:
        if self._provider_identity(provider) == self._observed_identity and self._observed_dim:
            return self._observed_dim
        return self._get_provider_dim(provider)

    def _record_dimension(self, provider, dimension: int) -> None:
        identity = self._provider_identity(provider)
        first = identity != self._observed_identity or not self._observed_dim
        changed = identity == self._observed_identity and self._observed_dim and self._observed_dim != dimension
        self._observed_identity, self._observed_dim = identity, dimension
        self._provider_dim = dimension
        if (first or changed) and self._get_provider_dim(provider) != dimension:
            logger.info(f"[Embedding] 使用模型实际维度 {dimension}（面板填写 {self._get_provider_dim(provider)}）")
        if changed:
            self._retire_faiss()
            self.invalidate_cache()
            db = getattr(self.plugin, "db_service", None)
            if db is not None:
                self._schedule_db_refresh(db)
            raise ValueError("嵌入输出维度已变化，已安排后台重建")

    def _schedule_db_refresh(self, db: Any) -> None:
        """Queue full search refresh off the event loop when a loop is running."""
        if db is None:
            return

        def _enqueue() -> None:
            try:
                db.enqueue_search_refresh(db.get_all_paths())
            except Exception as exc:
                logger.debug(f"[Embedding] 后台重建排队跳过: {exc}")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _enqueue()
            return
        task = loop.create_task(asyncio.to_thread(_enqueue))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def warning_rate_limited(self, key: str, message: str, interval: float | None = None) -> None:
        """同一 key 的 WARN 在窗口期内只输出一次，避免供应商故障时刷屏。"""
        now = time.monotonic()
        window = self.DEGRADED_LOG_INTERVAL_SECONDS if interval is None else interval
        last = self._warning_log_at.get(key)
        if last is not None and now - last < window:
            return
        self._warning_log_at[key] = now
        logger.warning(message)

    async def _close_retired_faiss(self) -> None:
        if not self._retired_faiss:
            return
        retired = list(self._retired_faiss)
        self._retired_faiss.clear()
        for client in retired:
            try:
                await client.close()
            except Exception:
                pass

    def _retire_faiss(self):
        if self._faiss_db is not None:
            self._retired_faiss.append(self._faiss_db)
        self._faiss_db = None
        self._faiss_available = None

    def _cleanup_stale_faiss_files(self, active_signature: str) -> int:
        """切换嵌入模型时清理旧签名遗留的 emoji_faiss_{old_sig}.* 索引文件。"""
        active_short = str(active_signature or "").split(":")[-1][:24]
        try:
            data_dir = Path(self._resolve_data_dir())
        except Exception as exc:
            logger.debug(f"[Embedding] 解析数据目录失败，跳过旧 Faiss 文件清理: {exc}")
            return 0
        if not data_dir.is_dir():
            return 0

        keep_prefix = f"emoji_faiss_{active_short}." if active_short else ""
        removed = 0
        for pattern in (
            "emoji_faiss_*.db",
            "emoji_faiss_*.db-wal",
            "emoji_faiss_*.db-shm",
            "emoji_faiss_*.index",
            "emoji_faiss.db",
            "emoji_faiss.db-wal",
            "emoji_faiss.db-shm",
            "emoji_faiss.index",
        ):
            for candidate in data_dir.glob(pattern):
                if not candidate.is_file():
                    continue
                if keep_prefix and candidate.name.startswith(keep_prefix):
                    continue
                try:
                    candidate.unlink()
                    removed += 1
                except OSError as exc:
                    logger.debug(f"[Embedding] 删除旧 Faiss 文件失败 {candidate}: {exc}")
        if removed > 0:
            logger.info(f"[Embedding] 模型切换已清理 {removed} 个旧 Faiss 索引文件")
        return removed

    async def _ensure_provider_ready(self):
        async with self._provider_ready_lock:
            provider = self._get_provider()
            if provider is None:
                return
            identity = self._provider_identity(provider)
            if identity == self._observed_identity and self._observed_dim:
                await self._close_retired_faiss()
                return
            self._retire_faiss()
            await self._close_retired_faiss()
            self.invalidate_cache()
            await EmbeddingProviderAdapter(self, provider).get_embedding("表情包")
            if self._provider_identity(provider) != identity:
                self._observed_identity = ""
                raise ValueError("嵌入模型在请求期间发生变化")
            signature = self._fallback_signature(provider)
            db = getattr(self.plugin, "db_service", None)
            if db is not None:
                previous = await asyncio.to_thread(db.get_meta_value, "embedding_active_signature")
                if previous != signature:
                    await asyncio.to_thread(self._cleanup_stale_faiss_files, signature)
                    all_paths = await asyncio.to_thread(db.get_all_paths)
                    await asyncio.to_thread(db.enqueue_search_refresh, all_paths)
                    await asyncio.to_thread(db.set_meta_value, "embedding_active_signature", signature)

    # ═══════════════════════════════════════════════════
    #  可用性
    # ═══════════════════════════════════════════════════

    def is_available(self) -> bool:
        """嵌入检索是否可用。"""
        if not getattr(self.plugin, "enable_embedding_search", False):
            return False

        if self._init_faiss():
            return True
        if self._init_fallback():
            return True
        return False

    # ═══════════════════════════════════════════════════
    #  FaissVecDB 向量存储
    # ═══════════════════════════════════════════════════

    def _init_faiss(self) -> bool:
        """初始化 FaissVecDB（Provider 冷启动尚未就绪时不永久置 False，允许后续自动恢复）。"""
        self._reset_provider_state_if_changed()
        if self._faiss_available is True and self._faiss_db is not None:
            return True
        if self._faiss_available is False and self._provider is not None and not self._provider_not_found:
            return False

        provider = self._get_provider()
        if provider is None:
            # 冷启动阶段 Provider 尚未挂载：保持 _faiss_available=None 以便就绪后自动恢复
            return False
        if self._provider_identity(provider) != self._observed_identity or not self._observed_dim:
            return False

        try:
            import faiss  # noqa: F401

            from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
        except ImportError:
            logger.info("[Embedding] faiss 未安装，降级 numpy")
            self._faiss_available = False
            return False

        try:
            data_dir = self._resolve_data_dir()
            signature = self._fallback_signature(provider).split(":")[-1][:24]
            db_path = f"{data_dir}/emoji_faiss_{signature}.db"
            index_path = f"{data_dir}/emoji_faiss_{signature}.index"
            needs_backfill = not Path(db_path).is_file() or not Path(index_path).is_file()

            self._faiss_db = FaissVecDB(db_path, index_path, EmbeddingProviderAdapter(self, provider))
            self._initialized_faiss = None
            if needs_backfill:
                db = getattr(self.plugin, "db_service", None)
                if db is not None:
                    self._schedule_db_refresh(db)
            self._faiss_available = True
            logger.info(f"[Embedding] FaissVecDB 就绪 (dim={self._provider_dim})")
            return True
        except Exception as e:
            logger.warning(f"[Embedding] FaissVecDB 初始化失败: {e}")
            self._faiss_available = False
            return False

    def _resolve_data_dir(self) -> str:
        """解析插件数据目录。"""
        directory = getattr(getattr(self.plugin, "plugin_config", None), "data_dir", None)
        directory = directory or getattr(self.plugin, "base_dir", None) or getattr(self.plugin, "data_dir", None)
        path = Path(directory or StarTools.get_data_dir("astrbot_plugin_visual_media"))
        if not path.is_absolute():
            raise ValueError("plugin data directory must be absolute")
        return str(path.resolve())

    def _fallback_signature(self, provider) -> str:
        model = provider.get_model() if callable(getattr(provider, "get_model", None)) else ""
        identity = [self._provider_identity(provider), model, self._effective_dim(provider), EMBEDDING_TEXT_VERSION]
        return "fallback:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _is_faiss_connection_error(exc: BaseException) -> bool:
        """Detect dropped/closed SQLite connections inside FaissVecDB document storage."""
        markers = (
            "no active connection",
            "connection closed",
            "connection is closed",
            "closed database",
            "cannot operate on a closed",
            "database is closed",
            "not connected",
        )
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            msg = str(current).lower()
            if any(marker in msg for marker in markers):
                return True
            if isinstance(current, (sqlite3.OperationalError, sqlite3.ProgrammingError)) and (
                "closed" in msg or "connection" in msg
            ):
                return True
            current = current.__cause__ or current.__context__
        return False

    @staticmethod
    def _faiss_connection_looks_closed(client: Any) -> bool:
        storage = getattr(client, "document_storage", None)
        if storage is None:
            return False
        for attr in ("conn", "_conn", "connection"):
            if hasattr(storage, attr) and getattr(storage, attr) is None:
                return True
        return False

    async def _ensure_faiss_initialized(self) -> None:
        """Reuse SDK initialization for clients created after startup/config changes,
        and re-initialize automatically if the underlying SQLite connection was dropped.
        """
        async with self._faiss_init_lock:
            client = self._faiss_db
            if client is None:
                return
            needs_init = self._initialized_faiss is not client or self._faiss_connection_looks_closed(client)
            if needs_init:
                await client.initialize()
                self._initialized_faiss = client

    async def _rebuild_faiss_client(self, *, reason: str = "") -> bool:
        """Retire a disconnected FaissVecDB client, recreate it, and re-run initialize()."""
        async with self._faiss_init_lock:
            old = self._faiss_db
            self._faiss_db = None
            self._initialized_faiss = None
            self._faiss_available = None
            if old is not None:
                self._retired_faiss.append(old)
            await self._close_retired_faiss()
            if not self._init_faiss() or self._faiss_db is None:
                return False
            try:
                await self._faiss_db.initialize()
                self._initialized_faiss = self._faiss_db
                logger.info(f"[Embedding] FaissVecDB 连接已自动重建恢复 ({reason or 'reconnect'})")
                return True
            except Exception as exc:
                self.warning_rate_limited(
                    "faiss_rebuild_failed",
                    f"[Embedding] FaissVecDB 连接重建失败: {exc}",
                )
                self._retire_faiss()
                return False

    async def _run_faiss_op(self, op_name: str, operation):
        """Run a FaissVecDB operation with automatic reconnect + single retry on closed SQLite connection."""
        last_exc: BaseException | None = None
        for attempt in range(2):
            await self._ensure_faiss_initialized()
            client = self._faiss_db
            if client is None:
                if attempt == 0 and await self._rebuild_faiss_client(reason=f"{op_name}:missing_client"):
                    client = self._faiss_db
                if client is None:
                    raise RuntimeError("FaissVecDB client unavailable")
            try:
                async with self._faiss_operation_lock:
                    if self._faiss_db is not client or self._faiss_db is None:
                        continue
                    return await operation(client)
            except Exception as exc:
                last_exc = exc
                if attempt == 0 and self._is_faiss_connection_error(exc):
                    self.warning_rate_limited(
                        f"faiss_reconnect:{op_name}",
                        f"[Embedding] FaissVecDB 连接断开（{op_name}: {exc}），自动重建连接并重试",
                    )
                    if await self._rebuild_faiss_client(reason=f"{op_name}:{exc}"):
                        continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"FaissVecDB operation '{op_name}' did not complete")

    # ═══════════════════════════════════════════════════
    #  numpy 降级（SQLite emoji_embedding 表）
    # ═══════════════════════════════════════════════════

    def _init_fallback(self) -> bool:
        """初始化 numpy 降级路径。"""
        provider = self._get_provider()
        if provider is None:
            return False
        self._load_fallback_matrix()
        return True  # provider 可用即就绪

    def _load_fallback_matrix(self) -> None:
        """从 SQLite emoji_embedding 表加载向量到内存矩阵。"""
        provider = self._get_provider()
        signature = self._fallback_signature(provider) if provider is not None else ""
        if self._fallback_loaded and self._fallback_model_sig == signature:
            return
        self._fallback_model_sig = signature

        self._fallback_matrix = None
        self._fallback_paths = []
        self._fallback_dim = 0

        db = getattr(self.plugin, "db_service", None)
        if not db or not hasattr(db, "load_embeddings_by_sig"):
            self._fallback_loaded = True
            return

        rows = db.load_embeddings_by_sig(signature) if signature else []

        if not rows:
            self._fallback_loaded = True
            return

        # 将数据库中的向量构建为 numpy 矩阵。
        vectors = []
        paths = []
        dim = self._effective_dim(provider) if provider is not None else 0
        for r in rows:
            blob = r.get("vector")
            path = r.get("path")
            if not blob or not path:
                continue
            try:
                vec = np.frombuffer(bytes(blob), dtype=np.float32)
                d = int(r.get("dim", 0))
                if d > 0 and len(vec) == d and (not dim or d == dim) and np.isfinite(vec).all():
                    vectors.append(vec)
                    paths.append(str(path))
                    dim = d
            except Exception:
                continue

        if vectors:
            mat = np.stack(vectors, axis=0)
            # 对向量执行 L2 归一化，以点积计算余弦相似度。
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1.0, norms)
            self._fallback_matrix = mat / norms
            self._fallback_paths = paths
            self._fallback_dim = dim
            logger.info(f"[Embedding] numpy 降级矩阵已加载: {len(paths)} 条 (dim={dim})")
        else:
            logger.debug("[Embedding] numpy 降级矩阵为空（无有效向量）")

        self._fallback_loaded = True

    def _check_fallback_dimension(self) -> bool:
        """检查降级矩阵维度是否与提供商匹配，不匹配时清空矩阵并等待回填。
        """
        if self._fallback_dim == 0 or self._provider_dim == 0:
            return True  # 无法判断，继续使用
        if self._fallback_dim != self._provider_dim:
            logger.warning(
                f"[Embedding] 维度不匹配: 已存={self._fallback_dim}, "
                f"provider={self._provider_dim}。当前矩阵停用，等待回填。"
            )
            self._fallback_loaded = False
            self._fallback_matrix = None
            self._fallback_paths = []
            self._fallback_dim = 0
            return False
        return True

    # ═══════════════════════════════════════════════════
    #  初始化
    # ═══════════════════════════════════════════════════

    async def initialize(self) -> None:
        """异步初始化嵌入提供商及其向量存储。"""
        # 开关关闭时完全跳过，不探测 provider、不输出日志
        if not self._embedding_enabled():
            return

        await self._upgrade_corpus_if_needed()

        # 1. Provider 就绪检查（先静默探测，避免冷启动反复探测与报错）
        provider = self._get_provider()
        if provider is None:
            logger.info(
                "[Embedding] 未就绪 — 请在 AstrBot 后台配置 Embedding 模型（或等待 Provider 加载完成后自动恢复）"
            )
            return

        try:
            await self._ensure_provider_ready()
        except Exception as exc:
            self.warning_rate_limited(
                "init_provider_probe",
                f"[Embedding] 冷启动 Provider 探测未就绪，将在后续调用时自动重试: {exc}",
            )
            return

        # 2. 尝试 FaissVecDB
        if self._init_faiss() and self._faiss_db is not None:
            try:
                await self._ensure_faiss_initialized()
                logger.info("[Embedding] FaissVecDB 初始化完成 ✅")
            except Exception as e:
                logger.warning(f"[Embedding] FaissVecDB 初始化失败，降级: {e}")
                self._retire_faiss()
                self._faiss_available = False

        # 3. 降级 numpy
        if not self._faiss_available or self._faiss_available is None:
            if self._init_fallback():
                self._check_fallback_dimension()
                count = len(self._fallback_paths)
                logger.info(
                    f"[Embedding] numpy 降级方案就绪 ✅ ({count} 条向量, dim={self._fallback_dim})"
                    if count > 0 else
                    "[Embedding] numpy 降级方案就绪 ✅ — 向量库为空，新入库自动填充"
                )

        self._initialized = bool(self._faiss_available or self._fallback_loaded)

    # ═══════════════════════════════════════════════════
    #  插入 / 删除
    # ═══════════════════════════════════════════════════

    def _category_info(self) -> dict[str, Any]:
        cfg = getattr(self.plugin, "plugin_config", None)
        info = getattr(cfg, "category_info", None) if cfg else None
        return info if isinstance(info, dict) else {}

    def _character_info(self) -> dict[str, Any]:
        cfg = getattr(self.plugin, "plugin_config", None)
        info = getattr(cfg, "character_info", None) if cfg else None
        return info if isinstance(info, dict) else {}

    def _build_search_text(self, entry: dict[str, Any]) -> str:
        """拼接嵌入文本：图上文字 + 角色 + 使用句 + 描述 + 多情绪。"""
        return build_meme_search_text(
            entry,
            category_info=self._category_info(),
            character_info=self._character_info(),
        )

    async def insert_emoji(self, path: str, entry: dict[str, Any]) -> bool:
        """计算并存储单张表情包的文本向量。

        失败不阻塞入库流程。
        """
        # 开关关闭时跳过写入，避免触发 provider 探测
        if not self._embedding_enabled():
            return False
        try:
            await self._ensure_provider_ready()
        except Exception as exc:
            self.warning_rate_limited("probe", f"[Embedding] 模型检测等待重试: {exc}")
            return False

        text = self._build_search_text(entry).strip()
        if not text:
            return False

        # 将待嵌入文本截断到 4000 字符。
        if len(text) > 4000:
            text = text[:4000]

        # FaissVecDB 路径（先在锁外完成远程嵌入与限流等待，仅在内存/SQLite 临界区持锁，断连自动重连）
        if self._init_faiss() and self._faiss_db is not None:
            try:
                precomputed: dict[str, list[float]] | None = None
                provider = self._get_provider()
                if provider is not None:
                    vec = await EmbeddingProviderAdapter(self, provider).get_embedding(text)
                    if not vec:
                        return False
                    precomputed = {text: vec}
                with precomputed_embeddings(precomputed):
                    ids = await self._run_faiss_op(
                        "insert_emoji",
                        lambda client: client.insert_batch(
                            contents=[text],
                            metadatas=[{"path": path, "category": str(entry.get("category", ""))}],
                            tasks_limit=1,
                        ),
                    )
                return len(ids) == 1
            except Exception as e:
                logger.debug(f"[Embedding] FaissVecDB 插入失败: {e}")
                return False

        # numpy 降级
        return await self._fallback_insert(path, text)

    async def delete_by_path(self, path: str) -> bool:
        """按 path 删除向量。"""
        await self._ensure_provider_ready()
        if self._init_faiss() and self._faiss_db is not None:
            await self._run_faiss_op(
                "delete_by_path",
                lambda client: client.delete_documents(metadata_filters={"path": path}),
            )
            return True

        db = getattr(self.plugin, "db_service", None)
        if db and hasattr(db, "delete_embedding"):
            await asyncio.to_thread(db.delete_embedding, path)
            if self._fallback_loaded:
                key = str(path)
                if self._fallback_paths and key in self._fallback_paths:
                    idx = self._fallback_paths.index(key)
                    self._fallback_paths.pop(idx)
                    if not self._fallback_paths:
                        self._fallback_matrix = None
                        self._fallback_dim = 0
                    elif self._fallback_matrix is not None:
                        self._fallback_matrix = np.delete(self._fallback_matrix, idx, axis=0)
            return True
        return False

    async def _fallback_insert(self, path: str, text: str) -> bool:
        """降级：get_embedding → upsert_embedding。"""
        provider = self._get_provider()
        if provider is None:
            return False
        identity = self._provider_identity(provider)

        try:
            vec = await EmbeddingProviderAdapter(self, provider).get_embedding(text)
            if not vec or len(vec) == 0:
                return False
        except Exception as e:
            logger.debug(f"[Embedding] get_embedding 失败: {e}")
            return False

        db = getattr(self.plugin, "db_service", None)
        if not db or not hasattr(db, "upsert_embedding"):
            return False

        try:
            if identity != self._provider_identity(provider):
                return False
            signature = self._fallback_signature(provider)
            values = np.asarray(vec, dtype=np.float32)
            expected = self._effective_dim(provider)
            if values.ndim != 1 or not np.isfinite(values).all() or (expected and len(values) != expected):
                return False
            await asyncio.to_thread(db.upsert_embedding, path, values.tobytes(),
                                    dim=len(values), model_sig=signature)
            if self._fallback_loaded and self._fallback_model_sig == signature:
                norm = float(np.linalg.norm(values))
                unit = values / (norm if norm != 0.0 else 1.0)
                key = str(path)
                if self._fallback_matrix is None or not self._fallback_paths:
                    self._fallback_matrix = unit.reshape(1, -1)
                    self._fallback_paths = [key]
                    self._fallback_dim = len(values)
                elif self._fallback_matrix.shape[1] == len(values):
                    if key in self._fallback_paths:
                        self._fallback_matrix[self._fallback_paths.index(key)] = unit
                    else:
                        self._fallback_matrix = np.vstack([self._fallback_matrix, unit.reshape(1, -1)])
                        self._fallback_paths.append(key)
                else:
                    self._fallback_loaded = False
            else:
                self._fallback_loaded = False  # 模型签名变化或尚未加载时触发重载
            return True
        except Exception as e:
            logger.debug(f"[Embedding] upsert_embedding 失败: {e}")
            return False

    # ═══════════════════════════════════════════════════
    #  检索 & 查询向量缓存
    # ═══════════════════════════════════════════════════

    @staticmethod
    def normalize_query_text(query: str) -> str:
        """规范化查询文本，折叠首尾与连续空白以提升查询向量缓存命中率。"""
        return " ".join(str(query or "").strip().split())

    def _query_cache_key(self, provider: Any, query: str) -> tuple[str, str] | None:
        normalized = self.normalize_query_text(query)
        if not normalized:
            return None
        return (self._fallback_signature(provider), normalized)

    def get_cached_query_embedding(self, provider: Any, query: str) -> list[float] | None:
        """读取未过期的查询向量缓存并刷新 LRU 顺序。"""
        key = self._query_cache_key(provider, query)
        if key is None:
            return None
        entry = self._query_embedding_cache.get(key)
        if entry is None:
            return None
        expires_at, vector = entry
        if time.monotonic() >= expires_at:
            self._query_embedding_cache.pop(key, None)
            return None
        self._query_embedding_cache.move_to_end(key)
        return list(vector)

    def set_cached_query_embedding(
        self, provider: Any, query: str, vector: list[float]
    ) -> None:
        """写入查询向量 LRU/TTL 缓存。"""
        key = self._query_cache_key(provider, query)
        if key is None or not vector:
            return
        max_entries = max(1, int(self.QUERY_CACHE_MAX_ENTRIES))
        ttl = max(0.0, float(self.QUERY_CACHE_TTL_SECONDS))
        if ttl <= 0:
            return
        self._query_embedding_cache[key] = (time.monotonic() + ttl, list(vector))
        self._query_embedding_cache.move_to_end(key)
        while len(self._query_embedding_cache) > max_entries:
            self._query_embedding_cache.popitem(last=False)

    async def search(self, query: str, k: int = 80) -> list[tuple[str, float]]:
        """按向量相似度检索前 K 张表情包。

        Returns:
            [(path, similarity_score), ...]  按相似度降序
        """
        if not query or not query.strip():
            return []
        with interactive_embedding_lane(True):
            try:
                await self._ensure_provider_ready()
            except Exception:
                return []

            # 将查询文本截断到 2000 字符。
            processed = query[:2000] if len(query) > 2000 else query

            # FaissVecDB 路径（远程查询嵌入在锁外执行；失败不二次请求，Faiss 失败时复用已算向量）
            precomputed_query_vec: list[float] | None = None
            if self._init_faiss() and self._faiss_db is not None:
                provider = self._get_provider()
                if provider is not None:
                    try:
                        precomputed_query_vec = await EmbeddingProviderAdapter(
                            self, provider, interactive=True
                        ).get_embedding(processed)
                        if not precomputed_query_vec:
                            return []
                    except Exception as e:
                        self.warning_rate_limited(
                            "query_embed", f"[Embedding] 查询向量计算失败: {e}"
                        )
                        return []
                try:
                    precomputed = (
                        {processed: precomputed_query_vec}
                        if precomputed_query_vec is not None
                        else None
                    )
                    with precomputed_embeddings(precomputed):
                        results = await self._run_faiss_op(
                            "retrieve",
                            lambda client: client.retrieve(
                                query=processed, k=k, fetch_k=k * 2, rerank=False
                            ),
                        )
                    out: list[tuple[str, float]] = []
                    for r in results:
                        data = getattr(r, "data", None)
                        if data is None:
                            continue
                        # FaissVecDB 返回的 data 是 {"id": int, "text": str, "metadata": dict}
                        if isinstance(data, dict):
                            meta = data.get("metadata", {})
                            if isinstance(meta, dict):
                                p = meta.get("path", "")
                                if p:
                                    out.append((p, float(r.similarity)))
                            elif isinstance(meta, str):
                                # metadata 被序列化成了 JSON 字符串
                                try:
                                    meta_dict = json.loads(meta)
                                    p = meta_dict.get("path", "")
                                    if p:
                                        out.append((p, float(r.similarity)))
                                except (json.JSONDecodeError, TypeError):
                                    pass
                        elif isinstance(data, str):
                            # data 本身是 JSON 字符串
                            try:
                                data_dict = json.loads(data)
                                meta = data_dict.get("metadata", {})
                                if isinstance(meta, dict):
                                    p = meta.get("path", "")
                                elif isinstance(meta, str):
                                    meta = json.loads(meta)
                                    p = meta.get("path", "") if isinstance(meta, dict) else ""
                                if p:
                                    out.append((p, float(r.similarity)))
                            except (json.JSONDecodeError, TypeError):
                                pass
                    return out
                except Exception as e:
                    self.warning_rate_limited("faiss_retrieve", f"[Embedding] FaissVecDB 检索失败: {e}")

            # numpy 降级（已算好的查询向量直接复用，不触发二次远程嵌入）
            return await self._fallback_search(
                processed, k, query_vector=precomputed_query_vec
            )

    async def _fallback_search(
        self,
        query: str,
        k: int,
        *,
        query_vector: list[float] | None = None,
    ) -> list[tuple[str, float]]:
        """降级：numpy 余弦相似度检索。"""
        try:
            self._load_fallback_matrix()
            if self._fallback_matrix is None or len(self._fallback_paths) == 0:
                return []

            provider = self._get_provider()
            if provider is None:
                return []

            # 优先复用 Faiss 路径已算好的查询向量，避免二次远程嵌入请求
            if query_vector is not None:
                vec = query_vector
                if not vec or self._fallback_model_sig != self._fallback_signature(provider):
                    return []
            else:
                try:
                    vec = await EmbeddingProviderAdapter(
                        self, provider, interactive=True
                    ).get_embedding(query)
                    if not vec:
                        return []
                    if self._fallback_model_sig != self._fallback_signature(provider):
                        return []
                except Exception:
                    return []

            # 余弦相似度
            qv = np.array(vec, dtype=np.float32)
            if qv.ndim != 1 or not np.isfinite(qv).all():
                return []
            q_norm = np.linalg.norm(qv)
            if q_norm == 0:
                return []
            qv = qv / q_norm

            # 维度不匹配时停用当前矩阵，保留其他模型的持久数据。
            if self._fallback_matrix.shape[1] != len(qv):
                logger.warning(
                    f"[Embedding] 维度不匹配: matrix={self._fallback_matrix.shape[1]}, "
                    f"query={len(qv)}。停用当前矩阵，等待回填。"
                )
                self._fallback_loaded = False
                self._fallback_matrix = None
                self._fallback_paths = []
                self._fallback_dim = 0
                return []

            scores = self._fallback_matrix @ qv
            if k <= 0 or len(scores) == 0:
                return []
            if len(scores) > k:
                cand_idx = np.argpartition(scores, -k)[-k:]
                top_idx = cand_idx[np.argsort(scores[cand_idx])[::-1]]
            else:
                top_idx = np.argsort(scores)[::-1]

            results: list[tuple[str, float]] = []
            for idx in top_idx:
                s = float(scores[idx])
                if s < 0.15:  # 低相似度截断
                    continue
                results.append((self._fallback_paths[int(idx)], s))
            return results
        except Exception as e:
            logger.warning(f"[Embedding] numpy 搜索异常: {e}")
            return []

    def invalidate_cache(self) -> None:
        """标记缓存过期。"""
        self._fallback_loaded = False
        self._fallback_matrix = None
        self._fallback_paths = []
        self._query_embedding_cache.clear()

    # ═══════════════════════════════════════════════════
    #  缺失向量批量回填
    # ═══════════════════════════════════════════════════

    async def backfill_existing(self, batch_size: int | None = None) -> int:
        """Reuse FaissVecDB bulk embedding, retries and storage APIs."""
        if not self._embedding_enabled():
            return 0
        await self._ensure_provider_ready()
        db = getattr(self.plugin, "db_service", None)
        if db is None:
            return 0
        batch_size = max(1, int(batch_size or self.BACKFILL_BATCH_SIZE))
        using_faiss = self._init_faiss() and self._faiss_db is not None
        if using_faiss:
            docs = await self._run_faiss_op(
                "backfill_list",
                lambda client: client.document_storage.get_documents(
                    metadata_filters={}, limit=None, offset=None
                ),
            )
            embedded = set()
            for doc in docs:
                metadata = doc.get("metadata") or {}
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                if isinstance(metadata, dict) and metadata.get("path"):
                    embedded.add(metadata["path"])
        else:
            provider = self._get_provider()
            if provider is None:
                return 0
            embedded = set(await asyncio.to_thread(db.get_all_embedding_paths,
                                                  model_sig=self._fallback_signature(provider)))
        missing = [path for path in await asyncio.to_thread(db.get_all_paths) if path not in embedded]
        written = 0
        for start in range(0, len(missing), batch_size):
            batch = missing[start:start + batch_size]
            generations = await asyncio.to_thread(db.get_search_refresh_generations, batch)
            entries = await asyncio.to_thread(db.get_entries, batch)
            all_items = [
                (path, entry, self._build_search_text(entry).strip()[:4000])
                for path, entry in entries.items()
            ]
            skipped_empty = [path for path, _entry, text in all_items if not text]
            items = [item for item in all_items if item[2]]
            successful = []
            if using_faiss and items:
                contents = [text for _, _, text in items]
                precomputed: dict[str, list[float]] | None = None
                insert_started = False
                provider = self._get_provider()
                try:
                    if provider is not None:
                        vectors = await EmbeddingProviderAdapter(
                            self, provider
                        ).get_embeddings_batch(
                            contents, batch_size=batch_size, tasks_limit=1
                        )
                        if len(vectors) != len(contents):
                            raise RuntimeError("incomplete embedding batch")
                        precomputed = dict(zip(contents, vectors))
                    insert_started = True
                    with precomputed_embeddings(precomputed):
                        ids = await self._run_faiss_op(
                            "backfill_insert",
                            lambda client: client.insert_batch(
                                contents=contents,
                                metadatas=[{"path": path, "category": entry.get("category", "")} for path, entry, _ in items],
                                batch_size=batch_size, tasks_limit=1,
                            ),
                        )
                    if len(ids) != len(items):
                        raise RuntimeError("incomplete FaissVecDB batch")
                    successful = [path for path, _, _ in items]
                except Exception as exc:
                    failed_paths = [path for path, _, _ in items]
                    # Older SDK versions can leave partial documents after a failed batch.
                    if insert_started:
                        for path in failed_paths:
                            try:
                                await self._run_faiss_op(
                                    "backfill_cleanup",
                                    lambda client, p=path: client.delete_documents(metadata_filters={"path": p}),
                                )
                            except Exception:
                                pass
                    await asyncio.to_thread(db.enqueue_search_refresh, failed_paths)
                    logger.warning(f"[Embedding] Batch backfill queued for retry: {exc}")
            else:
                for path, _, text in items:
                    if await self._fallback_insert(path, text):
                        successful.append(path)
                    else:
                        await asyncio.to_thread(db.enqueue_search_refresh, [path])
            written += len(successful)
            for path in (*successful, *skipped_empty):
                if path in generations:
                    await asyncio.to_thread(db.finish_search_refresh, path, generations[path])
        self.invalidate_cache()
        logger.info(f"[Embedding] Backfill completed: {written}/{len(missing)}")
        return written

    async def _upgrade_corpus_if_needed(self) -> None:
        """语料格式变更时清掉旧向量，让 backfill 按新文档重建。

        只动 SQLite/Faiss 索引文件，不加载本地视觉模型。
        """
        db = getattr(self.plugin, "db_service", None)
        if not db or not hasattr(db, "get_meta_value"):
            return
        try:
            current = str(db.get_meta_value("embedding_text_version") or "")
        except Exception:
            current = ""
        if current == EMBEDDING_TEXT_VERSION:
            return

        logger.info(
            f"[Embedding] 语料版本 {current or 'v1'} -> {EMBEDDING_TEXT_VERSION}，重建文本向量"
        )
        if self._faiss_db is not None:
            try:
                await self._faiss_db.close()
            except Exception:
                pass
            self._faiss_db = None
            self._faiss_available = None

        await asyncio.to_thread(self._cleanup_stale_faiss_files, "")

        if hasattr(db, "clear_all_embeddings"):
            try:
                db.clear_all_embeddings()
            except Exception as e:
                logger.debug(f"[Embedding] 清空 SQLite 向量失败: {e}")

        self.invalidate_cache()
        try:
            db.set_meta_value("embedding_text_version", EMBEDDING_TEXT_VERSION)
        except Exception as e:
            logger.debug(f"[Embedding] 写入语料版本失败: {e}")

    async def close(self) -> None:
        """关闭后台任务与 FaissVecDB 并重置状态。"""
        bg_tasks = getattr(self, "_bg_tasks", None)
        if bg_tasks:
            pending = [task for task in list(bg_tasks) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            bg_tasks.clear()
        for client in self._retired_faiss:
            try:
                await client.close()
            except Exception:
                pass
        self._retired_faiss.clear()
        if self._faiss_db is not None:
            try:
                await self._faiss_db.close()
            except Exception:
                pass
            self._faiss_db = None
            self._faiss_available = False
        self._initialized = False
