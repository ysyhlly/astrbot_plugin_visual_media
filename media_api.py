"""Dashboard API for the integrated library; reuse host authorization guards."""

import asyncio

from astrbot.api.web import json_response, request

from .tl.web_api import WebStudioAPI


class MediaAPI:
    def __init__(self, owner):
        self.owner = owner

    def register(self, context):
        for path, handler, methods in [
            ("/overview", self.overview, ["GET"]),
            ("/save", self.save, ["POST"]),
            ("/profiles", self.profiles, ["GET", "POST", "DELETE"]),
            ("/generate", self.generate, ["POST"]),
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

    def guard(self):
        denied = WebStudioAPI._access_guard("/providers")
        if denied is not None:
            return denied
        if self.owner._closed:
            return json_response(
                {"status": "error", "message": "插件正在关闭"}, status_code=503
            )
        return None

    async def overview(self):
        if (denied := self.guard()) is not None:
            return denied
        gen = self.owner.generation
        history = gen.generation_tracker.query_history(
            page=1, size=50, keyword="", source="", group_id="", user_id=""
        )
        return json_response(
            {
                "status": "ok",
                "data": {
                    "memes": self.owner.memes.db_service.count_total(),
                    "generation": gen.get_service().get_status(),
                    "history": history,
                    "profiles": self.owner.library.profiles(),
                },
            }
        )

    async def references(self):
        if (denied := self.guard()) is not None:
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
                if meta.get("scope_mode") != "public" or image_hash in seen:
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
        if (denied := self.guard()) is not None:
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
        if (denied := self.guard()) is not None:
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
        if (denied := self.guard()) is not None:
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
        if (denied := self.guard()) is not None:
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

    async def behavior(self):
        if (denied := self.guard()) is not None:
            return denied
        store = self.owner.behavior
        records, preferences = await asyncio.gather(
            asyncio.to_thread(store.records, 200),
            asyncio.to_thread(store.preference_rows),
        )
        settings = store.settings.public()
        settings["exact_repeat_minutes"] = settings["repeat_window_seconds"] / 60
        settings["dropped_audit_records"] = store.dropped_records
        return json_response(
            {
                "status": "ok",
                "data": {
                    "records": records,
                    "preferences": preferences,
                    "policy": settings,
                },
            }
        )

    async def reset_preference(self):
        if (denied := self.guard()) is not None:
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
        if (denied := self.guard()) is not None:
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
        if (denied := self.guard()) is not None:
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
        if (denied := self.guard()) is not None:
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
