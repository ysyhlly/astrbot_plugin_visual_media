"""Dashboard API for the integrated library; reuse unified series admin & CSRF guards."""

import asyncio

from astrbot.api.web import json_response, request

try:
    from .series_panels import require_series_admin
except ImportError:
    from series_panels import require_series_admin


class MediaAPI:
    def __init__(self, owner):
        self.owner = owner

    def register(self, context):
        for path, handler, methods in [
            ("/overview", self.overview, ["GET"]),
            ("/save", self.save, ["POST"]),
            ("/profiles", self.profiles, ["GET", "POST", "DELETE"]),
            ("/generate", self.generate, ["POST"]),
            ("/character-pack", self.character_pack, ["POST"]),
            ("/character-pack/commit", self.character_pack_commit, ["POST"]),
            ("/quick-steal", self.quick_steal, ["POST"]),
            ("/platform-matrix", self.platform_matrix, ["GET"]),
            ("/behavior", self.behavior, ["GET"]),
            ("/behavior/reset", self.reset_preference, ["POST"]),
            ("/references", self.references, ["GET"]),
            ("/reference", self.reference_image, ["GET"]),
            ("/tasks", self.tasks, ["GET"]),
            ("/tasks/recover", self.recover_task, ["POST"]),
            ("/task-preview", self.task_preview, ["GET"]),
        ]:
            context.register_web_api(
                "/astrbot_plugin_visual_media/media" + path,
                handler,
                methods,
                "Visual Media library",
            )

    async def guard(self, *, check_csrf: bool = True):
        try:
            await require_series_admin(
                getattr(self.owner, "context", None),
                request,
                check_csrf=check_csrf,
            )
        except PermissionError as exc:
            return json_response(
                {"status": "error", "message": str(exc)}, status_code=403
            )
        if self.owner._closed:
            return json_response(
                {"status": "error", "message": "插件正在关闭"}, status_code=503
            )
        return None

    async def overview(self):
        if (denied := await self.guard(check_csrf=False)) is not None:
            return denied
        gen = self.owner.generation
        umo = str(request.query.get("umo", "") or "").strip()
        group_id = ""
        if umo:
            parts = umo.split(":", 2)
            if len(parts) == 3 and "group" in parts[1].lower():
                group_id = parts[2].strip()
        history = gen.generation_tracker.query_history(
            page=1, size=50, keyword="", source="", group_id=group_id, user_id=""
        )
        if umo and isinstance(history, dict) and isinstance(history.get("items"), list):
            filtered = [
                item
                for item in history["items"]
                if str((item.get("requester") or {}).get("umo") or "").strip() == umo
                or (
                    not str((item.get("requester") or {}).get("umo") or "").strip()
                    and group_id
                    and str((item.get("requester") or {}).get("group_id") or "").strip()
                    == group_id
                )
            ]
            history = {**history, "items": filtered, "total": len(filtered)}
        return json_response(
            {
                "status": "ok",
                "data": {
                    "memes": self.owner.memes.db_service.count_total(),
                    "steal_enabled": bool(getattr(self.owner.memes, "steal_meme", False)),
                    "generation": gen.get_service().get_status(),
                    "history": history,
                    "profiles": self.owner.library.profiles(),
                },
            }
        )

    async def references(self):
        if (denied := await self.guard(check_csrf=False)) is not None:
            return denied
        try:
            page = max(1, min(10000, int(request.query.get("page", "1"))))
            query = str(request.query.get("q", ""))[:100].casefold()
        except (TypeError, ValueError):
            return json_response(
                {"status": "error", "message": "分页参数无效"}, status_code=400
            )

        def listing():
            root = self.owner.memes.base_dir.resolve()
            seen = set()
            items = []
            for path, meta in list(self.owner.memes.plugin_api._get_index().items()):
                image_hash = str(meta.get("hash", ""))
                if meta.get("scope_mode") not in {"public", "reference"} or image_hash in seen:
                    continue
                from pathlib import Path

                image = Path(path).resolve()
                if not image.is_relative_to(root) or not image.is_file():
                    continue
                desc = str(meta.get("desc") or image_hash)[:200]
                character = str(meta.get("character") or "")[:80]
                if (
                    query
                    and query
                    not in (desc + " " + character + " " + image_hash).casefold()
                ):
                    continue
                seen.add(image_hash)
                items.append(
                    {"hash": image_hash, "description": desc, "character": character}
                )
            items.sort(key=lambda x: (x["character"], x["hash"]))
            return {
                "items": items[(page - 1) * 24 : page * 24],
                "total": len(items),
                "page": page,
                "size": 24,
            }

        return json_response({"status": "ok", "data": await asyncio.to_thread(listing)})

    async def reference_image(self):
        if (denied := await self.guard(check_csrf=False)) is not None:
            return denied
        import base64
        import io

        from PIL import Image

        try:
            image_hash = str(request.query.get("hash", ""))
            size = max(48, min(960, int(request.query.get("size", "240"))))

            def load():
                path = self.owner.library.reference_path(image_hash)
                if path.stat().st_size > 32 * 1024 * 1024:
                    raise ValueError("参考图文件过大")
                with Image.open(path) as source:
                    if source.width * source.height > 40_000_000:
                        raise ValueError("参考图尺寸过大")
                    source.seek(0)
                    source.thumbnail((size, size))
                    out = io.BytesIO()
                    source.convert("RGBA").save(out, format="PNG")
                    return {
                        "url": "data:image/png;base64,"
                        + base64.b64encode(out.getvalue()).decode()
                    }

            return json_response(
                {"status": "ok", "data": await asyncio.to_thread(load)}
            )
        except (ValueError, OSError, TypeError):
            return json_response(
                {"status": "error", "message": "参考图不存在、不再公开或无法预览"},
                status_code=404,
            )

    async def tasks(self):
        if (denied := await self.guard(check_csrf=False)) is not None:
            return denied
        try:
            job_id = request.query.get("job_id")
            result = (
                await self.owner.tasks.detail(job_id)
                if job_id
                else await self.owner.tasks.listing(
                    max(1, min(10000, int(request.query.get("page", "1")))),
                    max(1, min(50, int(request.query.get("size", "20")))),
                )
            )
            return json_response({"status": "ok", "data": result})
        except (ValueError, TypeError) as exc:
            return json_response(
                {"status": "error", "message": str(exc)}, status_code=400
            )

    async def task_preview(self):
        if (denied := await self.guard(check_csrf=False)) is not None:
            return denied
        import base64
        import io

        from PIL import Image

        name = str(request.query.get("name", ""))

        def load():
            path = self.owner.generation.generation_tracker.gallery_image_path(name)
            if path is None:
                raise ValueError("图片已清理")
            with Image.open(path) as img:
                if img.width * img.height > 40_000_000:
                    raise ValueError("图片尺寸过大")
                img.thumbnail((256, 256))
                out = io.BytesIO()
                img.convert("RGBA").save(out, format="PNG")
                return {
                    "url": "data:image/png;base64,"
                    + base64.b64encode(out.getvalue()).decode()
                }

        try:
            return json_response(
                {"status": "ok", "data": await asyncio.to_thread(load)}
            )
        except (ValueError, OSError):
            return json_response(
                {"status": "error", "message": "图片预览不可用"}, status_code=404
            )

    async def recover_task(self):
        if (denied := await self.guard(check_csrf=True)) is not None:
            return denied
        try:
            data = await request.json()
            if not isinstance(data, dict) or not isinstance(data.get("job_id"), str):
                raise ValueError("任务参数无效")
            result = await self.owner.tasks.recover(
                data["job_id"], data.get("action"), data.get("request_id")
            )
            return json_response({"status": "ok", "data": result})
        except (ValueError, TypeError) as exc:
            return json_response(
                {"status": "error", "message": str(exc)}, status_code=400
            )

    async def _umo_alias_map(self) -> dict[str, str]:
        """Best-effort map of UMO -> human-readable group/session alias from AstrBot DB."""
        ctx = getattr(self.owner, "context", None)
        get_db = getattr(ctx, "get_db", None)
        if not callable(get_db):
            return {}
        try:
            db = get_db()
            alias_getter = getattr(db, "get_umo_aliases", None)
            if not callable(alias_getter):
                return {}
            aliases = await alias_getter()
        except Exception:
            return {}
        mapping: dict[str, str] = {}
        for alias in aliases or ():
            umo = str(getattr(alias, "umo", "") or "").strip()
            display = str(
                getattr(alias, "user_alias", "")
                or getattr(alias, "auto_name", "")
                or ""
            ).strip()
            if umo and display:
                mapping[umo] = display[:80]
        return mapping

    async def behavior(self):
        if (denied := await self.guard(check_csrf=False)) is not None:
            return denied
        store = self.owner.behavior
        records, preferences, scopes, alias_map = await asyncio.gather(
            asyncio.to_thread(store.records, 200),
            asyncio.to_thread(store.preference_rows),
            asyncio.to_thread(store.scope_summaries),
            self._umo_alias_map(),
        )
        if alias_map:
            for item in scopes:
                umo = str(item.get("umo") or "")
                alias = alias_map.get(umo)
                if alias and (
                    not item.get("label")
                    or store._is_generic_scope_label(item["label"])
                ):
                    item["label"] = alias
            label_by_scope = {
                str(item.get("scope") or ""): str(item.get("label") or "")
                for item in scopes
                if item.get("scope") and item.get("label")
            }
            for collection in (records, preferences):
                for row in collection:
                    sc = str(row.get("scope") or "")
                    umo = str(row.get("scope_umo") or "")
                    better = alias_map.get(umo) or label_by_scope.get(sc) or ""
                    if better and (
                        not row.get("scope_label")
                        or store._is_generic_scope_label(row["scope_label"])
                    ):
                        row["scope_label"] = better
        settings = store.settings.public()
        settings["exact_repeat_minutes"] = settings["repeat_window_seconds"] / 60
        settings["dropped_audit_records"] = store.dropped_records
        joint_prefetch = (
            store.joint_prefetch_summary()
            if callable(getattr(store, "joint_prefetch_summary", None))
            else {}
        )
        return json_response(
            {
                "status": "ok",
                "data": {
                    "records": records,
                    "preferences": preferences,
                    "scopes": scopes,
                    "policy": settings,
                    "joint_prefetch": joint_prefetch,
                },
            }
        )

    async def reset_preference(self):
        if (denied := await self.guard(check_csrf=True)) is not None:
            return denied
        import re

        data = await request.json()
        if not isinstance(data, dict) or not all(
            isinstance(data.get(k), str) and re.fullmatch(r"[0-9a-f]{24}", data[k])
            for k in ("scope", "member")
        ):
            return json_response(
                {"status": "error", "message": "会话或成员标识无效"}, status_code=400
            )
        await self.owner.behavior.reset_async(data["scope"], data["member"])
        return json_response({"status": "ok", "data": {"reset": True}})

    async def save(self):
        if (denied := await self.guard(check_csrf=True)) is not None:
            return denied
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("请求必须是对象")
            result = await self.owner.library.save_generation(
                data.get("generation_id"),
                data.get("image_index", 0),
                character=data.get("character", ""),
                description=data.get("description", ""),
                public=data.get("public") is True,
            )
            return json_response({"status": "ok", "data": result})
        except (ValueError, PermissionError) as exc:
            return json_response(
                {"status": "error", "message": str(exc)}, status_code=400
            )

    async def profiles(self):
        if (denied := await self.guard(check_csrf=True)) is not None:
            return denied
        try:
            if request.method == "GET":
                result = self.owner.library.profiles()
            else:
                data = await request.json()
                if not isinstance(data, dict):
                    raise ValueError("请求必须是对象")
                if request.method == "DELETE":
                    await self.owner.library.delete_profile(str(data.get("key", "")))
                    result = {"deleted": True}
                else:
                    result = await self.owner.library.save_profile(data)
            return json_response({"status": "ok", "data": result})
        except ValueError as exc:
            return json_response(
                {"status": "error", "message": str(exc)}, status_code=400
            )

    async def generate(self):
        if (denied := await self.guard(check_csrf=True)) is not None:
            return denied
        from .tl.plugin_service import PluginServiceError

        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("请求必须是对象")
            result = await self.owner.library.generate_character(
                data.get("character"), data.get("prompt")
            )
            if "error" in result:
                raise ValueError(result["error"])
            return json_response({"status": "ok", "data": result})
        except (ValueError, PluginServiceError) as exc:
            return json_response(
                {"status": "error", "message": str(exc)}, status_code=400
            )

    def _character_pack_service(self):
        svc = getattr(self.owner, "character_pack", None)
        if svc is None:
            from .core.processing.character_pack_service import CharacterPackService

            svc = CharacterPackService(self.owner)
            self.owner.character_pack = svc
        return svc

    async def character_pack(self):
        if (denied := await self.guard(check_csrf=True)) is not None:
            return denied
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("请求必须是对象")
            svc = self._character_pack_service()
            result = await svc.generate_pack(
                character=str(data.get("character") or "").strip(),
                emotions=data.get("emotions"),
                style=str(data.get("style") or "").strip(),
                preview_only=bool(data.get("preview_only", False)),
                scope_mode=str(data.get("scope_mode") or "public").strip(),
                origin_target=str(data.get("origin_target") or "").strip(),
                umo=str(data.get("umo") or "").strip(),
            )
            return json_response({"status": "ok", "data": result})
        except (ValueError, RuntimeError) as exc:
            return json_response(
                {"status": "error", "message": str(exc)}, status_code=400
            )
        except Exception as exc:
            return json_response(
                {"status": "error", "message": f"角色表情套图生成失败: {exc}"},
                status_code=500,
            )

    async def character_pack_commit(self):
        if (denied := await self.guard(check_csrf=True)) is not None:
            return denied
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("请求必须是对象")
            svc = self._character_pack_service()
            result = await svc.commit_pack(
                pack_id=str(data.get("pack_id") or "").strip(),
                indices=data.get("indices"),
                scope_mode=data.get("scope_mode"),
                origin_target=data.get("origin_target"),
            )
            return json_response({"status": "ok", "data": result})
        except ValueError as exc:
            return json_response(
                {"status": "error", "message": str(exc)}, status_code=400
            )

    async def quick_steal(self):
        if (denied := await self.guard(check_csrf=True)) is not None:
            return denied
        try:
            data = await request.json()
            enabled = True if not isinstance(data, dict) else bool(data.get("enabled", True))
            memes = self.owner.memes
            memes.steal_meme = enabled
            cfg = getattr(memes, "plugin_config", None)
            if cfg is not None:
                cfg.steal_meme = enabled
                if hasattr(cfg, "config") and isinstance(cfg.config, dict):
                    cfg.config["steal_meme"] = enabled
                    save = getattr(cfg.config, "save_config", None)
                    if callable(save):
                        save()
            raw_cfg = getattr(self.owner, "raw_config", None)
            if isinstance(raw_cfg, dict):
                raw_cfg.setdefault("memes", {})["steal_meme"] = enabled
                save_raw = getattr(raw_cfg, "save_config", None)
                if callable(save_raw):
                    save_raw()
            return json_response(
                {"status": "ok", "data": {"steal_enabled": enabled}}
            )
        except Exception as exc:
            return json_response(
                {"status": "error", "message": str(exc)}, status_code=400
            )

    async def platform_matrix(self):
        if (denied := await self.guard(check_csrf=False)) is not None:
            return denied
        from .core.util.platform_capabilities import list_platform_capability_matrix

        return json_response(
            {"status": "ok", "data": {"platforms": list_platform_capability_matrix()}}
        )
