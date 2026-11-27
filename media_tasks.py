"""Unified generation/check/delivery cards and explicit, idempotent recovery."""

import asyncio
import json
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image
from astrbot.core.platform.message_session import MessageSession

from .tl.core.errors import redact_text

try:
    from . import series_panels as _series_panels
except Exception:  # pragma: no cover - standalone fallback
    try:
        import series_panels as _series_panels  # type: ignore[no-redef]
    except Exception:
        _series_panels = None  # type: ignore[assignment]

TERMINAL = {"succeeded", "partial_success", "failed", "interrupted"}


def _delivered(record):
    try:
        return int(record.get("delivered_count") or 0) > 0
    except (TypeError, ValueError, OverflowError):
        return False


class MediaTasks:
    def __init__(self, owner):
        self.owner = owner
        self.path = Path(owner.generation._plugin_data_dir) / "media_tasks.sqlite3"
        self.lock = asyncio.Lock()
        self.closed = False
        now = time.time()
        self._last_prune_at = 0.0
        with self._connect() as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS tasks (key TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations (token TEXT PRIMARY KEY, job TEXT, action TEXT, status TEXT, result TEXT, created REAL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_operations_created ON operations(created)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_job_id ON tasks(json_extract(data, '$.job_id'))"
            )
            db.execute(
                "UPDATE operations SET status='uncertain' WHERE status='running'"
            )
            for key, raw in db.execute("SELECT key,data FROM tasks").fetchall():
                data = json.loads(raw)
                if data.get("stage") in {"checking", "delivering"}:
                    data["stage"] = (
                        "delivery_uncertain"
                        if data["stage"] == "delivering"
                        else "check_unknown"
                    )
                    db.execute(
                        "UPDATE tasks SET data=? WHERE key=?", (json.dumps(data), key)
                    )
            self._prune_expired(db, now)

    @staticmethod
    def _prune_expired(db, now: float) -> None:
        cutoff = now - 90 * 86400
        db.execute("DELETE FROM operations WHERE created<?", (cutoff,))
        db.execute("DELETE FROM tasks WHERE updated<?", (cutoff,))

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("PRAGMA busy_timeout=5000")
            if _series_panels is not None:
                apply_fn = getattr(_series_panels, "apply_series_sqlite_pragmas", None)
                if callable(apply_fn):
                    try:
                        apply_fn(db, getattr(self.owner, "context", None), readonly=False)
                    except Exception:
                        pass
            yield db

    def _update(self, key, fields, defaults=False):
        now = time.time()
        mono = time.monotonic()
        with self._connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM tasks WHERE key=?", (key,)).fetchone()
            old = json.loads(row[0]) if row else {}
            data = {**fields, **old} if defaults else {**old, **fields}
            db.execute(
                "INSERT OR REPLACE INTO tasks VALUES (?,?,?)",
                (key, json.dumps(data, ensure_ascii=False), now),
            )
            if mono - getattr(self, "_last_prune_at", 0.0) >= 300.0:
                self._prune_expired(db, now)
                self._last_prune_at = mono
            return data

    async def update(self, key, fields, *, defaults=False):
        return await asyncio.to_thread(self._update, key, fields, defaults)

    async def observe(self, key, fields, *, defaults=False):
        """The task console must never prevent an already generated image being delivered."""
        try:
            return await self.update(key, fields, defaults=defaults)
        except (sqlite3.Error, OSError):
            logger.warning(
                "[VisualMedia] task display metadata unavailable; generation delivery continues"
            )

    async def close(self):
        self.closed = True
        async with self.lock:
            pass

    def _all(self):
        with self._connect() as db:
            return {
                key: {**json.loads(raw), "_updated": updated}
                for key, raw, updated in db.execute(
                    "SELECT key,data,updated FROM tasks"
                )
            }

    def _for_jobs(self, job_ids, task_ids=()):
        wanted_jobs = tuple(dict.fromkeys(str(j) for j in job_ids if j))
        wanted_tasks = tuple(dict.fromkeys(str(t) for t in task_ids if t))
        if not wanted_jobs and not wanted_tasks:
            return {}
        clauses = []
        params = []
        if wanted_jobs:
            job_keys = [f"job:{j}" for j in wanted_jobs]
            placeholders_keys = ",".join("?" for _ in job_keys)
            placeholders_jobs = ",".join("?" for _ in wanted_jobs)
            clauses.append(f"key IN ({placeholders_keys})")
            params.extend(job_keys)
            clauses.append(f"json_extract(data, '$.job_id') IN ({placeholders_jobs})")
            params.extend(wanted_jobs)
        if wanted_tasks:
            placeholders_tasks = ",".join("?" for _ in wanted_tasks)
            clauses.append(f"key IN ({placeholders_tasks})")
            params.extend(wanted_tasks)
        sql = "SELECT key,data,updated FROM tasks WHERE " + " OR ".join(clauses)
        with self._connect() as db:
            return {
                key: {**json.loads(raw), "_updated": updated}
                for key, raw, updated in db.execute(sql, params)
            }

    @staticmethod
    def _by_job(backgrounds, metadata):
        by_background = {r["job_id"]: r for r in backgrounds if r.get("job_id")}
        by_metadata = {}
        for key, row in metadata.items():
            if row.get("job_id"):
                by_metadata.setdefault(row["job_id"], {})[key] = row
        return by_background, by_metadata

    @staticmethod
    def _select(job_id, metadata, by_background, by_metadata):
        bg = by_background.get(job_id, {})
        generic_key = "job:" + job_id
        candidates = dict(by_metadata.get(job_id, {}))
        # Pre-existing queued role snapshots can still be linked by the live
        # background task before their completion callback adds the job ID.
        task_key = bg.get("task_id")
        if task_key in metadata:
            live = metadata[task_key]
            if live.get("job_id") in (None, "", job_id):
                candidates[task_key] = live
        generic = candidates.pop(generic_key, {})
        key = max(
            candidates,
            key=lambda k: (
                bool(candidates[k].get("profile")),
                k == task_key,
                candidates[k]["_updated"],
                k,
            ),
            default=generic_key,
        )
        merged = {**generic, **candidates.get(key, {})}
        if (
            _delivered(bg)
            or generic.get("stage") == "delivered"
            or any(row.get("stage") == "delivered" for row in candidates.values())
        ):
            merged["stage"] = "delivered"
            merged.pop("recovery_error", None)
        return bg, merged, key

    async def context(self, job_id):
        row = self.owner.generation.generation_tracker.get(job_id)
        if row is None:
            raise ValueError("任务不存在或已清理")
        backgrounds = (
            await self.owner.generation.background_task_manager.snapshot_records()
        )
        all_meta = await asyncio.to_thread(self._all)
        bg, merged, key = self._select(job_id, all_meta, *self._by_job(backgrounds, all_meta))
        return row, bg, merged, key

    def _card(self, row, bg, meta):
        tracker = self.owner.generation.generation_tracker
        names = row.get("images") or []
        available = [n for n in names if tracker.gallery_image_path(n)]
        target = meta.get("target") or (row.get("requester") or {}).get("umo") or ""
        target_valid = False
        target_allowed = True
        if target:
            try:
                session = MessageSession.from_str(target)
                target_valid = True
                group_id = (
                    session.session_id
                    if getattr(session.message_type, "value", "") == "GroupMessage"
                    else ""
                )
                target_allowed = self.owner.generation.rate_limiter.allows_group(
                    SimpleNamespace(group_id=group_id)
                )
            except (ValueError, TypeError):
                pass
        check = meta.get("check") or {}
        status = row.get("status", "running")
        stage = meta.get("stage")
        if (
            stage == "delivering"
            and time.time() - meta.get("_updated", time.time()) > 45
        ):
            stage = "delivery_uncertain"
        if stage in {"checking", "delivering"} and bg.get("callback_status") in {
            "failed",
            "interrupted",
        }:
            stage = "check_unknown" if stage == "checking" else "delivery_uncertain"
        if stage == "check_unknown" and not check:
            check = {
                "status": "unknown",
                "reason": "形象核对被中断，尚未确认一致。",
                "issues": [],
            }
        active = status not in TERMINAL
        if active:
            stage = "queued" if status == "queued" else "generating"
        elif stage not in {
            "checking",
            "delivering",
            "delivered",
            "delivery_failed",
            "delivery_uncertain",
            "check_unknown",
        }:
            if not names and (row.get("source_urls") or row.get("generated_images", 0)):
                stage = "unavailable"
            elif not names:
                stage = (
                    "generation_failed"
                    if status == "failed"
                    else "interrupted"
                    if status == "interrupted"
                    else "no_image"
                )
            elif not available:
                stage = "expired"
            elif (bg.get("status") == "partial_success" and names) or bg.get(
                "callback_status"
            ) in {"failed", "interrupted"}:
                stage = "delivery_failed" if target else "ready"
            elif bg.get("callback_status") in {"pending", "running"}:
                stage = "checking" if meta.get("character") else "delivering"
            elif target:
                stage = (
                    "awaiting_delivery"
                    if meta.get("manual_retry")
                    else "delivery_unknown"
                )
            else:
                stage = "ready"
        if stage == "check_unknown" and available:
            stage = "delivery_unknown" if target else "ready"
        role_legacy = (row.get("caller") or {}).get(
            "plugin_id"
        ) == self.owner.name and not meta.get("profile")
        retry_reason = ""
        if not target_allowed:
            retry_reason = "原会话当前已禁用图片生成，请先检查群访问配置。"
        elif names:
            retry_reason = "已有生成图片，请查看结果或重发已有图片。"
        elif row.get("source_urls") or row.get("generated_images", 0):
            retry_reason = "已有生成结果，但本地图片不可用，请到创作台检查原始记录。"
        elif active:
            retry_reason = "任务尚未结束。"
        elif meta.get("recovery_id"):
            retry_reason = "已创建恢复任务，请查看新任务。"
        elif meta.get("recovery_uncertain"):
            retry_reason = "上次提交结果未知，请先核对最新任务，避免重复生成。"
        elif role_legacy:
            retry_reason = "旧任务没有完整角色快照，请从角色页重新创作。"
        elif not meta.get("profile") and meta.get("input_complete") is not True:
            retry_reason = (
                "任务未保存完整可复用输入，请到创作台确认参数和参考图后生成。"
            )
        elif not meta.get("profile") and len(row.get("prompt") or "") >= 2000:
            retry_reason = "历史提示词可能不完整，请到创作台确认后生成。"
        elif (
            int(row.get("requested_images") or 1)
            > self.owner.generation.cfg.batch_max_images_per_task
        ):
            retry_reason = "批量任务请逐个恢复失败子任务。"
        return {
            "job_id": row["job_id"],
            "task_id": bg.get("task_id") or meta.get("task_id", ""),
            "prompt": str(meta.get("user_prompt") or row.get("prompt") or "")[:400],
            "source": row.get("source"),
            "character": meta.get("character", ""),
            "stage": stage,
            "needs_character_setup": role_legacy,
            "created_at": row.get("created_at"),
            "finished_at": row.get("finished_at"),
            "duration_ms": row.get("duration_ms", 0),
            "images": available,
            "missing_images": len(names) - len(available),
            "requested_images": row.get("requested_images", 1),
            "generated_images": row.get("generated_images", 0),
            "check": {
                k: check[k] for k in ("status", "reason", "issues") if k in check
            },
            "reference_hashes": list(
                (meta.get("profile") or {}).get("references") or []
            ),
            "error": redact_text(
                str(meta.get("recovery_error") or row.get("error") or "")
            )[:500],
            "target": self.target_label(target),
            "can_retry": status in {"failed", "interrupted", "partial_success"}
            and not retry_reason,
            "retry_reason": retry_reason,
            "send_reason": ""
            if target_allowed
            else "原会话当前已禁用图片生成，请先检查群访问配置。",
            "can_resend": bool(
                target_valid
                and target_allowed
                and available
                and len(available) == len(names)
                and not active
                and stage
                in {
                    "delivery_failed",
                    "delivery_uncertain",
                    "delivery_unknown",
                    "awaiting_delivery",
                }
            ),
            "recovery_id": meta.get("recovery_id", ""),
            "retry_of": meta.get("retry_of", ""),
        }

    @staticmethod
    def target_label(target):
        if not target:
            return "仅保存在工坊"
        parts = target.split(":", 2)
        if len(parts) != 3:
            return "原会话信息不完整"
        kind = {"GroupMessage": "群聊", "FriendMessage": "私聊"}.get(parts[1], "原会话")
        return f"{parts[0]} · {kind} · …{parts[2][-4:]}"

    async def listing(self, page=1, size=20):
        rows = self.owner.generation.generation_tracker.records_snapshot()
        rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
        page_rows = rows[(page - 1) * size : page * size]
        page_job_ids = [r["job_id"] for r in page_rows if r.get("job_id")]
        backgrounds = (
            await self.owner.generation.background_task_manager.snapshot_records()
        )
        bg_task_ids = [
            b["task_id"]
            for b in backgrounds
            if b.get("task_id") and b.get("job_id") in set(page_job_ids)
        ]
        meta = await asyncio.to_thread(self._for_jobs, page_job_ids, bg_task_ids)

        def project():
            by_background, by_metadata = self._by_job(backgrounds, meta)
            cards = []
            for row in page_rows:
                bg, extra, _ = self._select(
                    row["job_id"], meta, by_background, by_metadata
                )
                cards.append(self._card(row, bg, extra))
            return {"items": cards, "total": len(rows), "page": page, "size": size}

        return await asyncio.to_thread(project)

    async def detail(self, job_id):
        row, bg, meta, _ = await self.context(job_id)
        return await asyncio.to_thread(self._card, row, bg, meta)

    def _operation(self, token, job, action, finish=None):
        with self._connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT job,action,status,result FROM operations WHERE token=?",
                (token,),
            ).fetchone()
            if finish is not None:
                db.execute(
                    "UPDATE operations SET status=?,result=? WHERE token=?",
                    ("done", json.dumps(finish, ensure_ascii=False), token),
                )
                return finish
            if row:
                if row[0] != job or row[1] != action:
                    raise ValueError("操作标识与任务不匹配")
                if row[2] == "done":
                    return json.loads(row[3])
                raise ValueError(
                    "上次操作结果尚未确认，请刷新任务后核对，系统不会自动重复执行。"
                )
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?)",
                (token, job, action, "running", "{}", time.time()),
            )
            return None

    async def recover(self, job_id, action, token):
        if (
            not isinstance(action, str)
            or action not in {"retry", "resend"}
            or not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", token)
        ):
            raise ValueError("恢复操作参数无效")
        async with self.lock:
            if self.closed:
                raise ValueError("插件正在关闭，请稍后重试")
            # Once accepted, keep the lock until the effect and receipt settle.
            # The HTTP client can replay the token after disconnecting.
            work = asyncio.create_task(self._recover_locked(job_id, action, token))
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not work.cancelled() and work.exception() is not None:
                    logger.warning(
                        "[VisualMedia] dashboard recovery failed after request cancellation"
                    )
                raise

    async def _recover_locked(self, job_id, action, token):
        cached = await asyncio.to_thread(self._operation, token, job_id, action)
        if cached is not None:
            return cached
        submitted = False
        try:
            row, bg, meta, key = await self.context(job_id)
            card = await asyncio.to_thread(self._card, row, bg, meta)
            if not card["can_" + action]:
                raise ValueError(
                    card["retry_reason"]
                    if action == "retry"
                    else card["send_reason"] or "当前任务不支持发送恢复，请刷新状态。"
                )
            if action == "resend":
                paths = [
                    self.owner.generation.generation_tracker.gallery_image_path(n)
                    for n in row["images"]
                ]
                if not paths or any(p is None for p in paths):
                    raise ValueError("图片已清理，不能重发。")
                target = meta.get("target") or (row.get("requester") or {}).get("umo")
                session = MessageSession.from_str(target)
                await self.update(
                    key, {"job_id": job_id, "stage": "delivering", "recovery_error": ""}
                )
                try:
                    sent = await asyncio.wait_for(
                        self.owner.context.send_message(
                            session,
                            MessageChain([Image.fromFileSystem(str(p)) for p in paths]),
                        ),
                        timeout=30,
                    )
                    if not sent:
                        raise RuntimeError("找不到原会话的平台连接")
                except Exception:
                    await self.update(
                        key,
                        {
                            "stage": "delivery_uncertain",
                            "recovery_error": "平台未确认发送。图片保留，再次发送可能重复，请先核对原会话。",
                        },
                    )
                    raise ValueError(
                        "平台未确认发送，未重新生图。请核对原会话后再操作。"
                    ) from None
                await self.update(key, {"stage": "delivered", "recovery_error": ""})
                event = SimpleNamespace(
                    unified_msg_origin=target, get_sender_id=lambda: ""
                )
                await self.owner.behavior.arecord(
                    event,
                    "generate",
                    "delivered",
                    "dashboard_resend",
                    {
                        "task_id": bg.get("task_id") or meta.get("task_id", ""),
                        "job_id": job_id,
                        "image_count": len(paths),
                    },
                )
                result = {
                    "ok": True,
                    "job_id": job_id,
                    "message": "已有图片已发送到原会话，没有重新生图。",
                }
            elif meta.get("profile"):
                meta = {
                    **meta,
                    "retry_of": job_id,
                    "target": meta.get("target")
                    or (row.get("requester") or {}).get("umo", ""),
                    "requester": row.get("requester") or {},
                }
                if not isinstance(meta.get("prepared_prompt"), str) or not meta.get(
                    "user_prompt"
                ):
                    raise ValueError("原角色任务快照不完整，请到角色页确认后重新创作。")
                for ref in meta["profile"].get("references", []):
                    await asyncio.to_thread(self.owner.library.reference_path, ref)
                submitted = True
                accepted = await self.owner.library.generate_character(
                    meta["character"], meta["user_prompt"], _recovery=meta
                )
                task_id = accepted["task_id"]
                new = await self.owner.generation.get_service().get_task(
                    task_id, plugin_id=self.owner.name
                )
                new_id = new.get("job_id") or task_id
                await self.update(key, {"job_id": job_id, "recovery_id": new_id})
                result = {
                    "ok": True,
                    "job_id": new_id,
                    "task_id": task_id,
                    "message": "已按原角色档案重试，结果先保存在工坊。",
                }
            else:
                refs = row.get("reference_names") or []
                tracker = self.owner.generation.generation_tracker
                if any(tracker.gallery_image_path(n) is None for n in refs):
                    raise ValueError("原参考图已清理，请到创作台重新选择；未启动生成。")
                payload = {
                    "reuse_job_id": job_id,
                    "reference_names": refs,
                    "image_count": int(row.get("requested_images") or 1),
                }
                studio = self.owner.generation.web_studio_service
                _, warning = studio.validate_payload(payload)
                if warning:
                    raise ValueError(
                        "原参数已不可完整复用，请到创作台调整；未启动生成。"
                    )
                submitted = True
                accepted = await studio.generate(
                    payload, requester=row.get("requester") or {}
                )
                new_id = accepted["job_id"]
                await self.update(
                    "job:" + new_id,
                    {
                        "job_id": new_id,
                        "target": meta.get("target")
                        or (row.get("requester") or {}).get("umo", ""),
                        "manual_retry": True,
                        "retry_of": job_id,
                    },
                )
                await self.update(key, {"job_id": job_id, "recovery_id": new_id})
                result = {
                    "ok": True,
                    "job_id": new_id,
                    "message": "已按原参数重试，结果先保存在工坊。",
                }
        except Exception as exc:
            if submitted:
                await self.update(
                    key,
                    {
                        "recovery_uncertain": True,
                        "recovery_error": "提交结果未确认，请先核对最新任务；系统不会自动重复生成。",
                    },
                )
            result = {"ok": False, "message": redact_text(str(exc))[:500]}
        return await asyncio.to_thread(self._operation, token, job_id, action, result)
