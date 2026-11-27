"""Focused regression tests for visual_media P0 (joint_prefetch / embedding) & P1 (openai_compat / maintenance / disk caps)."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _ensure_optional_stubs() -> None:
    """Install minimal stubs for optional runtime deps missing in bare unit-test venvs."""
    if "rank_bm25" not in sys.modules:
        try:
            import rank_bm25  # noqa: F401
        except ImportError:
            mod = types.ModuleType("rank_bm25")

            class _StubBM25Plus:
                def __init__(self, corpus):
                    self.corpus = [list(doc) for doc in corpus]

                def get_scores(self, tokens):
                    tok_set = set(tokens)
                    return [
                        float(len(tok_set & set(doc))) / max(1, len(doc))
                        for doc in self.corpus
                    ]

                def get_top_k(self, tokens, k=10):
                    scores = self.get_scores(tokens)
                    ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
                    return ranked[:k]

            mod.BM25Plus = _StubBM25Plus
            sys.modules["rank_bm25"] = mod

    if "rapidfuzz" not in sys.modules:
        try:
            import rapidfuzz  # noqa: F401
        except ImportError:
            rf = types.ModuleType("rapidfuzz")
            rf_dist = types.ModuleType("rapidfuzz.distance")
            rf_lev = types.ModuleType("rapidfuzz.distance.Levenshtein")

            def _lev_distance(a: str, b: str) -> int:
                if a == b:
                    return 0
                if not a:
                    return len(b)
                if not b:
                    return len(a)
                prev = list(range(len(b) + 1))
                for i, ca in enumerate(a, 1):
                    curr = [i]
                    for j, cb in enumerate(b, 1):
                        curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb)))
                    prev = curr
                return prev[-1]

            rf_lev.distance = _lev_distance
            rf_dist.Levenshtein = rf_lev
            rf.distance = rf_dist
            sys.modules["rapidfuzz"] = rf
            sys.modules["rapidfuzz.distance"] = rf_dist
            sys.modules["rapidfuzz.distance.Levenshtein"] = rf_lev

    if "astrbot.api.star" not in sys.modules:
        star_mod = types.ModuleType("astrbot.api.star")
        star_mod.StarTools = SimpleNamespace(
            get_data_dir=lambda name: Path(os.getcwd()) / ".tmp_test_data" / name
        )
        star_mod.Context = type("Context", (), {})
        sys.modules["astrbot.api.star"] = star_mod

    if "astrbot.api.event" not in sys.modules:
        ev_mod = types.ModuleType("astrbot.api.event")
        ev_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
        ev_mod.MessageChain = type("MessageChain", (), {})
        sys.modules["astrbot.api.event"] = ev_mod

    if "astrbot.api.message_components" not in sys.modules:
        comp_mod = types.ModuleType("astrbot.api.message_components")
        comp_mod.Image = type("Image", (), {})
        comp_mod.Plain = type("Plain", (), {})
        sys.modules["astrbot.api.message_components"] = comp_mod
    else:
        comp_mod = sys.modules["astrbot.api.message_components"]
        if not hasattr(comp_mod, "Plain"):
            comp_mod.Plain = type("Plain", (), {})


_ensure_optional_stubs()


class _SafeTestLogger:
    def debug(self, msg, *args, **kwargs):
        return None

    def info(self, msg, *args, **kwargs):
        return None

    def warning(self, msg, *args, **kwargs):
        return None

    def error(self, msg, *args, **kwargs):
        return None


@pytest.fixture(autouse=True)
def _restore_safe_logger(monkeypatch):
    """Guard against earlier test files leaving a partial _DummyLogger on astrbot.api."""
    api_mod = sys.modules.get("astrbot.api")
    if api_mod is not None and not hasattr(getattr(api_mod, "logger", None), "info"):
        safe = _SafeTestLogger()
        monkeypatch.setattr(api_mod, "logger", safe, raising=False)
        for mod_name in (
            "core.tools.meme_search_tool",
            "core.search.meme_smart_select_service",
            "core.maintenance.service",
            "tl.api.openai_compat",
            "tl.tl_api",
        ):
            mod = sys.modules.get(mod_name)
            if mod is not None and not hasattr(getattr(mod, "logger", None), "info"):
                monkeypatch.setattr(mod, "logger", safe, raising=False)


# ---------------------------------------------------------------------------
# P0-1: Decoupled interactive embedding lane & LRU/TTL query embedding cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interactive_embedding_lane_bypasses_5s_background_gate():
    from core.search.embedding_provider_adapter import (
        EmbeddingProviderAdapter,
        interactive_embedding_lane,
    )
    from core.util.model_requests import model_request_gate

    plugin = SimpleNamespace(
        plugin_config=SimpleNamespace(model_request_interval=5.0),
    )

    class _FakeProvider:
        provider_config = {"id": "emb-p1"}

        def __init__(self):
            self.calls = 0

        def get_dim(self):
            return 3

        def get_model(self):
            return "text-embedding-v3"

        async def get_embedding(self, text):
            self.calls += 1
            await asyncio.sleep(0.02)
            return [1.0, 0.0, 0.0]

    class _FakeService:
        def __init__(self):
            self.plugin = plugin

        def _extract_provider_id(self, p):
            return p.provider_config["id"]

        def _provider_identity(self, p):
            return "emb-p1:v3"

        def _effective_dim(self, p):
            return 3

        def _record_dimension(self, p, dim):
            return None

    provider = _FakeProvider()
    service = _FakeService()
    gate = model_request_gate(plugin)

    # Trigger one background call to arm the 5.0s serial interval on emb-p1.
    bg_adapter = EmbeddingProviderAdapter(service, provider, interactive=False)
    await bg_adapter.get_embedding("backfill-doc-1")
    assert gate._next["emb-p1"] > time.monotonic() + 3.0

    # Two interactive searches must NOT wait 5.0s and should run concurrently.
    t0 = time.monotonic()
    with interactive_embedding_lane(True):
        v1, v2 = await asyncio.gather(
            bg_adapter.get_embedding("interactive-q1"),
            EmbeddingProviderAdapter(service, provider, interactive=True).get_embedding(
                "interactive-q2"
            ),
        )
    elapsed = time.monotonic() - t0
    assert v1 == [1.0, 0.0, 0.0]
    assert v2 == [1.0, 0.0, 0.0]
    assert elapsed < 0.5, f"Interactive lane blocked on 5s background gate: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_query_embedding_lru_ttl_cache_hits_evicts_and_expires(monkeypatch):
    from core.search.embedding_provider_adapter import (
        EmbeddingProviderAdapter,
        interactive_embedding_lane,
    )
    from core.search.embedding_service import EmbeddingService

    plugin = SimpleNamespace(
        plugin_config=SimpleNamespace(model_request_interval=5.0),
        enable_embedding_search=True,
    )
    service = EmbeddingService(plugin)
    service.QUERY_CACHE_MAX_ENTRIES = 2
    service.QUERY_CACHE_TTL_SECONDS = 10.0

    class _Provider:
        def __init__(self, pid: str):
            self.provider_config = {"id": pid}
            self.calls: list[str] = []

        def get_dim(self):
            return 2

        def get_model(self):
            return f"model-{self.provider_config['id']}"

        async def get_embedding(self, text):
            self.calls.append(text)
            return [float(len(self.calls)), 1.0]

    p1 = _Provider("p1")
    p2 = _Provider("p2")
    adapter1 = EmbeddingProviderAdapter(service, p1)
    adapter2 = EmbeddingProviderAdapter(service, p2)

    now = [1000.0]
    monkeypatch.setattr("core.search.embedding_service.time.monotonic", lambda: now[0])

    with interactive_embedding_lane(True):
        v1 = await adapter1.get_embedding("  hello   world  ")
        v2 = await adapter1.get_embedding("hello world")
        assert v1 == v2
        assert len(p1.calls) == 1

        # Different provider signature does not share cache entry
        v_p2 = await adapter2.get_embedding("hello world")
        assert len(p2.calls) == 1
        assert v_p2 == [1.0, 1.0]

        # LRU eviction: capacity=2, currently holds (p1, hello world) and (p2, hello world).
        # Touch (p1, hello world) so (p2, hello world) becomes LRU, then insert q2.
        await adapter1.get_embedding("hello world")
        await adapter1.get_embedding("second query")
        assert len(p1.calls) == 2
        # (p2, hello world) was evicted -> should refetch
        await adapter2.get_embedding("hello world")
        assert len(p2.calls) == 2

        # TTL expiration
        now[0] += 11.0
        await adapter1.get_embedding("second query")
        assert len(p1.calls) == 3

        # invalidate_cache clears query cache
        service.invalidate_cache()
        await adapter1.get_embedding("second query")
        assert len(p1.calls) == 4


# ---------------------------------------------------------------------------
# P0-2: MemeSearchToolWorkflow validates only top-K files, not full library
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meme_search_tool_validates_only_topk_files(tmp_path, monkeypatch):
    from core.tools.meme_search_tool import MemeSearchToolWorkflow

    existing_file = tmp_path / "good.png"
    existing_file.write_bytes(b"png")
    missing_file = tmp_path / "missing.png"

    idx = {str(tmp_path / f"item_{i}.png"): {"desc": f"meme {i}", "category": "happy"} for i in range(150)}
    idx[str(existing_file)] = {"desc": "good meme", "category": "happy", "tags": ["开心"]}
    idx[str(missing_file)] = {"desc": "missing meme", "category": "happy", "tags": ["开心"]}

    class _TurnState:
        def __init__(self):
            self.candidates = []

        def set_candidates(self, items):
            self.candidates = list(items)

        def get_candidates(self):
            return list(self.candidates)

    turn_state = _TurnState()

    class _Workflow(MemeSearchToolWorkflow):
        MAX_SEARCH_RESULTS = 5
        SEND_MEME_TOOL_NAME = "send_meme"

        def __init__(self):
            self.plugin_config = SimpleNamespace(character_info={})
            self.db_service = SimpleNamespace(
                count_total=lambda: len(idx),
                get_index_cache_readonly=lambda: idx,
                get_library_snapshot=lambda: (idx, 1),
            )
            self.index_manager = SimpleNamespace(_migration_done=True)
            self._emoji_sender_engine = SimpleNamespace(emoji_turn_state=lambda _ev: turn_state)
            self.meme_selector = SimpleNamespace(
                _is_entry_allowed_for_event=lambda _row, _ev: True,
                smart_search=AsyncMock(
                    return_value=[
                        (str(missing_file), "missing meme", "happy", "开心"),
                        (str(existing_file), "good meme", "happy", "开心"),
                    ]
                ),
            )

        def is_send_enabled_for_event(self, _ev):
            return True

    checked_paths: list[str] = []
    real_isfile = os.path.isfile

    def _spy_isfile(path):
        checked_paths.append(str(path))
        return real_isfile(path)

    monkeypatch.setattr("core.tools.meme_search_tool.os.path.isfile", _spy_isfile)

    wf = _Workflow()
    outcomes: list[str] = []
    outputs = [
        chunk
        async for chunk in wf._search_meme_impl(
            SimpleNamespace(), "开心", on_outcome=lambda o, _v: outcomes.append(o)
        )
    ]

    assert outputs
    assert outcomes == ["candidates"]
    assert len(turn_state.candidates) == 1
    assert turn_state.candidates[0]["path"] == str(existing_file)
    # Only the 2 top-K candidates were stat'd — NOT the 150-entry full library!
    assert len(checked_paths) == 2

    # When ALL returned top-K files are missing, report files_missing.
    wf.meme_selector.smart_search = AsyncMock(
        return_value=[(str(missing_file), "missing meme", "happy", "开心")]
    )
    outcomes.clear()
    outputs2 = [
        chunk
        async for chunk in wf._search_meme_impl(
            SimpleNamespace(), "开心", on_outcome=lambda o, _v: outcomes.append(o)
        )
    ]
    assert outcomes == ["files_missing"]
    assert any("丢失" in out for out in outputs2)


# ---------------------------------------------------------------------------
# P0-3: Concurrent local recall + timeout fallback + semantic pass-through + raw/accepted logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smart_select_concurrent_local_recall_returns_local_on_embedding_timeout():
    from core.search.meme_smart_select_service import MemeSmartSelectService

    class _SlowEmbedding:
        def is_available(self):
            return True

        async def search(self, query, k=48):
            await asyncio.sleep(2.0)
            return [("/tmp/emb.png", 0.9)]

    class _FakeSearchEngine:
        _bm25_signature = None
        _bm25_dirty = False
        _bm25_index = None
        _bm25_doc_paths: list[str] = []
        plugin = None

        async def _build_bm25_index(self, _idx):
            return None

    scope = SimpleNamespace(_is_entry_allowed_for_event=lambda _d, _e: True)
    strategy = SimpleNamespace(_calculate_recent_penalty=lambda _c, _p: 0.0)
    svc = MemeSmartSelectService(None, _FakeSearchEngine(), strategy, scope)
    svc._embedding_service = _SlowEmbedding()

    idx = {
        "/tmp/local_hit.png": {
            "overlay_text": "早安打工人",
            "desc": "清晨打招呼",
            "category": "happy",
            "tags": ["早安"],
            "scenes": ["早上问候"],
        }
    }

    t0 = time.monotonic()
    paths, emb_scores = await svc._recall_candidate_paths(
        idx, "早安打工人", None, set(), embedding_timeout=0.05
    )
    elapsed = time.monotonic() - t0

    assert elapsed < 0.35
    assert "/tmp/local_hit.png" in paths
    assert emb_scores == {}


@pytest.mark.asyncio
async def test_smart_select_semantic_passthrough_and_raw_accepted_logs(monkeypatch):
    from core.search import meme_smart_select_service as mod
    from core.search.meme_smart_select_service import MemeSmartSelectService

    logged: list[str] = []

    class _CaptureLogger:
        def info(self, msg, *args):
            logged.append(msg % args if args else str(msg))

        def debug(self, msg, *args):
            return None

        def warning(self, msg, *args):
            logged.append(msg % args if args else str(msg))

        def error(self, msg, *args):
            logged.append(msg % args if args else str(msg))

    monkeypatch.setattr(mod, "logger", _CaptureLogger())

    class _FakeEmbedding:
        def is_available(self):
            return True

        async def search(self, query, k=48):
            # One valid strong semantic hit (0.36, zero literal overlap),
            # one weak semantic noise (0.20, zero literal overlap),
            # and one drifted path not present in idx.
            return [
                ("/tmp/sem_good.png", 0.36),
                ("/tmp/sem_weak.png", 0.20),
                ("/tmp/drifted_missing.png", 0.88),
            ]

    class _FakeSearchEngine:
        _bm25_signature = None
        _bm25_dirty = False
        _bm25_index = None
        _bm25_doc_paths: list[str] = []
        plugin = None

        async def _build_bm25_index(self, _idx):
            return None

    scope = SimpleNamespace(_is_entry_allowed_for_event=lambda _d, _e: True)
    strategy = SimpleNamespace(_calculate_recent_penalty=lambda _c, _p: 0.0)
    svc = MemeSmartSelectService(None, _FakeSearchEngine(), strategy, scope)
    svc._embedding_service = _FakeEmbedding()

    idx = {
        "/tmp/sem_good.png": {"desc": "猫咪摊手", "category": "sigh", "tags": ["猫"]},
        "/tmp/sem_weak.png": {"desc": "小狗奔跑", "category": "happy", "tags": ["狗"]},
    }

    ranked = await svc._rank_emoji_candidates(
        "", "加班好累想摆烂", idx=idx, require_files=False, penalize_recent=False
    )
    ranked_paths = [item[0] for item in ranked]
    assert "/tmp/sem_good.png" in ranked_paths
    assert "/tmp/sem_weak.png" not in ranked_paths
    assert any("raw=3 accepted=2" in line for line in logged), logged
    assert any("raw=2 accepted=1" in line for line in logged), logged


@pytest.mark.asyncio
async def test_joint_prefetch_returns_local_fallback_when_wait_seconds_expires():
    from core.search.meme_smart_select_service import _EMBEDDING_TIMEOUT_VAR
    from core.tools.joint_prefetch import JointReplyPrefetch

    class _TurnState:
        def __init__(self):
            self.candidates = []

        def set_candidates(self, items):
            self.candidates = list(items)

        def get_candidates(self):
            return list(self.candidates)

    class _Event:
        unified_msg_origin = "g:1"

        def __init__(self):
            self._extras = {}
            self._emoji_turn_state = {}

        def get_platform_id(self):
            return "qq"

        def get_self_id(self):
            return "bot1"

        def get_sender_id(self):
            return "u1"

    class _Plugin:
        def __init__(self):
            self.plugin_config = SimpleNamespace(
                llm_joint_reply=True,
                auto_send_meme=True,
                get_event_target=lambda _e: ("group", "1"),
            )
            self.db_service = SimpleNamespace(get_library_version=lambda: 1)
            self.meme_selector = SimpleNamespace(
                is_path_allowed_for_event=lambda _p, _e: True
            )
            self._emoji_sender_engine = SimpleNamespace(
                emoji_turn_state=lambda ev: ev._emoji_turn_state.setdefault(
                    "state", _TurnState()
                )
            )

        def is_send_enabled_for_event(self, _ev):
            return True

        async def _search_meme_impl(self, ev, query, *, on_outcome=None):
            timeout_var = _EMBEDDING_TIMEOUT_VAR.get()
            if timeout_var is None or timeout_var > 0:
                await asyncio.sleep(2.0)
            self._emoji_sender_engine.emoji_turn_state(ev).set_candidates(
                [{"id": 1, "path": "/tmp/local_bm25.png", "desc": "本地命中"}]
            )
            if on_outcome:
                on_outcome("candidates", 1)
            yield "ok"

    plugin = _Plugin()
    event = _Event()
    prefetch = JointReplyPrefetch(plugin, event, "早安")
    try:
        candidates = await prefetch.result(plugin, event, "早安", wait_seconds=0.05)
    finally:
        await prefetch.close()

    assert len(candidates) == 1
    assert candidates[0]["path"] == "/tmp/local_bm25.png"
    assert prefetch.outcome == "local_fallback"


@pytest.mark.asyncio
async def test_joint_prefetch_local_fallback_timeout_is_capped():
    from core.tools.joint_prefetch import JointReplyPrefetch

    class _TurnState:
        def __init__(self):
            self.candidates = []

        def get_candidates(self):
            return list(self.candidates)

    class _Event:
        unified_msg_origin = "g:2"

        def __init__(self):
            self._extras = {}
            self._emoji_turn_state = {}

        def get_platform_id(self):
            return "qq"

        def get_self_id(self):
            return "bot1"

        def get_sender_id(self):
            return "u1"

    class _SlowPlugin:
        def __init__(self):
            self.plugin_config = SimpleNamespace(
                llm_joint_reply=True,
                auto_send_meme=True,
                get_event_target=lambda _e: ("group", "2"),
            )
            self.db_service = SimpleNamespace(get_library_version=lambda: 1)
            self.meme_selector = SimpleNamespace(
                is_path_allowed_for_event=lambda _p, _e: True
            )
            self._emoji_sender_engine = SimpleNamespace(
                emoji_turn_state=lambda ev: ev._emoji_turn_state.setdefault(
                    "state", _TurnState()
                )
            )

        def is_send_enabled_for_event(self, _ev):
            return True

        async def _search_meme_impl(self, ev, query, *, on_outcome=None):
            await asyncio.sleep(2.0)
            yield "never"

    plugin = _SlowPlugin()
    event = _Event()
    prefetch = JointReplyPrefetch(plugin, event, "早安")
    t0 = time.monotonic()
    try:
        # 0.08s join wait + capped 0.12s local fallback = ~0.20s (well below old 0.35+0.35s)
        candidates = await prefetch.result(plugin, event, "早安", wait_seconds=0.08)
    finally:
        await prefetch.close()
    elapsed = time.monotonic() - t0

    assert candidates == []
    assert prefetch.outcome == "timeout"
    assert elapsed < 0.35, f"Expected capped join+fallback wait <0.35s, took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_prepare_joint_reply_bounds_non_explicit_wait_by_prefetch_elapsed(
    monkeypatch,
):
    from core.tools import joint_prefetch, joint_reply

    observed_waits: list[float | None] = []
    now = [100.0]
    monkeypatch.setattr(joint_reply.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        joint_reply, "attach_temporary_candidates", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        joint_reply, "is_vibe_serious_for_event", lambda _p, _e: (False, "")
    )

    class _FakePrefetch:
        def __init__(self, started: float):
            self.started = started
            self.outcome = "candidates"
            self.cache_reason = "bypass"

        async def result(self, _plugin, _event, _query, *, wait_seconds=None):
            observed_waits.append(wait_seconds)
            return []

        async def close(self):
            return None

    plugin = SimpleNamespace(
        plugin_config=SimpleNamespace(llm_joint_reply=True, auto_send_meme=True),
        _emoji_sender_engine=SimpleNamespace(is_command_event=lambda _e: False),
        media_behavior=None,
    )

    cases = [
        ("哈哈哈哈", 99.50, 0.08),   # elapsed 0.50s > 0.35s -> floor 0.08s
        ("哈哈哈哈", 99.80, 0.15),   # elapsed 0.20s -> remaining 0.15s
        ("哈哈哈哈", 100.0, 0.35),   # elapsed 0.00s -> cap 0.35s
        ("发个表情包", 99.50, None),  # explicit media -> unbounded (None)
    ]
    for query, started, expected_wait in cases:
        fake_prefetch = _FakePrefetch(started=started)
        monkeypatch.setattr(
            joint_prefetch,
            "start_joint_prefetch",
            lambda _p, _e, fp=fake_prefetch: fp,
        )
        extras = {}
        ev = SimpleNamespace(
            _cd_joint_meme_supported_v1=True,
            _cd_original_user_text_v1=query,
            get_extra=lambda k, d=None, ex=extras: ex.get(k, d),
            set_extra=lambda k, v, ex=extras: ex.__setitem__(k, v),
        )
        req = SimpleNamespace(
            system_prompt="base",
            extra_user_content_parts=[],
            func_tool=None,
        )
        assert await joint_reply.prepare_joint_reply(plugin, ev, req) is True

        actual = observed_waits[-1]
        if expected_wait is None:
            assert actual is None
        else:
            assert actual == pytest.approx(expected_wait, abs=1e-6)


# ---------------------------------------------------------------------------
# P1-1: OpenAI compat parallel ref prep, dead assignment removed, refusal fail-fast & retry cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_compat_parallel_reference_prep_and_no_dead_assignment(monkeypatch):
    from tl.api import openai_compat
    from tl.api.openai_compat import OpenAICompatProvider
    from tl.api_types import ApiRequestConfig

    src = Path(openai_compat.__file__).read_text(encoding="utf-8")
    # Ensure duplicate dead image_payload assignment is gone.
    assert src.count('"image_url": {"url": payload_url}') == 1

    active = 0
    max_active = 0

    async def _slow_ref_data_uri(client, config, image_input, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.08)
        active -= 1
        return f"data:image/png;base64,encoded-{image_input}"

    monkeypatch.setattr(openai_compat, "reference_data_uri", _slow_ref_data_uri)

    provider = OpenAICompatProvider()
    cfg = ApiRequestConfig(
        model="gpt-image",
        prompt="draw cat",
        reference_images=["a.png", "b.png", "a.png", "c.png"],
        image_input_mode="force_base64",
    )
    t0 = time.monotonic()
    payload = await provider._prepare_payload(client=SimpleNamespace(), config=cfg)
    elapsed = time.monotonic() - t0

    assert max_active >= 2, "Reference images were not prepared concurrently"
    assert elapsed < 0.22, f"Expected parallel prep <0.22s, took {elapsed:.2f}s"
    content = payload["messages"][0]["content"]
    urls = [part["image_url"]["url"] for part in content[1:]]
    assert urls == [
        "data:image/png;base64,encoded-a.png",
        "data:image/png;base64,encoded-b.png",
        "data:image/png;base64,encoded-a.png",
        "data:image/png;base64,encoded-c.png",
    ]


@pytest.mark.asyncio
async def test_openai_compat_refusal_fails_fast_and_no_image_retry_capped(monkeypatch):
    from tl.api.openai_compat import OpenAICompatProvider
    from tl.api_types import APIError, ApiRequestConfig
    from tl.tl_api import GeminiAPIClient

    provider = OpenAICompatProvider()
    client = GeminiAPIClient(["k1"])
    client.proxy = None

    # Refusal / policy text -> non-retryable content_policy_refusal
    for refusal_text in (
        "I cannot generate this image because it violates our content policy. api_key=secret123",
        "抱歉，该请求涉及敏感内容，无法生成图片。",
    ):
        with pytest.raises(APIError) as exc_info:
            await provider.parse_response(
                client=client,
                response_data={"choices": [{"message": {"content": refusal_text}}]},
                session=None,
                request_config=ApiRequestConfig(model="m", prompt="p"),
            )
        assert exc_info.value.error_type == "content_policy_refusal"
        assert exc_info.value.retryable is False
        assert "secret123" not in exc_info.value.message

    # Non-refusal plain text -> still raises retryable no_image_retry
    with pytest.raises(APIError) as exc_info2:
        await provider.parse_response(
            client=client,
            response_data={"choices": [{"message": {"content": "Here is a description of a cat."}}]},
            session=None,
            request_config=ApiRequestConfig(model="m", prompt="p"),
        )
    assert exc_info2.value.error_type == "no_image_retry"

    # Cap no_image_retry in GeminiAPIClient._make_request to 1 retry with 1.5s backoff
    attempts = 0
    sleeps: list[float] = []

    async def _always_no_image(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise APIError("only text", 500, "no_image_retry")

    async def _fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(client, "_get_session", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "_perform_request", _always_no_image)
    monkeypatch.setattr("tl.tl_api.asyncio.sleep", _fake_sleep)

    with pytest.raises(APIError) as retry_exc:
        await client._make_request(
            url="https://example.com/v1/chat/completions",
            payload={},
            headers={},
            api_type="openai",
            model="m",
            max_retries=5,
        )
    assert retry_exc.value.error_type == "no_image_retry"
    assert attempts == 2, f"Expected 1 initial + 1 retry = 2 attempts, got {attempts}"
    assert sleeps == [1.5]

    # Refusal (retryable=False) fails fast on attempt 1 with 0 sleeps
    attempts = 0
    sleeps.clear()

    async def _refusal(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise APIError("refused", 400, "content_policy_refusal", retryable=False)

    monkeypatch.setattr(client, "_perform_request", _refusal)
    with pytest.raises(APIError) as ref_exc:
        await client._make_request(
            url="https://example.com/v1/chat/completions",
            payload={},
            headers={},
            api_type="openai",
            model="m",
            max_retries=5,
        )
    assert ref_exc.value.error_type == "content_policy_refusal"
    assert attempts == 1
    assert sleeps == []


# ---------------------------------------------------------------------------
# P1-2: Maintenance orphan walk offloaded + pending/ byte cap + tightened default disk caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintenance_cleanup_orphans_offloaded_and_pending_cap(tmp_path, monkeypatch):
    from core.maintenance.retention import (
        DEFAULT_MAX_DISK_BYTES,
        DEFAULT_MAX_PENDING_DISK_BYTES,
        pending_eviction_candidates,
    )
    from core.maintenance.service import MaintenanceService

    categories_dir = tmp_path / "categories"
    (categories_dir / "happy").mkdir(parents=True)
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir(parents=True)

    kept_cat = categories_dir / "happy" / "kept.png"
    kept_cat.write_bytes(b"a" * 100)
    orphan_cat = categories_dir / "happy" / "orphan.png"
    orphan_cat.write_bytes(b"b" * 100)
    old_ts = time.time() - 3600
    os.utime(orphan_cat, (old_ts, old_ts))

    p1 = pending_dir / "p1.png"
    p2 = pending_dir / "p2.png"
    p3 = pending_dir / "p3.png"
    p1.write_bytes(b"1" * 400)
    p2.write_bytes(b"2" * 400)
    p3.write_bytes(b"3" * 400)

    pending_rows = [
        {"id": 1, "path": str(p1), "created_at": 100},
        {"id": 2, "path": str(p2), "created_at": 200},
        {"id": 3, "path": str(p3), "created_at": 300},
    ]
    deleted_pending_batches: list[list[int]] = []

    class _FakeDB:
        def get_all_paths(self):
            return [str(kept_cat)]

        def get_pending_paginated(self, page=1, page_size=100000):
            return (list(pending_rows), len(pending_rows))

        def delete_pending_batch(self, ids):
            deleted_pending_batches.append(list(ids))

        def count_total(self):
            return 1

    plugin = SimpleNamespace(
        db_service=_FakeDB(),
        plugin_config=SimpleNamespace(
            categories_dir=categories_dir,
            pending_dir=pending_dir,
            max_pending_disk_bytes=500,
        ),
    )
    svc = MaintenanceService(plugin)
    # Bypass ctime grace check on freshly created Windows test file
    monkeypatch.setattr(svc, "_recent_publication", lambda path: False)

    to_thread_funcs: list[str] = []
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(func, *args, **kwargs):
        to_thread_funcs.append(getattr(func, "__name__", repr(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("core.maintenance.service.asyncio.to_thread", _spy_to_thread)

    await svc._cleanup_orphans()
    assert "_collect_orphan_files_sync" in to_thread_funcs
    assert kept_cat.exists()
    assert not orphan_cat.exists()

    # Pending disk cap eviction: 3 x 400 = 1200 bytes > 500 bytes -> evicts oldest 2 (ids 1 & 2)
    evicted_preview = pending_eviction_candidates(pending_rows, max_disk_bytes=500)
    assert [r["id"] for r in evicted_preview] == [1, 2]

    evicted_count = await svc._enforce_pending_disk_cap()
    assert evicted_count == 2
    assert deleted_pending_batches == [[1, 2]]
    assert not p1.exists()
    assert not p2.exists()
    assert p3.exists()

    # Verify tightened combined default disk caps
    assert DEFAULT_MAX_DISK_BYTES == 256 * 1024 * 1024
    assert DEFAULT_MAX_PENDING_DISK_BYTES == 128 * 1024 * 1024


def test_tightened_combined_default_disk_caps():
    from core.maintenance.retention import (
        DEFAULT_MAX_DISK_BYTES,
        DEFAULT_MAX_PENDING_DISK_BYTES,
    )
    from tl.plugin_config import PluginConfig as TlPluginConfig

    tl_cfg = TlPluginConfig()
    assert tl_cfg.webui_gallery_max_size_mb == 256
    assert tl_cfg.image_cache_max_size_mb == 256.0

    # Verify tl/tl_utils.py and tl/web_studio_service.py source defaults directly
    # (without requiring cv2 in bare test environments).
    root = Path(__file__).resolve().parents[1]
    tl_utils_src = (root / "tl" / "tl_utils.py").read_text(encoding="utf-8")
    studio_src = (root / "tl" / "web_studio_service.py").read_text(encoding="utf-8")
    assert "_DEFAULT_IMAGE_CACHE_MAX_SIZE_MB = 256.0" in tl_utils_src
    assert "_UPLOAD_QUOTA_BYTES = 100 * 1024 * 1024" in studio_src

    combined_mb = (
        DEFAULT_MAX_DISK_BYTES / (1024 * 1024)
        + DEFAULT_MAX_PENDING_DISK_BYTES / (1024 * 1024)
        + tl_cfg.webui_gallery_max_size_mb
        + 100
        + tl_cfg.image_cache_max_size_mb
    )
    assert combined_mb <= 1024, f"Combined default disk caps too large: {combined_mb} MB"
