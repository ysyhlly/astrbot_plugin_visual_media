"""Per-provider request pacing shared by collection, review and vector search."""

import asyncio
import time
from email.utils import parsedate_to_datetime


def is_rate_limited(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    status = status or getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or any(word in str(exc).lower() for word in
                                ("429", "ratelimit", "rate limit", "request limit", "too many requests"))


def retry_after(exc: Exception, default: float = 30) -> float:
    headers = getattr(getattr(exc, "response", None), "headers", None) or getattr(exc, "headers", None) or {}
    value = headers.get("Retry-After", headers.get("retry-after", ""))
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return default


class ModelRequestGate:
    DEFAULT_INTERACTIVE_CONCURRENCY = 4

    def __init__(self, plugin):
        self.plugin = plugin
        self._locks = {}
        self._next = {}
        self._interactive_semaphores: dict[str, asyncio.Semaphore] = {}

    def _interactive_semaphore(self, provider_id: str, max_concurrency: int) -> asyncio.Semaphore:
        sem = self._interactive_semaphores.get(provider_id)
        if sem is None:
            sem = asyncio.Semaphore(max(1, int(max_concurrency)))
            self._interactive_semaphores[provider_id] = sem
        return sem

    async def call_interactive(
        self,
        provider_id: str,
        operation,
        *,
        max_concurrency: int = DEFAULT_INTERACTIVE_CONCURRENCY,
        timeout: float | None = None,
    ):
        """Zero-interval concurrent lane for interactive queries (decoupled from 5s background gate)."""
        sem = self._interactive_semaphore(provider_id, max_concurrency)
        async with sem:
            if timeout is not None and timeout > 0:
                return await asyncio.wait_for(operation(), timeout=timeout)
            return await operation()

    async def call(
        self,
        provider_id: str,
        operation,
        *,
        interactive: bool = False,
        max_concurrency: int = DEFAULT_INTERACTIVE_CONCURRENCY,
        timeout: float | None = None,
    ):
        if interactive:
            return await self.call_interactive(
                provider_id,
                operation,
                max_concurrency=max_concurrency,
                timeout=timeout,
            )
        lock = self._locks.setdefault(provider_id, asyncio.Lock())
        async with lock:
            delay = self._next.get(provider_id, 0) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            cfg = getattr(self.plugin, "plugin_config", None)
            interval = max(0, float(getattr(cfg, "model_request_interval", 5.0)))
            self._next[provider_id] = time.monotonic() + interval
        try:
            if timeout is not None and timeout > 0:
                return await asyncio.wait_for(operation(), timeout=timeout)
            return await operation()
        except Exception as exc:
            if is_rate_limited(exc):
                self._next[provider_id] = max(
                    self._next.get(provider_id, 0),
                    time.monotonic() + retry_after(exc),
                )
            raise


def model_request_gate(plugin) -> ModelRequestGate:
    gate = getattr(plugin, "_model_request_gate", None)
    if gate is None:
        gate = ModelRequestGate(plugin)
        plugin._model_request_gate = gate
    return gate
