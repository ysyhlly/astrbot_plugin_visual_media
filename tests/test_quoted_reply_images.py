from __future__ import annotations

import base64
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)
_TINY_PNG_B64 = base64.b64encode(_TINY_PNG).decode("ascii")


class _StubImage:
    def __init__(self, file=None, url=None):
        self.file = file
        self.url = url


class _StubAt:
    def __init__(self, qq=None):
        self.qq = qq


class _StubReply:
    def __init__(self, id=None, chain=None):
        self.id = id
        self.chain = chain


@pytest.fixture
def image_handler_cls(monkeypatch):
    mc_mod = types.ModuleType("astrbot.api.message_components")
    mc_mod.At = _StubAt
    mc_mod.Image = _StubImage
    mc_mod.Reply = _StubReply
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", mc_mod)

    tl_utils_mod = sys.modules["tl.tl_utils"]

    class _StubAvatarManager:
        async def get_avatar(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(tl_utils_mod, "AvatarManager", _StubAvatarManager, raising=False)
    monkeypatch.setattr(
        tl_utils_mod,
        "is_valid_base64_image_str",
        lambda val: isinstance(val, str) and val.startswith("iVBOR"),
        raising=False,
    )

    name = "tl._quoted_reply_test_image_handler"
    source = Path(__file__).resolve().parents[1] / "tl" / "image_handler.py"
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module.ImageHandler


class _StubAPIClient:
    async def _normalize_reference_image_input(self, candidate, image_input_mode="force_base64"):
        path = Path(str(candidate))
        if path.is_file():
            return "image/png", base64.b64encode(path.read_bytes()).decode("ascii")
        return None, None


class _MockEvent:
    def __init__(self, components):
        self.message_obj = types.SimpleNamespace(message=list(components), self_id="bot")

    def get_messages(self):
        return list(self.message_obj.message)

    def get_self_id(self):
        return "bot"


@pytest.mark.asyncio
async def test_fetch_images_from_reply_embedded_image(image_handler_cls, tmp_path):
    img_file = tmp_path / "quoted.png"
    img_file.write_bytes(_TINY_PNG)
    img_comp = _StubImage(file=str(img_file), url=str(img_file))
    reply_comp = _StubReply(id="1165912249", chain=[img_comp])
    handler = image_handler_cls(api_client=_StubAPIClient())

    images, avatars = await handler.fetch_images_from_event(_MockEvent([reply_comp]))
    assert images == [_TINY_PNG_B64]
    assert avatars == []


@pytest.mark.asyncio
async def test_fetch_images_from_reply_forward_nodes(image_handler_cls, tmp_path):
    img_file = tmp_path / "forward.png"
    img_file.write_bytes(_TINY_PNG)
    img_comp = _StubImage(file=str(img_file), url=str(img_file))

    class Node:
        def __init__(self, content):
            self.content = content

    class Forward:
        def __init__(self, nodes):
            self.nodes = nodes

    reply_comp = _StubReply(id="1165912250", chain=[Forward([Node([img_comp])])])
    handler = image_handler_cls(api_client=_StubAPIClient())

    images, _avatars = await handler.fetch_images_from_event(_MockEvent([reply_comp]))
    assert images == [_TINY_PNG_B64]


@pytest.mark.asyncio
async def test_fetch_images_from_reply_id_only_fallback(image_handler_cls, tmp_path, monkeypatch):
    img_file = tmp_path / "fallback.png"
    img_file.write_bytes(_TINY_PNG)
    reply_comp = _StubReply(id="1165912251", chain=[])
    handler = image_handler_cls(api_client=_StubAPIClient())

    async def _fake_extract(event, comp):
        assert comp is reply_comp
        return [str(img_file)]

    stub_mod = types.ModuleType("astrbot.core.utils.quoted_message_parser")
    stub_mod.extract_quoted_message_images = _fake_extract
    monkeypatch.setitem(sys.modules, "astrbot.core.utils.quoted_message_parser", stub_mod)

    images, _avatars = await handler.fetch_images_from_event(_MockEvent([reply_comp]))
    assert images == [_TINY_PNG_B64]
