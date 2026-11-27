"""Group crowd-curation service (/meme 审图): pull pending meme & vote to approve/reject."""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from astrbot.api import logger

from ..util.safe_io import safe_remove_file

APPROVE_WORDS = frozenset({"通过", "赞成", "+1", "要", "留下", "收", "收录", "好", "ok", "yes"})
REJECT_WORDS = frozenset({"拒绝", "反对", "-1", "不要", "丢弃", "删", "删除", "滚", "no"})


class CrowdCurationService:
    """Manage active per-group pending sticker voting sessions."""

    SESSION_TTL_SECONDS = 10 * 60

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def vote_threshold(self) -> int:
        behavior = getattr(self.owner, "behavior", None)
        settings = getattr(behavior, "settings", None) if behavior else None
        raw = getattr(settings, "crowd_curation_threshold", 2)
        try:
            return max(1, min(20, int(raw)))
        except (TypeError, ValueError):
            return 2

    def _prune(self) -> None:
        now = time.time()
        expired = [
            umo
            for umo, sess in self._sessions.items()
            if now - float(sess.get("created_at", 0) or 0) > self.SESSION_TTL_SECONDS
        ]
        for umo in expired:
            self._sessions.pop(umo, None)

    def get_active_session(self, umo: str) -> dict[str, Any] | None:
        self._prune()
        return self._sessions.get(str(umo or "").strip())

    def _pick_next_pending(self, umo: str, *, skip_ids: set[int] | None = None) -> dict[str, Any] | None:
        memes = getattr(self.owner, "memes", None)
        db = getattr(memes, "db_service", None) if memes else None
        if db is None or not callable(getattr(db, "get_pending_paginated", None)):
            return None
        rows, _, _ = db.get_pending_paginated(page=1, page_size=50)
        skipped = skip_ids or set()
        target_origin = f"umo:{umo}" if umo else ""

        preferred: list[dict[str, Any]] = []
        others: list[dict[str, Any]] = []
        for row in rows:
            pid = int(row.get("id") or 0)
            if pid <= 0 or pid in skipped:
                continue
            status = str(row.get("review_status") or "pending").strip().lower()
            if status in {"queued", "processing"}:
                continue
            path = str(row.get("path") or "")
            if not path or not os.path.isfile(path):
                continue
            if target_origin and str(row.get("origin_target") or "").strip() == target_origin:
                preferred.append(row)
            else:
                others.append(row)
        candidates = preferred or others
        return candidates[0] if candidates else None

    async def start_session(self, event: Any, *, skip_current: bool = False) -> tuple[dict[str, Any] | None, str]:
        """Start or display the active crowd-curation candidate for this group."""
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo:
            return None, "无法识别当前会话，暂无法发起群聊审图。"

        async with self._lock:
            self._prune()
            current = self._sessions.get(umo)
            skip_ids: set[int] = set()
            if current and not skip_current:
                if os.path.isfile(str(current.get("path") or "")):
                    return current, ""
                self._sessions.pop(umo, None)
            elif current and skip_current:
                skip_ids.add(int(current.get("pending_id") or 0))
                self._sessions.pop(umo, None)

            row = await asyncio.to_thread(self._pick_next_pending, umo, skip_ids=skip_ids)
            if row is None:
                return None, "当前待审核池为空，暂时没有待审表情。"

            threshold = self.vote_threshold()
            session = {
                "pending_id": int(row["id"]),
                "path": str(row["path"]),
                "hash": str(row.get("hash") or ""),
                "category": str(row.get("category") or "未分类"),
                "desc": str(row.get("desc") or "").strip(),
                "approve_voters": set(),
                "reject_voters": set(),
                "threshold": threshold,
                "created_at": time.time(),
            }
            self._sessions[umo] = session
            return session, ""

    @staticmethod
    def parse_vote(text: str) -> str | None:
        clean = re.sub(r"[\s。！!~～,.，、]+", "", str(text or "")).strip().lower()
        if clean in APPROVE_WORDS:
            return "approve"
        if clean in REJECT_WORDS:
            return "reject"
        return None

    async def cast_vote(self, event: Any, vote: str) -> str | None:
        """Record a member's vote and resolve if threshold is reached."""
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        sender_id = str(
            (event.get_sender_id() if callable(getattr(event, "get_sender_id", None)) else "")
            or ""
        ).strip()
        if not umo or not sender_id:
            return None

        resolved_action: str | None = None
        pending_id = 0
        pending_path = ""
        threshold = 2
        approve_cnt = 0
        reject_cnt = 0

        async with self._lock:
            self._prune()
            sess = self._sessions.get(umo)
            if not sess:
                return None
            pending_id = int(sess["pending_id"])
            pending_path = str(sess["path"])
            threshold = int(sess["threshold"])

            if vote == "approve":
                if sender_id in sess["approve_voters"]:
                    return (
                        f"你已经投过「通过」票啦（当前 通过 {len(sess['approve_voters'])}/{threshold} · "
                        f"拒绝 {len(sess['reject_voters'])}/{threshold}）。"
                    )
                sess["reject_voters"].discard(sender_id)
                sess["approve_voters"].add(sender_id)
            elif vote == "reject":
                if sender_id in sess["reject_voters"]:
                    return (
                        f"你已经投过「拒绝」票啦（当前 通过 {len(sess['approve_voters'])}/{threshold} · "
                        f"拒绝 {len(sess['reject_voters'])}/{threshold}）。"
                    )
                sess["approve_voters"].discard(sender_id)
                sess["reject_voters"].add(sender_id)
            else:
                return None

            approve_cnt = len(sess["approve_voters"])
            reject_cnt = len(sess["reject_voters"])
            if approve_cnt >= threshold:
                resolved_action = "approve"
                self._sessions.pop(umo, None)
            elif reject_cnt >= threshold:
                resolved_action = "reject"
                self._sessions.pop(umo, None)

        if resolved_action is None:
            side_label = "通过" if vote == "approve" else "拒绝"
            return (
                f"已记「{side_label}」1 票（待审 #{pending_id}："
                f"通过 {approve_cnt}/{threshold} · 拒绝 {reject_cnt}/{threshold}）。"
            )

        memes = getattr(self.owner, "memes", None)
        db = getattr(memes, "db_service", None) if memes else None
        api = getattr(memes, "plugin_api", None) if memes else None
        if db is None or api is None:
            return "审图服务暂不可用，请稍后再试。"

        if resolved_action == "approve":
            try:
                if callable(getattr(db, "update_pending", None)):
                    await db.update_pending(
                        pending_id,
                        {"scope_mode": "local", "origin_target": f"umo:{umo}"},
                        allowed_fields=("scope_mode", "origin_target"),
                    )
                ok, msg = await api._approve_one(db, pending_id)
                if ok:
                    if msg == "queued":
                        return f"✅ 审图通过（{approve_cnt}/{threshold} 票）！待审 #{pending_id} 已进入识图队列，完成后自动加入本群表情库。"
                    return f"✅ 审图通过（{approve_cnt}/{threshold} 票）！待审 #{pending_id} 已加入本群表情库。"
                return f"审图票数已达标，但入库失败：{msg}"
            except Exception as exc:
                logger.warning("[CrowdCuration] approve failed id=%s: %s", pending_id, exc)
                return f"审图入库异常：{exc}"

        # Reject
        try:
            removed = db.delete_pending_batch([pending_id]) if callable(getattr(db, "delete_pending_batch", None)) else []
            for row in removed:
                p = str(row.get("path") or "")
                if p:
                    await safe_remove_file(p)
            if not removed and pending_path:
                if callable(getattr(db, "delete_pending", None)):
                    db.delete_pending(pending_id)
                await safe_remove_file(pending_path)
            return f"🗑️ 审图未通过（{reject_cnt}/{threshold} 票），待审 #{pending_id} 已从待审核池移除。"
        except Exception as exc:
            logger.warning("[CrowdCuration] reject failed id=%s: %s", pending_id, exc)
            return f"移除待审表情失败：{exc}"

    async def try_handle_vote_message(self, event: Any) -> str | None:
        """Intercept natural group chat votes ('通过'/'拒绝'/'+1'/'-1') when a session is active."""
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo or self.get_active_session(umo) is None:
            return None
        text = str(getattr(event, "message_str", "") or "").strip()
        vote = self.parse_vote(text)
        if vote is None:
            return None
        return await self.cast_vote(event, vote)
