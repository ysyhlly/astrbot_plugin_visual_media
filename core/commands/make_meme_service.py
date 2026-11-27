"""C-side group meme creation (/做表情) with daily per-user quota and reply-to-save."""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..util.platform_capabilities import get_platform_capabilities

SAVE_REPLY_KEYWORDS = frozenset({"收藏", "入库", "存图", "收了", "保存表情", "收藏表情"})


class MakeMemeService:
    """Group meme creator with relaxed default 10/person/day quota and reply-to-save."""

    RECENT_CACHE_TTL_SECONDS = 10 * 60
    MAX_RECENT_ENTRIES = 200

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        root = Path(
            getattr(getattr(owner, "generation", None), "_plugin_data_dir", None)
            or getattr(getattr(owner, "memes", None), "base_dir", ".")
        )
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "make_meme_quota.sqlite3"
        self._recent: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS make_meme_quota (
                    day TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, scope, user_id)
                )
                """
            )

    def daily_limit(self) -> int:
        behavior = getattr(self.owner, "behavior", None)
        settings = getattr(behavior, "settings", None) if behavior else None
        raw = getattr(settings, "make_meme_daily_limit", 10)
        try:
            return max(1, min(200, int(raw)))
        except (TypeError, ValueError):
            return 10

    def _today_key(self) -> str:
        tz_getter = getattr(self.owner, "_series_tz", None)
        tz = tz_getter() if callable(tz_getter) else timezone.utc
        return datetime.now(timezone.utc).astimezone(tz).date().isoformat()

    def _consume_quota_sync(self, scope: str, user_id: str, limit: int) -> tuple[bool, int]:
        day = self._today_key()
        now = time.time()
        with self._connect() as db:
            row = db.execute(
                "SELECT used FROM make_meme_quota WHERE day=? AND scope=? AND user_id=?",
                (day, scope, user_id),
            ).fetchone()
            used = int(row[0]) if row else 0
            if used >= limit:
                return False, used
            new_used = used + 1
            db.execute(
                """
                INSERT INTO make_meme_quota (day, scope, user_id, used, updated)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(day, scope, user_id) DO UPDATE SET used=excluded.used, updated=excluded.updated
                """,
                (day, scope, user_id, new_used, now),
            )
            return True, new_used

    def _refund_quota_sync(self, scope: str, user_id: str) -> None:
        day = self._today_key()
        with self._connect() as db:
            db.execute(
                """
                UPDATE make_meme_quota
                SET used = MAX(0, used - 1), updated = ?
                WHERE day=? AND scope=? AND user_id=?
                """,
                (time.time(), day, scope, user_id),
            )

    def get_usage_sync(self, scope: str, user_id: str) -> tuple[int, int]:
        day = self._today_key()
        limit = self.daily_limit()
        with self._connect() as db:
            row = db.execute(
                "SELECT used FROM make_meme_quota WHERE day=? AND scope=? AND user_id=?",
                (day, scope, user_id),
            ).fetchone()
            return (int(row[0]) if row else 0), limit

    @staticmethod
    def extract_quote_context(event: Any) -> dict[str, str]:
        """Extract replied quote text, quoted speaker ID & nickname from message chain."""
        quote_text = ""
        speaker_id = ""
        speaker_name = ""
        image_ref = ""
        try:
            messages = list(event.get_messages() or [])
        except Exception:
            messages = []

        for comp in messages:
            cls_name = type(comp).__name__.lower()
            if cls_name == "reply" or hasattr(comp, "chain"):
                raw_text = str(
                    getattr(comp, "message_str", "")
                    or getattr(comp, "text", "")
                    or ""
                ).strip()
                if not raw_text and getattr(comp, "chain", None):
                    parts = []
                    for sub in getattr(comp, "chain", []) or []:
                        sub_text = getattr(sub, "text", None)
                        if sub_text:
                            parts.append(str(sub_text).strip())
                        elif not image_ref and type(sub).__name__.lower() == "image":
                            image_ref = str(
                                getattr(sub, "file", "")
                                or getattr(sub, "url", "")
                                or ""
                            ).strip()
                    raw_text = " ".join(p for p in parts if p)
                if raw_text:
                    quote_text = raw_text[:180]
                speaker_id = str(getattr(comp, "sender_id", "") or "").strip()
                speaker_name = str(
                    getattr(comp, "sender_nickname", "")
                    or getattr(comp, "sender_name", "")
                    or ""
                ).strip()
            elif not image_ref and cls_name == "image":
                image_ref = str(
                    getattr(comp, "file", "") or getattr(comp, "url", "") or ""
                ).strip()

        return {
            "quote_text": quote_text,
            "speaker_id": speaker_id,
            "speaker_name": speaker_name,
            "image_ref": image_ref,
        }

    def _prune_recent(self) -> None:
        now = time.time()
        expired = [
            k
            for k, v in self._recent.items()
            if now - float(v.get("created_at", 0) or 0) > self.RECENT_CACHE_TTL_SECONDS
        ]
        for k in expired:
            self._recent.pop(k, None)
        while len(self._recent) > self.MAX_RECENT_ENTRIES:
            oldest = min(
                self._recent,
                key=lambda k: float(self._recent[k].get("created_at", 0) or 0),
            )
            self._recent.pop(oldest, None)

    @staticmethod
    def build_meme_prompt(
        *,
        caption: str,
        quote_text: str = "",
        speaker_name: str = "",
        has_avatar: bool = False,
    ) -> str:
        """Build a punchy sticker/meme generation prompt."""
        parts = [
            "请制作一张高清聊天表情包贴纸（1:1 构图，纯色或简洁浅色背景，主体清晰、表情夸张搞笑、极具吐槽感/接梗感）。"
        ]
        if has_avatar:
            parts.append("参考附带头像的人物/角色形象特征，绘制成可爱的 Q 版或卡通梗图角色。")
        if quote_text and caption:
            who = f"「{speaker_name}」说过：" if speaker_name else "群友金句："
            parts.append(f"{who}「{quote_text}」。")
            parts.append(f"结合情绪/配文要求：「{caption}」。")
            parts.append(f"在画面醒目位置用清晰大号中文字体配字：「{caption[:28]}」。")
        elif quote_text:
            who = f"「{speaker_name}」的金句" if speaker_name else "群友金句"
            parts.append(f"根据{who}制作反差搞笑表情包：「{quote_text}」。")
            parts.append(f"画面配上清晰醒目的中文表情文字：「{quote_text[:28]}」。")
        else:
            parts.append(f"表情主题与配文：「{caption}」。")
            parts.append(f"在画面醒目位置用清晰大号中文字体配字：「{caption[:28]}」。")
        return "\n".join(parts)

    async def create_meme(self, event: Any, raw_prompt: str = "") -> dict[str, Any]:
        """Execute /做表情 for ``event`` and return generation payload or error."""
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        sender_id = str(
            (event.get_sender_id() if callable(getattr(event, "get_sender_id", None)) else "")
            or ""
        ).strip()
        if not umo or not sender_id:
            return {"ok": False, "error": "无法识别当前会话或成员身份，暂无法制作表情。"}

        gen = getattr(self.owner, "generation", None)
        if gen is None:
            return {"ok": False, "error": "生图引擎尚未就绪，请稍后再试。"}

        limiter = getattr(gen, "rate_limiter", None)
        if limiter is not None and callable(getattr(limiter, "allows_group", None)):
            if not limiter.allows_group(event):
                return {"ok": False, "error": "当前会话未开启生图功能。"}

        quote_ctx = self.extract_quote_context(event)
        caption = str(raw_prompt or "").strip()
        if not caption and not quote_ctx["quote_text"]:
            used, limit = await asyncio.to_thread(self.get_usage_sync, umo, sender_id)
            return {
                "ok": False,
                "error": (
                    "用法：/做表情 <情绪或文案>（也可回复一条群友消息再发送 /做表情 [吐槽文案]）\n"
                    f"今日已用 {used}/{limit} 次，生成后回复「收藏」或「入库」即可保存到本群专属表情库。"
                ),
            }

        limit = self.daily_limit()
        allowed, used_now = await asyncio.to_thread(
            self._consume_quota_sync, umo, sender_id, limit
        )
        if not allowed:
            return {
                "ok": False,
                "error": f"你今天在本群的 /做表情 额度已用完（{used_now}/{limit} 次），明天再来造梗吧~",
            }

        # Collect reference image (quoted image or speaker/sender avatar when platform supports it)
        refs: list[str] = []
        caps = get_platform_capabilities(event)
        if quote_ctx["image_ref"] and Path(quote_ctx["image_ref"]).is_file():
            refs.append(quote_ctx["image_ref"])
        elif caps.supports_avatar_lookup:
            avatar_handler = getattr(gen, "avatar_handler", None)
            target_uid = quote_ctx["speaker_id"] or sender_id
            if avatar_handler is not None and target_uid and callable(
                getattr(avatar_handler, "download_user_avatar", None)
            ):
                try:
                    avatar_b64 = await avatar_handler.download_user_avatar(target_uid, event)
                    if avatar_b64:
                        refs.append(avatar_b64)
                except Exception as exc:
                    logger.debug("[MakeMeme] avatar fetch skipped: %s", exc)

        prompt = self.build_meme_prompt(
            caption=caption,
            quote_text=quote_ctx["quote_text"],
            speaker_name=quote_ctx["speaker_name"],
            has_avatar=bool(refs),
        )

        try:
            from ...tl.generation_tracker import requester_from_event

            service = gen.get_service(1)
            submission = await service.submit(
                plugin_id=getattr(self.owner, "name", "astrbot_plugin_visual_media"),
                plugin_name="视觉工坊 · 群聊造梗",
                prompt=prompt,
                umo=umo if umo.count(":") >= 2 else None,
                requester=requester_from_event(event),
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
                str(p)
                for p in (task.get("image_paths") or [])
                if isinstance(p, str) and Path(p).is_file()
            ]
            if status not in {"succeeded", "partial_success"} or not image_paths:
                await asyncio.to_thread(self._refund_quota_sync, umo, sender_id)
                err = str(task.get("error") or task.get("message") or f"生成失败 ({status})")
                return {"ok": False, "error": f"制作表情失败（已退还今日额度）：{err}"}

            image_path = image_paths[0]
            overlay = (caption or quote_ctx["quote_text"])[:60]
            desc = (
                f"群友造梗：{caption}（引用：{quote_ctx['quote_text']}）"
                if caption and quote_ctx["quote_text"]
                else f"群友造梗：{overlay}"
            )[:240]

            async with self._lock:
                self._prune_recent()
                self._recent[umo] = {
                    "path": image_path,
                    "prompt": prompt,
                    "desc": desc,
                    "overlay_text": overlay,
                    "creator_id": sender_id,
                    "created_at": time.time(),
                    "saved": False,
                }

            remaining = max(0, limit - used_now)
            return {
                "ok": True,
                "image_path": image_path,
                "used": used_now,
                "limit": limit,
                "remaining": remaining,
                "notice": (
                    f"🎨 表情已做好！（今日剩余 {remaining}/{limit} 次）\n"
                    "回复「收藏」或「入库」即可直接保存到本群专属表情库~"
                ),
            }
        except Exception as exc:
            await asyncio.to_thread(self._refund_quota_sync, umo, sender_id)
            logger.warning("[MakeMeme] generation error: %s", exc)
            return {"ok": False, "error": f"制作表情失败（已退还今日额度）：{exc}"}

    @staticmethod
    def is_save_trigger(text: str) -> bool:
        clean = re.sub(r"[\s。！!~～,.，、]+", "", str(text or "")).strip()
        return clean in SAVE_REPLY_KEYWORDS

    async def try_handle_save_reply(self, event: Any) -> str | None:
        """If a group member replies 收藏/入库 shortly after /做表情, persist to local library."""
        text = str(getattr(event, "message_str", "") or "").strip()
        if not self.is_save_trigger(text):
            return None

        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo:
            return None

        async with self._lock:
            self._prune_recent()
            entry = self._recent.get(umo)
            if not entry or entry.get("saved"):
                return None
            image_path = Path(str(entry.get("path") or ""))
            if not image_path.is_file():
                self._recent.pop(umo, None)
                return None
            # Mark optimistically under lock to prevent duplicate concurrent saves
            entry["saved"] = True

        try:
            memes = getattr(self.owner, "memes", None)
            api = getattr(memes, "plugin_api", None) if memes else None
            cfg = getattr(memes, "plugin_config", None) if memes else None
            if api is None:
                entry["saved"] = False
                return "表情库尚未就绪，暂无法收藏。"

            raw = await asyncio.to_thread(image_path.read_bytes)
            digest = hashlib.sha256(raw).hexdigest()
            ext = image_path.suffix.lower() or ".png"
            categories = list(getattr(cfg, "categories", None) or [])
            category = "happy" if "happy" in categories else (categories[0] if categories else "generated")
            tags = ["群友造梗", "做表情"]
            desc = str(entry.get("desc") or "群友自制表情")
            scenes = [str(entry.get("overlay_text") or "群聊玩梗")]
            overlay_text = str(entry.get("overlay_text") or "")

            proc = getattr(memes, "image_processor_service", None)
            if proc is not None and callable(getattr(proc, "classify_image", None)):
                try:
                    cat, rt, rd, _, rs, ro, emotions = await proc.classify_image(
                        event=None,
                        file_path=str(image_path),
                        categories=categories,
                        content_filtration=False,
                    )
                    if cat and cat != getattr(proc, "CATEGORY_FILTERED", None):
                        category = str(cat)
                        if rt:
                            tags = list(dict.fromkeys([*rt, "群友造梗"]))
                        if rd:
                            desc = str(rd)
                        if rs:
                            scenes = list(rs)
                        if ro:
                            overlay_text = str(ro)
                except Exception:
                    pass

            await api._persist_image(
                file_content=raw,
                file_ext=ext,
                category=category,
                file_hash=digest,
                tags=tags,
                desc=desc,
                scenes=scenes,
                overlay_text=overlay_text,
                emotions=[category],
                scope_mode="local",
                origin_target=f"umo:{umo}",
                add_method="make_meme",
            )
            return f"✅ 已收藏到本群专属表情库（分类：{category}）！"
        except Exception as exc:
            entry["saved"] = False
            logger.warning("[MakeMeme] save reply failed: %s", exc)
            return f"收藏失败：{exc}"
