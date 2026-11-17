from pathlib import Path
import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from PIL import Image
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot.dashboard.asgi_runtime import FastAPIAppAdapter, call_request_view

from astrbot_plugin_visual_media.tl.plugin_config import ProviderCandidate
from astrbot_plugin_visual_media.tl.generation_scheduler import scheduled_generation
from test_media_library import Event, generated


def app_for(owner):
    app = FastAPI()
    adapter = FastAPIAppAdapter(app)

    def endpoint_for(handler):
        async def endpoint(request: Request):
            username = request.headers.get("x-test-user")
            wrapped = PluginRequest(request, plugin_name=owner.name, username=username)
            with bind_request_context(wrapped):
                return await call_request_view(
                    request, adapter, handler, dict(request.path_params)
                )

        return endpoint

    for route, handler, methods, _ in owner.context.registered_web_apis:
        path = route.replace("<name>", "{name}")
        app.add_api_route(path, endpoint_for(handler), methods=methods)
    return app


@pytest.mark.asyncio
async def test_real_http_library_and_studio_contracts(owner):
    app = app_for(owner)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        prefix = "/astrbot_plugin_visual_media"
        assert (await client.get(prefix + "/media/overview")).status_code == 403
        client.headers["x-test-user"] = "admin"
        overview = await client.get(prefix + "/media/overview")
        assert overview.status_code == 200
        assert overview.json()["data"]["memes"] == 0
        response = await client.get(prefix + "/images")
        assert response.status_code == 200
        assert response.json()["images"] == []
        assert (await client.get(prefix + "/webui/capabilities")).status_code == 200
        profile = await client.post(
            prefix + "/media/profiles", json={"key": "anan", "name": "安安"}
        )
        assert profile.status_code == 200
        assert profile.json()["data"]["key"] == "anan"
        job, name = await generated(owner)
        saved = await client.post(
            prefix + "/media/save", json={"generation_id": job, "character": "anan"}
        )
        assert saved.status_code == 200
        listing = (await client.get(prefix + "/images")).json()["images"]
        assert len(listing) == 1 and listing[0]["character"] == "anan"
        preview = await client.get(prefix + "/webui/image_b64/" + name)
        assert preview.status_code == 200
        assert preview.json()["data"]["b64"]
        for payload in [None, [], {"generation_id": job, "image_index": True}]:
            assert (
                await client.post(prefix + "/media/save", json=payload)
            ).status_code == 400
        client.headers["origin"] = "https://foreign.example"
        assert (
            await client.post(prefix + "/media/profiles", json={"key": "denied"})
        ).status_code == 403
        del client.headers["origin"]
        client.headers["x-test-user"] = "api_key:limited"
        assert (
            await client.post(prefix + "/media/profiles", json={"key": "denied"})
        ).status_code == 403


@pytest.mark.asyncio
async def test_generation_queue_to_gallery_to_searchable_meme(owner):
    generation = owner.generation
    source = Path(generation._plugin_data_dir) / "fixture.png"
    Image.new("RGB", (64, 64), "yellow").save(source)

    class Client:
        generation_scheduler = generation.generation_scheduler

        @scheduled_generation
        async def generate_image(self, config, **kwargs):
            config.successful_provider = "openai_images"
            config.successful_model = "fixture"
            return [], [str(source)], None, None

        async def close(self):
            pass

    generation.cfg.provider_candidates = [
        ProviderCandidate(
            id="openai_images#1",
            api_type="openai_images",
            settings={"model": "fixture", "api_keys": ["test-only"]},
        )
    ]
    generation.api_client = Client()
    generation._update_modules_api_client()
    service = owner.get_service()
    assert service.get_status()["ready"]
    task = await service.submit(
        plugin_id=owner.name,
        prompt="开心笑脸",
        umo="qq:GroupMessage:1",
        requester={"user_id": "1"},
    )
    result = await service.wait_task(task["task_id"], plugin_id=owner.name, timeout=10)
    assert result["status"] == "succeeded", result
    records = generation.generation_tracker.records_snapshot()
    completed = next(row for row in records if row["status"] == "succeeded")
    assert completed["images"]
    saved = await owner.library.save_generation(
        completed["job_id"], event=Event(), description="开心笑脸"
    )
    path, meta = owner.memes.plugin_api._find_index_entry_by_hash(saved["hash"])
    assert Path(path).is_file()
    assert meta["desc"] == "开心笑脸"
    # A successful SDK delivery can wake wait_task before scheduler finalization.
    async with asyncio.timeout(2):
        while generation.generation_scheduler.active:
            await asyncio.sleep(0.01)
    public = await owner.library.save_generation(completed["job_id"], public=True)
    await owner.library.save_profile(
        {
            "key": "anan",
            "appearance": "blue hair",
            "style": "watercolor",
            "references": [public["hash"]],
        }
    )
    task = await owner.library.generate_character("anan", "smiling")
    assert task["status"] == "queued"
    result = await service.wait_task(task["task_id"], plugin_id=owner.name, timeout=10)
    assert result["status"] == "succeeded", result
    assert any(
        "blue hair\nwatercolor\nsmiling" in row.get("prompt", "")
        for row in generation.generation_tracker.records_snapshot()
    )


@pytest.mark.asyncio
async def test_reload_closes_old_services_and_rebinds_routes(paths):
    from test_lifecycle import Host
    from astrbot_plugin_visual_media.main import VisualMediaPlugin
    from astrbot_plugin_visual_media.tl.plugin_service import PluginServiceError

    host = Host()
    first = VisualMediaPlugin(host, {})
    await first.initialize()
    old = first.get_service()
    count = len(host.registered_web_apis)
    await first.terminate()
    second = VisualMediaPlugin(host, {})
    await second.initialize()
    try:
        assert len(host.registered_web_apis) == count
        assert old.instance_id != second.get_service().instance_id
        with pytest.raises(PluginServiceError):
            await old.submit(plugin_id=first.name, prompt="must not submit")
    finally:
        await second.terminate()
