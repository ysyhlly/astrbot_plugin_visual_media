import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from astrbot_plugin_visual_media.core.search.meme_scope_service import MemeScopeService
from astrbot_plugin_visual_media.runtime import ConfigSection


class Event:
    def __init__(self, umo="qq:GroupMessage:1"):
        self.unified_msg_origin = umo
        self.extra = {}

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value


async def generated(owner, umo="qq:GroupMessage:1", color="green"):
    tracker = owner.generation.generation_tracker
    record = await tracker.begin(
        source="test",
        prompt="A cheerful character",
        params={},
        requester={"umo": umo} if umo else {},
    )
    name = record["job_id"] + ".png"
    tracker.gallery_dir.mkdir(exist_ok=True)
    Image.new("RGB", (32, 32), color).save(tracker.gallery_dir / name)
    await tracker.complete(
        record["job_id"], image_files=[name], text_content="", stats={}
    )
    return record["job_id"], name


@pytest.mark.asyncio
async def test_generated_meme_roundtrip_dedup_and_retention(owner):
    job, name = await generated(owner)
    saved = await owner.library.save_generation(job, event=Event())
    again = await owner.library.save_generation("gen:" + job, event=Event())
    assert saved == again
    assert owner.memes.db_service.count_total() == 1
    path, meta = owner.memes.plugin_api._find_index_entry_by_hash(saved["hash"])
    assert meta["scope_mode"] == "local"
    assert meta["origin_target"] == "umo:qq:GroupMessage:1"
    assert meta["is_favorite"] == 1
    visibility = MemeScopeService(owner.memes)
    assert visibility._is_entry_allowed_for_event(meta, Event())
    assert not visibility._is_entry_allowed_for_event(meta, Event("qq:GroupMessage:2"))
    assert not visibility._is_entry_allowed_for_event(
        meta, Event("other:GroupMessage:1")
    )
    await owner.generation.generation_tracker.delete([job])
    assert Path(path).is_file(), "Gallery deletion must not delete a saved meme"
    with Image.open(path) as image:
        image.verify()


@pytest.mark.asyncio
async def test_concurrent_save_is_idempotent(owner):
    job, _ = await generated(owner)
    results = await asyncio.gather(
        *(owner.library.save_generation(job, event=Event()) for _ in range(8))
    )
    assert len({item["hash"] for item in results}) == 1
    assert owner.memes.db_service.count_total() == 1


@pytest.mark.asyncio
async def test_foreign_generation_cannot_be_imported(owner):
    job, _ = await generated(owner)
    with pytest.raises(PermissionError):
        await owner.library.save_generation(
            job, event=Event("qq:FriendMessage:2"), public=True
        )
    assert owner.memes.db_service.count_total() == 0


@pytest.mark.asyncio
async def test_same_bytes_in_two_scopes_do_not_broaden_access(owner):
    first, _ = await generated(owner)
    second, _ = await generated(owner, "qq:FriendMessage:2")
    await owner.library.save_generation(first, event=Event())
    await owner.library.save_generation(second, event=Event("qq:FriendMessage:2"))
    rows = owner.memes.plugin_api._get_index()
    assert len(rows) == 2
    assert {row["origin_target"] for row in rows.values()} == {
        "umo:qq:GroupMessage:1",
        "umo:qq:FriendMessage:2",
    }


@pytest.mark.asyncio
async def test_web_image_requires_explicit_public_choice(owner):
    job, _ = await generated(owner, "")
    with pytest.raises(ValueError, match="公开"):
        await owner.library.save_generation(job)
    saved = await owner.library.save_generation(job, public=True)
    _, meta = owner.memes.plugin_api._find_index_entry_by_hash(saved["hash"])
    assert meta["scope_mode"] == "public"


@pytest.mark.asyncio
async def test_profile_references_require_public_files_and_persist(owner):
    job, _ = await generated(owner)
    saved = await owner.library.save_generation(job, event=Event())
    with pytest.raises(ValueError, match="公开"):
        await owner.library.save_profile({"key": "anan", "references": [saved["hash"]]})
    public_job, _ = await generated(owner, color="blue")
    public = await owner.library.save_generation(public_job, public=True)
    profile = await owner.library.save_profile(
        {
            "key": "anan",
            "name": "安安",
            "appearance": "蓝色头发",
            "references": [public["hash"]],
        }
    )
    assert owner.library.profiles() == [profile]
    assert "anan" in owner.memes.plugin_config.get_characters()
    from astrbot_plugin_visual_media.media_library import MediaLibrary

    assert MediaLibrary(owner).profiles() == [profile]


@pytest.mark.asyncio
async def test_invalid_record_and_image_are_rejected(owner):
    job, name = await generated(owner)
    for index in [-1, True, "0", 99]:
        with pytest.raises(ValueError):
            await owner.library.save_generation(job, index, event=Event())
    (owner.generation.generation_tracker.gallery_dir / name).unlink()
    with pytest.raises(ValueError):
        await owner.library.save_generation(job, event=Event())


def test_config_saves_preserve_sibling_and_rollback():
    class Config(dict):
        broken = False
        _save_revision = 0

        def save_config(self):
            if self.broken:
                raise OSError("disk full")
            self._save_revision += 1

    parent = Config(
        memes={"chance": 0.2}, generation={"provider_settings": {"model": "a"}}
    )
    memes, generation = (
        ConfigSection(parent, "memes"),
        ConfigSection(parent, "generation"),
    )
    memes.save_config({"chance": 0.3})
    generation.save_config({"provider_settings": {"model": "b"}})
    assert parent == {
        "memes": {"chance": 0.3},
        "generation": {"provider_settings": {"model": "b"}},
    }
    parent.broken = True
    with pytest.raises(OSError):
        memes.save_config({"chance": 0.9})
    assert parent["memes"]["chance"] == memes["chance"] == 0.3
    assert generation._save_revision == 2


@pytest.mark.asyncio
async def test_generation_reservation_suppresses_passive_meme(owner):
    event = Event()
    event.set_extra("visual_media_generation_requested", True)
    owner.memes._prepare_emoji_after_llm = lambda *args: pytest.fail(
        "Passive meme entered generation turn"
    )
    await owner._prepare_emoji_after_llm(event, SimpleNamespace())
    assert not event.get_extra("stealer_active_sent"), (
        "Reservation is not a send receipt"
    )


def test_generation_invalidates_already_prefetched_joint_image(tmp_path):
    from astrbot_plugin_visual_media.core.tools.joint_reply import JointMemePlan

    path = tmp_path / "candidate.png"
    path.write_bytes(b"fixture")
    event = Event()
    plugin = SimpleNamespace(
        plugin_config=SimpleNamespace(),
        is_send_enabled_for_event=lambda event: True,
        meme_selector=SimpleNamespace(
            is_path_allowed_for_event=lambda path, event: True
        ),
    )
    plan = JointMemePlan(plugin, event, [{"path": str(path)}])
    text, chains = plan.resolve('{"text":"好的","meme_id":1}')
    assert len(chains) == 1
    assert chains[0]._cd_media_validate_v1()
    event.set_extra("visual_media_generation_requested", True)
    assert not chains[0]._cd_media_validate_v1()
    assert plan.resolve('{"text":"好的","meme_id":1}') == ("好的", [])
