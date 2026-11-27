"""Connect generated images, the meme index and durable character profiles."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from astrbot.api import logger
from PIL import Image

from .core.events.event_context import unwrap_event

try:
    from . import series_panels as _series_panels
except Exception:  # pragma: no cover - standalone fallback
    try:
        import series_panels as _series_panels  # type: ignore[no-redef]
    except Exception:
        _series_panels = None  # type: ignore[assignment]


class MediaLibrary:
    """Keep cross-feature ownership and profile data inside the new plugin."""

    def __init__(self, owner):
        self.owner = owner
        self.root = Path(owner.generation._plugin_data_dir)
        self.path = self.root / "visual_library.sqlite3"
        self.lock = asyncio.Lock()
        self._public_ref_cache: dict[str, str] = {}
        self._public_ref_index_sig: tuple[int, int] | None = None
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS saved (generation TEXT, image_index INTEGER, scope TEXT, hash TEXT, PRIMARY KEY(generation,image_index,scope))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS profiles (key TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        if _series_panels is not None:
            apply_fn = getattr(_series_panels, "apply_series_sqlite_pragmas", None)
            if callable(apply_fn):
                try:
                    ctx = getattr(self.owner, "context", None)
                    apply_fn(db, ctx, readonly=False)
                except Exception:
                    pass
        try:
            with db:
                yield db
        finally:
            db.close()

    def _max_image_bytes(self) -> int:
        if _series_panels is not None:
            getter = getattr(_series_panels, "get_series_resource_limits", None)
            if callable(getter):
                try:
                    ctx = getattr(self.owner, "context", None)
                    limits = getter(ctx)
                    if isinstance(limits, dict):
                        return max(
                            1024 * 1024,
                            min(
                                32 * 1024 * 1024,
                                int(limits.get("visual_max_image_bytes", 20 * 1024 * 1024)),
                            ),
                        )
                except Exception:
                    pass
        return 20 * 1024 * 1024

    def profiles(self):
        with self._connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT data FROM profiles ORDER BY key")
            ]

    _ALLOWED_REF_SCOPES = frozenset({"public", "reference"})

    def _rebuild_public_ref_cache(self, index: dict) -> None:
        self._public_ref_cache = {
            m.get("hash"): p
            for p, m in index.items()
            if isinstance(m, dict)
            and m.get("hash")
            and m.get("scope_mode") in self._ALLOWED_REF_SCOPES
        }
        self._public_ref_index_sig = (id(index), len(index))

    def reference_path(self, image_hash):
        api = self.owner.memes.plugin_api
        db = getattr(api, "_db", None)
        raw_path = None
        if db is not None and callable(getattr(db, "get_emoji_by_hash_and_scope", None)):
            found = db.get_emoji_by_hash_and_scope(
                image_hash, scope_modes=tuple(self._ALLOWED_REF_SCOPES)
            )
            if found is None:
                raise ValueError("角色共用参考图必须是公开表情或专用参考图，请先在表情库确认可见范围")
            raw_path = found[0]
        else:
            index = api._get_index()
            sig = (id(index), len(index))
            if self._public_ref_index_sig != sig:
                self._rebuild_public_ref_cache(index)
            raw_path = self._public_ref_cache.get(image_hash)
            meta = index.get(raw_path) if raw_path else None
            if not (
                isinstance(meta, dict)
                and meta.get("hash") == image_hash
                and meta.get("scope_mode") in self._ALLOWED_REF_SCOPES
            ):
                self._rebuild_public_ref_cache(index)
                raw_path = self._public_ref_cache.get(image_hash)
                meta = index.get(raw_path) if raw_path else None
                if not (
                    isinstance(meta, dict)
                    and meta.get("hash") == image_hash
                    and meta.get("scope_mode") in self._ALLOWED_REF_SCOPES
                ):
                    raise ValueError("角色共用参考图必须是公开表情或专用参考图，请先在表情库确认可见范围")
        path = Path(raw_path).resolve()
        if (
            not path.is_relative_to(self.owner.memes.base_dir.resolve())
            or not path.is_file()
        ):
            raise ValueError("角色参考图不存在")
        return path

    async def isolate_references(self, ref_hashes: list[str], character: str = "") -> int:
        """Mark character standard reference images as scope_mode='reference' to isolate them from random meme sends."""
        api = getattr(getattr(self.owner, "memes", None), "plugin_api", None)
        if api is None or not ref_hashes:
            return 0
        updated = 0
        target_hashes = [str(h).strip() for h in ref_hashes if str(h or "").strip()]
        if not target_hashes:
            return 0
        db = getattr(api, "_db", None)
        if db is not None and callable(getattr(db, "get_emojis_by_hashes", None)):
            matches = await asyncio.to_thread(db.get_emojis_by_hashes, target_hashes)
        else:
            target_set = set(target_hashes)
            matches = [
                (p, m)
                for p, m in api._get_index().items()
                if isinstance(m, dict) and str(m.get("hash") or "") in target_set
            ]
        for path, meta in matches:
            changes: dict = {}
            if meta.get("scope_mode") != "reference":
                changes["scope_mode"] = "reference"
            if character and not str(meta.get("character") or "").strip():
                changes["character"] = character
            if changes and await api._update_index_path(path, changes):
                updated += 1
        return updated

    async def save_profile(self, data):
        key = str(data.get("key", "")).strip()
        if not re.fullmatch(r"[\w-]{1,64}", key):
            raise ValueError("角色标识只允许中文、字母、数字、下划线和短横线")
        refs = data.get("references", [])
        if (
            not isinstance(refs, list)
            or len(refs) > 8
            or not all(isinstance(ref, str) for ref in refs)
        ):
            raise ValueError("最多选择 8 张角色参考图")
        for ref in refs:
            self.reference_path(ref)
        isolate_refs = bool(data.get("isolate_references", False))
        profile = {
            "key": key,
            "name": str(data.get("name") or key)[:80],
            "appearance": str(data.get("appearance") or "")[:4000],
            "style": str(data.get("style") or "")[:2000],
            "references": list(dict.fromkeys(refs)),
            "isolate_references": isolate_refs,
        }
        async with self.lock:
            with self._connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO profiles VALUES (?, ?)",
                    (key, json.dumps(profile, ensure_ascii=False)),
                )
            cfg = self.owner.memes.plugin_config
            if key not in cfg.characters:
                cfg.characters.append(key)
            cfg.character_info[key] = {
                "name": profile["name"],
                "desc": profile["appearance"],
            }
            cfg.save_characters()
            cfg.save_character_info()
        if isolate_refs and profile["references"]:
            await self.isolate_references(profile["references"], character=key)
        return profile

    async def delete_profile(self, key):
        async with self.lock:
            with self._connect() as db:
                db.execute("DELETE FROM profiles WHERE key = ?", (key,))

    async def save_generation(
        self,
        generation_id,
        image_index=0,
        *,
        character="",
        description="",
        event=None,
        public=False,
    ):
        event = unwrap_event(event)
        if (
            not isinstance(generation_id, str)
            or type(image_index) is not int
            or image_index < 0
        ):
            raise ValueError("生成记录或图片序号无效")
        generation_id = generation_id.removeprefix("gen:")
        tracker = self.owner.generation.generation_tracker
        record = tracker.get(generation_id)
        if not record:
            raise ValueError("生成记录不存在")
        umo = str((record.get("requester") or {}).get("umo") or "")
        if event is not None:
            current = str(getattr(event, "unified_msg_origin", "") or "")
            if not current or current != umo:
                raise PermissionError("只能收藏当前会话生成的图片")
            public = False
        if not public and not umo:
            raise ValueError("此图片没有会话来源，请在面板明确选择公开收藏")
        images = record.get("images") or []
        if image_index >= len(images):
            raise ValueError("生成记录中没有这张图片")
        path = tracker.gallery_image_path(images[image_index])
        if path is None:
            raise ValueError("图片已过期或不可读取")
        if (
            character
            and character not in self.owner.memes.plugin_config.get_characters()
        ):
            raise ValueError("角色不存在，请先创建角色")
        max_bytes = self._max_image_bytes()

        def _read_verify_and_hash() -> tuple[bytes, str, str]:
            if path.stat().st_size > max_bytes:
                raise ValueError("图片超过表情入库大小上限")
            data = path.read_bytes()
            with Image.open(io.BytesIO(data)) as img:
                if img.width * img.height > 40_000_000:
                    raise ValueError("图片像素数过大")
                image_format = str(img.format).lower()
                img.verify()
            ext = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}.get(
                image_format
            )
            if not ext:
                raise ValueError("图片格式不支持收藏")
            return data, ext, hashlib.sha256(data).hexdigest()

        raw, suffix, digest = await asyncio.to_thread(_read_verify_and_hash)
        scope = "public" if public else "umo:" + umo
        api = self.owner.memes.plugin_api
        db = getattr(api, "_db", None)
        has_scoped_sql = db is not None and callable(
            getattr(db, "get_emoji_by_hash_and_scope", None)
        )
        async with self.lock:
            # Content deduplication is scoped: never broaden an existing private item.
            if has_scoped_sql:
                found = await asyncio.to_thread(
                    db.get_emoji_by_hash_and_scope,
                    digest,
                    scope_modes=("public",) if public else ("local",),
                    origin_target=None if public else scope,
                )
            else:
                found = next(
                    (
                        (p, m)
                        for p, m in api._get_index().items()
                        if m.get("hash") == digest
                        and (
                            m.get("scope_mode") == "public"
                            if public
                            else m.get("scope_mode") == "local"
                            and m.get("origin_target") == scope
                        )
                    ),
                    None,
                )
            if found and Path(found[0]).is_file():
                saved = {
                    "hash": digest,
                    "category": found[1].get("category", "unknown"),
                }
                await api._update_index_path(found[0], {"is_favorite": 1})
            else:
                saved = await api._persist_image(
                    file_content=raw,
                    file_ext=suffix,
                    category="generated",
                    file_hash=digest,
                    tags=["生成表情"],
                    desc=str(description or record.get("prompt") or "生成表情")[:2000],
                    character=character,
                    scope_mode="public" if public else "local",
                    origin_target="" if public else scope,
                    is_favorite=True,
                )
                if has_scoped_sql:
                    found_new = await asyncio.to_thread(
                        db.get_emoji_by_hash_and_scope,
                        digest,
                        origin_target="" if public else scope,
                    )
                    target = found_new[0] if found_new else ""
                else:
                    target = next(
                        p
                        for p, m in api._get_index().items()
                        if m.get("hash") == digest
                        and m.get("origin_target", "") == ("" if public else scope)
                    )
                # Share storage where supported; each library retains its own lifetime.
                link = Path(target).with_suffix(".link-tmp")
                try:
                    os.link(path, link)
                    os.replace(link, target)
                except OSError:
                    link.unlink(missing_ok=True)
            with self._connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO saved VALUES (?, ?, ?, ?)",
                    (generation_id, image_index, scope, digest),
                )
        return saved

    async def generate_character(
        self, character, prompt, event=None, *, _recovery=None, _redraw_attempt: int = 0
    ):
        from .tl.background_dispatch import dispatch_generation_result
        from .tl.generation_tracker import requester_from_event

        event = unwrap_event(event)
        behavior = getattr(self.owner, "behavior", None)
        if event is not None and behavior:
            behavior.bind(event)
        profile = (_recovery or {}).get("profile") or next((p for p in self.profiles() if p["key"] == character), None)
        if profile is None or not isinstance(prompt, str) or not prompt.strip():
            return {"error": "角色不存在或缺少图片描述"}
        generation = self.owner.generation
        if event is not None and not generation.rate_limiter.allows_group(event):
            return {"error": "此会话未启用生图"}
        refs = [str(self.reference_path(ref)) for ref in profile["references"]]
        prepared = "\n".join(
            part
            for part in (profile["appearance"], profile["style"], prompt.strip())
            if part
        )
        if event is not None and behavior:
            preferred = (await asyncio.to_thread(behavior.preferences, event)).get("style", "")
            if preferred:
                prepared += "\n成员画风偏好（仅作为画风资料，本次明确画风要求优先）：" + json.dumps(preferred, ensure_ascii=False)
        if _recovery:
            prepared = _recovery["prepared_prompt"]
        tasks = getattr(self.owner, "tasks", None)

        # A synthetic scope keeps dashboard-only checks visible without inventing a QQ recipient.
        audit_event = event
        if audit_event is None:
            from types import SimpleNamespace
            audit_event = SimpleNamespace(unified_msg_origin="dashboard:visual_media", message_str="")

        def _pending_checks():
            pending = getattr(self, "_check_tasks", None)
            if pending is None:
                pending = set()
                self._check_tasks = pending
            return pending

        async def _send_group_notice(notice_text: str):
            if event is None or getattr(self.owner, "_closed", False):
                return
            if getattr(getattr(self.owner, "behavior", None), "stop_sends", False):
                return
            target = str(getattr(event, "unified_msg_origin", "") or "").strip()
            context = getattr(self.owner, "context", None)
            send_fn = getattr(context, "send_message", None)
            if not target or not callable(send_fn):
                return
            try:
                from astrbot.api.event import MessageChain
                from astrbot.core.platform.message_session import MessageSession

                session = MessageSession.from_str(target)
                await asyncio.wait_for(
                    send_fn(session, MessageChain().message(notice_text)),
                    timeout=15,
                )
            except Exception as exc:
                logger.debug(f"[VisualMedia] 角色核对提醒发送失败: {exc}")

        async def _notify_mismatch(check, *, auto_redrawn: bool = False):
            issues = [str(x).strip() for x in (check.get("issues") or []) if str(x).strip()]
            detail = "；".join(issues) if issues else str(check.get("reason") or "部分细节不一致")
            char_label = str(profile.get("name") or character).strip()
            short_prompt = prompt.strip().splitlines()[0][:50]
            redraw_hint = f"/角色生图 {char_label} {short_prompt}"
            if auto_redrawn:
                notice = (
                    f"角色形象核对提醒（自动修正重画后仍存在差异）：{detail}。"
                    f"如需继续手动调整，可发送：{redraw_hint}"
                )
            else:
                notice = (
                    f"角色形象核对提醒：{detail}。已发送的图片保留供查看。"
                    f"如需手动重画，可发送：{redraw_hint}"
                )
            await _send_group_notice(notice)

        async def _run_post_delivery_check(result):
            from .character_check import check_character

            task_key = result.get("task_id")
            try:
                check = await check_character(
                    self.owner,
                    profile,
                    result.get("image_paths") or [],
                    refs,
                    prompt,
                    event,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"[VisualMedia] 后台角色核对异常: {exc}")
                return
            if tasks and task_key:
                fields = {"check": check}
                if event is None:
                    fields["stage"] = "ready"
                await tasks.observe(task_key, fields)
            if behavior:
                await behavior.arecord(
                    audit_event,
                    "character_check",
                    check["status"],
                    "reference_comparison",
                    {
                        "character": character,
                        "task_id": result.get("task_id", ""),
                        "redraw_attempt": _redraw_attempt,
                        **check,
                    },
                )
            if check.get("status") == "mismatch":
                auto_redraw = bool(
                    getattr(
                        getattr(behavior, "settings", None),
                        "character_check_auto_redraw",
                        True,
                    )
                )
                if auto_redraw and _redraw_attempt == 0 and not getattr(self.owner, "_closed", False):
                    issues = [
                        str(x).strip() for x in (check.get("issues") or []) if str(x).strip()
                    ]
                    detail = "；".join(issues) if issues else str(check.get("reason") or "外观细节存在偏差")
                    await _send_group_notice(
                        f"角色形象核对发现偏差（{detail}），正在自动修正并重画 1 次…"
                    )
                    corrective_prompt = (
                        f"{prompt.strip()}\n"
                        f"【外观核对修正要求：上一版存在「{detail}」，请严格依照角色参考图与设定修正以上偏差】"
                    )
                    try:
                        redraw_res = await self.generate_character(
                            character,
                            corrective_prompt,
                            event,
                            _redraw_attempt=1,
                        )
                        if behavior:
                            await behavior.arecord(
                                audit_event,
                                "character_check",
                                "submitted" if not redraw_res.get("error") else "failed",
                                "character_check_auto_redraw",
                                {
                                    "character": character,
                                    "source_task_id": result.get("task_id", ""),
                                    "redraw_task_id": redraw_res.get("task_id", ""),
                                    "detail": detail,
                                },
                            )
                        return
                    except Exception as exc:
                        logger.warning(f"[VisualMedia] 角色核对自动重画触发失败: {exc}")
                await _notify_mismatch(check, auto_redrawn=_redraw_attempt > 0)

        async def completed(result):
            success = result.get("status") in {"succeeded", "partial_success"}
            task_key = result.get("task_id")
            if behavior:
                await behavior.arecord(
                    audit_event,
                    "generate",
                    "generated" if success else "failed",
                    "character_generation",
                    {
                        "character": character,
                        "task_id": result.get("task_id", ""),
                        "reference_count": len(refs),
                    },
                )
            if event is None:
                if tasks and task_key:
                    await tasks.observe(
                        task_key,
                        {
                            "job_id": result.get("job_id", ""),
                            "stage": "ready" if success else "generation_failed",
                        },
                    )
                if success:
                    task = asyncio.create_task(_run_post_delivery_check(result))
                    pending = _pending_checks()
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                return

            if tasks and task_key:
                await tasks.observe(
                    task_key,
                    {
                        "job_id": result.get("job_id", ""),
                        "stage": "delivering" if success else "generation_failed",
                    },
                )
            text = result.get("text_content") or ""
            payload = (
                (
                    result.get("image_urls") or [],
                    result.get("image_paths") or [],
                    text,
                    None,
                )
                if success
                else result.get("error") or "图片生成失败"
            )
            delivered = await dispatch_generation_result(
                generation,
                event,
                success,
                payload,
                scene="character",
                notify_llm=True,
                task_id=result.get("task_id") or None,
            )
            if tasks and task_key and success:
                await tasks.observe(
                    task_key,
                    {"stage": "delivered" if delivered else "delivery_failed"},
                )
            if success:
                # Run character verification after image delivery so extra vision latency never blocks output.
                task = asyncio.create_task(_run_post_delivery_check(result))
                pending = _pending_checks()
                pending.add(task)
                task.add_done_callback(pending.discard)

        completed._visual_callback_timeout = 180

        result = await generation.get_service().submit(
            plugin_id=self.owner.name,
            plugin_name="视觉工坊 · 角色创作",
            prompt=prepared,
            umo=event.unified_msg_origin if event else ((_recovery or {}).get("target") or None),
            requester=requester_from_event(event) if event else (_recovery or {}).get("requester"),
            reference_images=refs,
            use_rate_limit=True,
            on_complete=completed,
        )
        if event is not None:
            event.set_extra("visual_media_generation_requested", True)
        if tasks:
            job_id = result.get("job_id")
            if not job_id:
                # The public submission receipt keeps its task-only shape.
                background = getattr(generation, "background_task_manager", None)
                if background is not None:
                    try:
                        record = await background.get_for_plugin(result["task_id"], self.owner.name)
                        job_id = record.get("job_id") if record else None
                    except (OSError, ValueError):
                        logger.warning("[VisualMedia] character job link unavailable at submit")
            await tasks.observe(result["task_id"],{"task_id":result["task_id"],"character":character,"profile":profile,
                **({"job_id":job_id} if job_id else {}),
                "user_prompt":prompt,"prepared_prompt":prepared,"stage":"queued",
                "target":event.unified_msg_origin if event else (_recovery or {}).get("target",""),
                "manual_retry":bool(_recovery),"retry_of":(_recovery or {}).get("retry_of","")},defaults=True)
        if behavior:
            await behavior.arecord(audit_event, "generate", "submitted", "character_task_queued", {"character":character,"task_id":result["task_id"],"reference_count":len(refs),"style":profile.get("style","")[:300]})
        return {
            "task_id": result["task_id"],
            "status": "queued",
            "message": "角色图已提交，可在创作台查看进度。",
        }

    async def close(self):
        pending = getattr(self, "_check_tasks", None)
        if not pending:
            return
        tasks = [task for task in list(pending) if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        pending.clear()
