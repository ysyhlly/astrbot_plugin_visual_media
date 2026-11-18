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

from PIL import Image

from astrbot.api import logger

from .core.events.event_context import unwrap_event


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
        try:
            with db:
                yield db
        finally:
            db.close()

    def profiles(self):
        with self._connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT data FROM profiles ORDER BY key")
            ]

    def _rebuild_public_ref_cache(self, index: dict) -> None:
        self._public_ref_cache = {
            m.get("hash"): p
            for p, m in index.items()
            if isinstance(m, dict)
            and m.get("hash")
            and m.get("scope_mode") == "public"
        }
        self._public_ref_index_sig = (id(index), len(index))

    def reference_path(self, image_hash):
        index = self.owner.memes.plugin_api._get_index()
        sig = (id(index), len(index))
        if self._public_ref_index_sig != sig:
            self._rebuild_public_ref_cache(index)
        raw_path = self._public_ref_cache.get(image_hash)
        meta = index.get(raw_path) if raw_path else None
        if not (
            isinstance(meta, dict)
            and meta.get("hash") == image_hash
            and meta.get("scope_mode") == "public"
        ):
            self._rebuild_public_ref_cache(index)
            raw_path = self._public_ref_cache.get(image_hash)
            meta = index.get(raw_path) if raw_path else None
            if not (
                isinstance(meta, dict)
                and meta.get("hash") == image_hash
                and meta.get("scope_mode") == "public"
            ):
                raise ValueError("角色共用参考图必须是公开表情，请先在表情库确认可见范围")
        path = Path(raw_path).resolve()
        if (
            not path.is_relative_to(self.owner.memes.base_dir.resolve())
            or not path.is_file()
        ):
            raise ValueError("角色参考图不存在")
        return path

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
        profile = {
            "key": key,
            "name": str(data.get("name") or key)[:80],
            "appearance": str(data.get("appearance") or "")[:4000],
            "style": str(data.get("style") or "")[:2000],
            "references": list(dict.fromkeys(refs)),
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
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("图片超过表情入库大小上限")
        raw = path.read_bytes()
        with Image.open(io.BytesIO(raw)) as img:
            if img.width * img.height > 40_000_000:
                raise ValueError("图片像素数过大")
            image_format = str(img.format).lower()
            img.verify()
        suffix = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}.get(
            image_format
        )
        if not suffix:
            raise ValueError("图片格式不支持收藏")
        scope = "public" if public else "umo:" + umo
        digest = hashlib.sha256(raw).hexdigest()
        api = self.owner.memes.plugin_api
        async with self.lock:
            # Content deduplication is scoped: never broaden an existing private item.
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

    async def generate_character(self, character, prompt, event=None, *, _recovery=None):
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

        async def _notify_mismatch(check):
            if event is None or getattr(self.owner, "_closed", False):
                return
            if getattr(getattr(self.owner, "behavior", None), "stop_sends", False):
                return
            target = str(getattr(event, "unified_msg_origin", "") or "").strip()
            context = getattr(self.owner, "context", None)
            send_fn = getattr(context, "send_message", None)
            if not target or not callable(send_fn):
                return
            issues = [str(x).strip() for x in (check.get("issues") or []) if str(x).strip()]
            detail = "；".join(issues) if issues else str(check.get("reason") or "部分细节不一致")
            notice = f"角色形象核对提醒：{detail}。已发送的图片保留供查看，尚未自动重画。"
            try:
                from astrbot.api.event import MessageChain
                from astrbot.core.platform.message_session import MessageSession

                session = MessageSession.from_str(target)
                await asyncio.wait_for(
                    send_fn(session, MessageChain().message(notice)),
                    timeout=15,
                )
            except Exception as exc:
                logger.debug(f"[VisualMedia] 角色核对差异提醒发送失败: {exc}")

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
                        **check,
                    },
                )
            if check.get("status") == "mismatch":
                await _notify_mismatch(check)

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
