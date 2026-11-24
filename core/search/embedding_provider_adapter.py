"""Keep SDK vector storage while pacing requests and reporting observed dimensions."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar

import numpy as np

from ..util.model_requests import is_rate_limited, model_request_gate

_INTERACTIVE_EMBEDDING_LANE: ContextVar[bool] = ContextVar(
    "visual_media_interactive_embedding_lane", default=False
)


@contextmanager
def interactive_embedding_lane(enabled: bool = True):
    """Route embedding provider calls inside this scope through the interactive lane."""
    token = _INTERACTIVE_EMBEDDING_LANE.set(bool(enabled))
    try:
        yield
    finally:
        _INTERACTIVE_EMBEDDING_LANE.reset(token)


class EmbeddingProviderAdapter:
    def __init__(self, service, provider, *, interactive: bool = False):
        self.service = service
        self.provider = provider
        self.interactive = bool(interactive)

    def _is_interactive(self) -> bool:
        return self.interactive or _INTERACTIVE_EMBEDDING_LANE.get()

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def get_dim(self):
        return self.service._effective_dim(self.provider)

    async def _request(self, operation):
        key = self.service._extract_provider_id(self.provider)
        gate = model_request_gate(self.service.plugin)
        if self._is_interactive():
            for attempt in range(2):
                try:
                    return await gate.call_interactive(key, operation)
                except Exception as exc:
                    if not is_rate_limited(exc) or attempt == 1:
                        raise
                    await asyncio.sleep(0.25)
        for attempt in range(3):
            try:
                return await gate.call(key, operation)
            except Exception as exc:
                if not is_rate_limited(exc) or attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)

    def _validate(self, vectors):
        array = np.asarray(vectors, dtype=np.float32)
        if array.ndim != 2 or not array.shape[0] or not array.shape[1] or not np.isfinite(array).all():
            raise ValueError("嵌入模型返回了无效向量")
        self.service._record_dimension(self.provider, array.shape[1])
        return array.tolist()

    async def get_embedding(self, text):
        interactive = self._is_interactive()
        if interactive:
            getter = getattr(self.service, "get_cached_query_embedding", None)
            if callable(getter):
                cached = getter(self.provider, text)
                if cached is not None:
                    return cached
        identity = self.service._provider_identity(self.provider)
        vector = await self._request(lambda: self.provider.get_embedding(text))
        if identity != self.service._provider_identity(self.provider):
            raise ValueError("嵌入模型在请求期间发生变化")
        validated = self._validate([vector])[0]
        if interactive:
            setter = getattr(self.service, "set_cached_query_embedding", None)
            if callable(setter):
                setter(self.provider, text, validated)
        return validated

    async def get_embeddings(self, texts):
        if callable(getattr(self.provider, "get_embeddings", None)):
            identity = self.service._provider_identity(self.provider)
            vectors = await self._request(lambda: self.provider.get_embeddings(texts))
            if identity != self.service._provider_identity(self.provider):
                raise ValueError("嵌入模型在请求期间发生变化")
            if len(vectors) != len(texts):
                raise ValueError("嵌入模型返回的向量数量与请求不一致")
            return self._validate(vectors)
        return [await self.get_embedding(text) for text in texts]

    async def get_embeddings_batch(self, texts, batch_size=16, tasks_limit=1, max_retries=1, progress_callback=None):
        # Reuse the framework's batch implementation with one task and one outer
        # attempt: shared pacing/retries already happen inside get_embeddings.
        from astrbot.core.provider.provider import EmbeddingProvider
        batch = getattr(EmbeddingProvider, "get_embeddings_batch", None)
        if callable(batch):
            return await batch(self, texts, batch_size=batch_size, tasks_limit=1,
                               max_retries=1, progress_callback=progress_callback)
        vectors = []
        for start in range(0, len(texts), batch_size):
            vectors.extend(await self.get_embeddings(texts[start:start + batch_size]))
        return vectors
