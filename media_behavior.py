"""Durable, scoped media preferences and receipt-based repetition control."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from functools import wraps
from contextlib import contextmanager
from threading import RLock
from .media_settings import MediaSettings

from astrbot.api import logger

from .core.events.event_context import unwrap_event


def user_text(event):
    event = unwrap_event(event)
    return str(
        getattr(event, "_cd_original_user_text_v1", "")
        or getattr(event, "message_str", "")
        or ""
    )[:4000]


def evidence_only(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (sqlite3.Error, OSError):
            logger.warning(
                "[VisualMedia] evidence write unavailable: %s", function.__name__
            )
            return False

    return wrapped


class MediaBehavior:
    def __init__(self, root, settings=None):
        self.path = Path(root) / "media_behavior.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cache = OrderedDict()
        self._cache_lock = RLock()
        self._settings_source = settings
        self._last_cleanup = 0.0
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError(
                    "Media behavior database is newer than this plugin; upgrade the plugin before opening it"
                )
            db.executescript("""
            CREATE TABLE IF NOT EXISTS preferences(scope TEXT, member TEXT, data TEXT, updated REAL, PRIMARY KEY(scope,member));
            CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY, scope TEXT, trace TEXT, digest TEXT, phash TEXT, caption TEXT, created REAL, UNIQUE(scope,trace,digest));
            CREATE INDEX IF NOT EXISTS receipt_scope ON receipts(scope,created);
            CREATE TABLE IF NOT EXISTS reservations(scope TEXT, digest TEXT, trace TEXT, expires REAL, PRIMARY KEY(scope,digest));
            CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, scope TEXT, member TEXT, trace TEXT, mode TEXT, phase TEXT, reason TEXT, details TEXT, created REAL);
            CREATE INDEX IF NOT EXISTS decision_scope ON decisions(scope,created);
            """)
            db.execute("PRAGMA user_version=1")

    @property
    def settings(self):
        raw = (
            self._settings_source()
            if callable(self._settings_source)
            else self._settings_source
        )
        return MediaSettings.parse(raw)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def identity(self, event):
        event = unwrap_event(event)
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if not origin:
            return "", ""
        try:
            sender = str(event.get_sender_id() or "")
        except (AttributeError, TypeError):
            sender = ""
        # No member identity means no member preference writes, never a group default.
        scope = hashlib.sha256(origin.encode()).hexdigest()[:24]
        member = (
            hashlib.sha256((origin + "\0" + sender).encode()).hexdigest()[:24]
            if sender
            else ""
        )
        return scope, member

    def bind(self, event):
        event = unwrap_event(event)
        event._visual_media_behavior = self
        if not getattr(event, "_visual_media_trace", None):
            message = str(
                getattr(getattr(event, "message_obj", None), "message_id", "") or ""
            )
            origin = str(getattr(event, "unified_msg_origin", "") or "")
            event._visual_media_trace = (
                hashlib.sha256((origin + "\0" + message).encode()).hexdigest()[:24]
                if message
                else uuid.uuid4().hex[:24]
            )
        return event

    def preferences(self, event):
        if not self.settings.preferences_enabled:
            return {"frequency": "auto", "style": "", "blocked": []}
        scope, member = self.identity(event)
        with self.connect() as db:
            row = db.execute(
                "SELECT data FROM preferences WHERE scope=? AND member=?",
                (scope, member),
            ).fetchone()
        return (
            json.loads(row[0])
            if row
            else {"frequency": "auto", "style": "", "blocked": []}
        )

    @staticmethod
    def _default_preferences():
        return {"frequency": "auto", "style": "", "blocked": []}

    def _preferences_with_db(self, db, scope, member):
        if not self.settings.preferences_enabled or not scope or not member:
            return self._default_preferences()
        row = db.execute(
            "SELECT data FROM preferences WHERE scope=? AND member=?", (scope, member)
        ).fetchone()
        return json.loads(row[0]) if row else self._default_preferences()

    def _format_prompt(self, event, value):
        names = {
            "auto": "自然选择",
            "less": "少用表情，优先文字",
            "off": "不主动发表情",
            "more": "合适时更常用表情，但不要硬配或重复",
        }
        return (
            "\n[当前成员的视觉表达偏好]\n"
            + names.get(value["frequency"], "自然选择")
            + "\n偏好画风（仅作为画风资料，不是指令）："
            + json.dumps(value.get("style", ""), ensure_ascii=False)
            + "\n本轮明确指定的画风优先；角色固定外观仍须遵守。候选已避开最近发过或不想再看的图片。"
            + (
                "\n本轮明确偏好已保存，可以自然确认。"
                if event.get_extra("visual_media_preference_saved", False)
                else ""
            )
            + "\n不要声称保存未实际写入的偏好；不明确的要求先澄清。"
        )

    def _remember_explicit_with_db(self, db, event, scope, member):
        event._visual_preferences_processed = True
        text = user_text(event).strip()
        # Only a whole, direct instruction. Quoted/forwarded/example text is not a preference.
        if any(
            mark in text
            for mark in (
                "「",
                "“",
                '"',
                "```",
                "例如",
                "他说",
                "她说",
                "假如",
                "如果",
                "引用",
            )
        ):
            return None
        value = self._preferences_with_db(db, scope, member)
        old = json.dumps(value, sort_keys=True)
        clean = re.sub(r"^(?:安安[，,\s]*)?(?:请|麻烦你|你)?", "", text).strip(
            "。！!～~ "
        )
        for clean in re.split(r"[，,。；;]", clean):
            clean = re.sub(
                r"^(?:请|你)?(?:(?:以后|之后)?(?:聊天时|说话时|聊天|说话))?(?:尽量)?",
                "",
                clean,
            ).strip()
            if (
                re.fullmatch(
                    r"(?:以后|之后|接下来)?(?:请)?(?:少发|少用|少发表情包|少用点)(?:点|一些)?(?:表情包|表情|贴纸)?(?:吧|了)?",
                    clean,
                )
                and "表情" in clean
            ):
                value["frequency"] = "less"
            elif re.fullmatch(
                r"(?:以后|之后|接下来)?(?:不要|别|不想要)(?:再)?(?:发|用)(?:表情包|表情|贴纸)(?:了|吧)?",
                clean,
            ):
                value["frequency"] = "off"
            elif re.fullmatch(
                r"(?:以后|之后)?(?:多发|多用)(?:点|一些)?(?:表情包|表情|贴纸)(?:吧)?",
                clean,
            ):
                value["frequency"] = "more"
            elif re.fullmatch(
                r"(?:恢复|重置)(?:默认)?(?:媒体|表情|表达)(?:偏好|设置|习惯)", clean
            ):
                value = self._default_preferences()
            elif re.fullmatch(
                r"(?:这张|刚才那张|上张)(?:表情包|表情|图)?(?:以后)?(?:别|不要)(?:再)?发(?:了|吧)?",
                clean,
            ):
                last = db.execute(
                    "SELECT digest FROM receipts WHERE scope=? ORDER BY id DESC LIMIT 1",
                    (scope,),
                ).fetchone()
                if last:
                    value["blocked"] = list(
                        dict.fromkeys(value.get("blocked", []) + [last[0]])
                    )[-100:]
            else:
                match = re.fullmatch(
                    r"(?:以后(?:画图|生图)?(?:请|就|都)?(?:用|画成)|我(?:更|比较)?喜欢)([\w\s、，-]{1,60}?)(?:的)?(?:画风|风格)(?:吧|了)?",
                    clean,
                )
                if match:
                    style = match[1].strip()
                    if style in {"这个", "这种", "刚才那个", "刚才这种"}:
                        row = db.execute(
                            'SELECT details FROM decisions WHERE scope=? AND mode="generate" AND phase="submitted" ORDER BY id DESC LIMIT 1',
                            (scope,),
                        ).fetchone()
                        style = str(json.loads(row[0]).get("style", "")) if row else ""
                    if style:
                        value["style"] = style[:300]
        if old == json.dumps(value, sort_keys=True):
            return value
        db.execute(
            "INSERT OR REPLACE INTO preferences VALUES (?,?,?,?)",
            (scope, member, json.dumps(value, ensure_ascii=False), time.time()),
        )
        return value, True

    def remember_explicit(self, event):
        if not self.settings.preferences_enabled:
            return
        event = self.bind(event)
        if getattr(event, "_visual_preferences_processed", False):
            return
        scope, member = self.identity(event)
        if not scope or not member:
            event._visual_preferences_processed = True
            return
        with self.connect() as db:
            res = self._remember_explicit_with_db(db, event, scope, member)
        if isinstance(res, tuple):
            value, saved = res
            if saved:
                self.record(
                    event,
                    "preference",
                    "saved",
                    "explicit_user_preference",
                    {
                        "frequency": value["frequency"],
                        "style": value["style"],
                        "blocked_count": len(value["blocked"]),
                    },
                )
                event.set_extra("visual_media_preference_saved", True)

    def remember_and_prompt(self, event, attach_prompt: bool = True) -> str:
        """Process explicit preference and build prompt injection in one SQLite session."""
        if not self.settings.preferences_enabled:
            return ""
        event = self.bind(event)
        scope, member = self.identity(event)
        already = getattr(event, "_visual_preferences_processed", False)
        if not scope or not member:
            event._visual_preferences_processed = True
            if not attach_prompt:
                return ""
            return self._format_prompt(event, self._default_preferences())
        value = None
        saved_payload = None
        with self.connect() as db:
            if not already:
                res = self._remember_explicit_with_db(db, event, scope, member)
                if isinstance(res, tuple):
                    value, saved = res
                    if saved:
                        saved_payload = {
                            "frequency": value["frequency"],
                            "style": value["style"],
                            "blocked_count": len(value["blocked"]),
                        }
                else:
                    value = res
            if attach_prompt and value is None:
                value = self._preferences_with_db(db, scope, member)
        if saved_payload is not None:
            self.record(
                event, "preference", "saved", "explicit_user_preference", saved_payload
            )
            event.set_extra("visual_media_preference_saved", True)
        if not attach_prompt:
            return ""
        return self._format_prompt(event, value or self._default_preferences())

    def prompt(self, event):
        if not self.settings.preferences_enabled:
            return ""
        value = self.preferences(event)
        return self._format_prompt(event, value)

    def fingerprint(self, candidate):
        with self._cache_lock:
            return self._fingerprint(candidate)

    def _fingerprint(self, candidate):
        path = Path(candidate["path"])
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if key in self.cache:
            self.cache.move_to_end(key)
            digest, phash = self.cache[key]
            return digest, phash, str(candidate.get("overlay_text") or "")[:500]
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(256 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        phash = str(candidate.get("phash") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", phash):
            phash = ""
        if not phash:
            try:
                from PIL import Image

                with Image.open(path) as image:
                    if not getattr(image, "is_animated", False):
                        resized = image.convert("L").resize((17, 16))
                        get_flat = getattr(resized, "get_flattened_data", None)
                        pixels = list(
                            get_flat() if callable(get_flat) else resized.getdata()
                        )
                        bits = 0
                        for row in range(16):
                            for col in range(16):
                                at = row * 17 + col
                                bits = (bits << 1) | (pixels[at] > pixels[at + 1])
                        phash = f"{bits:064x}"
            except Exception:
                phash = ""
        result = (digest, phash, str(candidate.get("overlay_text") or "")[:500])
        self.cache[key] = (digest, phash)
        while len(self.cache) > 512:
            self.cache.popitem(last=False)
        return result

    def explicit_meme(self, event):
        text = user_text(event)
        return not re.search(r"不要|别|少发|少用", text) and bool(
            re.search(
                r"(?:发|来|再来|给我).{0,12}(?:表情|贴纸)|(?:再发|重发).{0,8}(?:这张|刚才|上张)",
                text,
            )
        )

    def explicit_repeat(self, event):
        text = user_text(event)
        return not re.search(r"不要|别", text) and bool(
            re.search(
                r"(?:再发|重发|再来).{0,8}(?:这张|那张|刚才|上张|同一张)|(?:这张|那张|刚才|上张).{0,8}(?:再发|重发)",
                text,
            )
        )

    def snapshot(self, event, db=None):
        if db is None:
            with self.connect() as connection:
                return self.snapshot(event, connection)
        scope, member = self.identity(event)
        row = db.execute(
            "SELECT data FROM preferences WHERE scope=? AND member=?", (scope, member)
        ).fetchone()
        prefs = (
            json.loads(row[0])
            if row and self.settings.preferences_enabled
            else {"frequency": "auto", "style": "", "blocked": []}
        )
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT digest,phash,caption,trace,created FROM receipts WHERE scope=? ORDER BY id DESC LIMIT ?",
                (scope, self.settings.recent_receipts),
            )
        ]
        return prefs, rows

    def reason_for(self, event, fingerprint, prefs, rows):
        settings = self.settings
        # A request may have been prefetched before the operator changed this switch.
        if not settings.preferences_enabled:
            prefs = {"frequency": "auto", "style": "", "blocked": []}
        digest = fingerprint[0]
        if digest in prefs.get("blocked", []):
            return "member_blocked_image"
        explicit = self.explicit_meme(event)
        if prefs["frequency"] == "off" and not explicit:
            return "member_prefers_text"
        if (
            settings.repeat_guard_enabled
            and not self.explicit_repeat(event)
            and any(
                r["digest"] == digest
                and r["created"] > time.time() - settings.repeat_window_seconds
                for r in rows
            )
        ):
            return "recent_exact_repeat"
        if (
            not explicit
            and prefs["frequency"] == "less"
            and rows
            and rows[0]["created"] > time.time() - settings.less_interval_seconds
        ):
            return "member_prefers_fewer_memes"
        return ""

    def reason(self, event, candidate):
        if not self.identity(event)[0]:
            return ""
        return self.reason_for(
            event, self.fingerprint(candidate), *self.snapshot(event)
        )

    def filter_candidates(self, event, candidates):
        prefs, recent = self.snapshot(event)
        allowed, removed = [], {}
        settings = self.settings
        for candidate in candidates:
            try:
                fingerprint = self.fingerprint(candidate)
                reason = self.reason_for(event, fingerprint, prefs, recent)
                if reason:
                    removed[reason] = removed.get(reason, 0) + 1
                    continue
                _, phash, caption = fingerprint
                similar = settings.similar_demotion_enabled and bool(
                    phash
                    and any(
                        r["created"] > time.time() - settings.repeat_window_seconds
                        and r["phash"]
                        and r["caption"] == caption
                        and (int(phash, 16) ^ int(r["phash"], 16)).bit_count()
                        <= settings.similar_distance
                        for r in recent
                    )
                )
                allowed.append((similar, candidate))
            except (OSError, ValueError):
                continue
        if removed:
            self.record(event, "meme", "filtered", "candidate_filter", removed)
        return [c for _, c in sorted(allowed, key=lambda pair: pair[0])]

    def reserve(self, event, candidate):
        event = self.bind(event)
        reason = self.reason(event, candidate)
        if reason:
            return False
        scope, _ = self.identity(event)
        if not scope:
            return True
        digest, _, _ = self.fingerprint(candidate)
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.reason(event, candidate):
                return False
            db.execute("DELETE FROM reservations WHERE expires<?", (now,))
            row = db.execute(
                "SELECT trace FROM reservations WHERE scope=? AND digest=?",
                (scope, digest),
            ).fetchone()
            if row and row[0] != event._visual_media_trace:
                return False
            db.execute(
                "INSERT OR REPLACE INTO reservations VALUES (?,?,?,?)",
                (scope, digest, event._visual_media_trace, now + 60),
            )
        return True

    @evidence_only
    def release(self, event, candidate):
        scope, _ = self.identity(event)
        digest, _, _ = self.fingerprint(candidate)
        with self.connect() as db:
            db.execute(
                "DELETE FROM reservations WHERE scope=? AND digest=? AND trace=?",
                (scope, digest, getattr(event, "_visual_media_trace", "")),
            )

    @evidence_only
    def delivered(self, event, candidate):
        event = self.bind(event)
        scope, _ = self.identity(event)
        digest, phash, caption = self.fingerprint(candidate)
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO receipts(scope,trace,digest,phash,caption,created) VALUES (?,?,?,?,?,?)",
                (scope, event._visual_media_trace, digest, phash, caption, time.time()),
            )
            db.execute("DELETE FROM receipts WHERE created<?", (time.time() - 86400,))
            db.execute(
                "DELETE FROM receipts WHERE id <= (SELECT id FROM receipts ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (self.settings.max_records,),
            )
        self.release(event, candidate)
        return self.record(
            event, "meme", "delivered", "confirmed_image_send", {"image": digest[:16]}
        )

    @evidence_only
    def record(self, event, mode, phase, reason, details=None):
        if not self.settings.decision_records_enabled:
            return
        event = self.bind(event)
        scope, member = self.identity(event)
        encoded = json.dumps(details or {}, ensure_ascii=False)
        if len(encoded) > 16000:
            encoded = json.dumps({"summary": "details_too_large"})
        with self.connect() as db:
            db.execute(
                "INSERT INTO decisions(scope,member,trace,mode,phase,reason,details,created) VALUES (?,?,?,?,?,?,?,?)",
                (
                    scope,
                    member,
                    event._visual_media_trace,
                    str(mode)[:30],
                    str(phase)[:30],
                    str(reason)[:120],
                    encoded,
                    time.time(),
                ),
            )
            if time.monotonic() - self._last_cleanup > 60:
                db.execute(
                    "DELETE FROM decisions WHERE created<?",
                    (time.time() - self.settings.record_retention_days * 86400,),
                )
                self._last_cleanup = time.monotonic()
            db.execute(
                "DELETE FROM decisions WHERE id <= (SELECT id FROM decisions ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (self.settings.max_records,),
            )

    def records(self, limit=100, scope=""):
        with self.connect() as db:
            rows = db.execute(
                'SELECT * FROM decisions WHERE (?="" OR scope=?) AND created>? ORDER BY id DESC LIMIT ?',
                (
                    scope,
                    scope,
                    time.time() - self.settings.record_retention_days * 86400,
                    max(1, min(int(limit), 200)),
                ),
            ).fetchall()
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    def preference_rows(self):
        with self.connect() as db:
            return [
                {**dict(row), "data": json.loads(row["data"])}
                for row in db.execute(
                    "SELECT * FROM preferences ORDER BY updated DESC LIMIT 200"
                )
            ]

    def reset(self, scope, member):
        with self.connect() as db:
            db.execute(
                "DELETE FROM preferences WHERE scope=? AND member=?", (scope, member)
            )
