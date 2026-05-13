"""Bounded concurrency for the chart-generate route.

The pipeline holds the torch lock during Demucs (and to a lesser extent
Beat This! and MERT). Two simultaneous chart-generate requests would
serialize behind that lock anyway, but with no upper bound on the queue
depth a small number of requests can spike resident memory (each holding
~50MB of audio buffers + ~1GB of resident model weights they share) and
push the box into swap.

This module gives a single asyncio.Semaphore with a configurable slot
count + an optional acquire timeout. Route handlers acquire it before
calling build_chart_from_audio; on timeout they return 429 so the client
sees a clear "server busy" error instead of a 60s+ hang.

Sizing guidance (`MAX_CONCURRENT_CHARTS` env var):
  - CPU-only Demucs: 1 (Demucs already saturates a 4-8 core box)
  - GPU Demucs: 1 (one GPU = one job)
  - Baseline / ML-light only: 2-4 is fine
Default 1 is conservative; raise via env if you've benchmarked headroom.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger("beatbridge.util.concurrency")


def _max_concurrent() -> int:
    raw = os.environ.get("MAX_CONCURRENT_CHARTS", "1")
    try:
        n = int(raw)
        return max(1, n)
    except ValueError:
        logger.warning("Invalid MAX_CONCURRENT_CHARTS=%r; defaulting to 1", raw)
        return 1


def _acquire_timeout_s() -> float:
    """Seconds a request will wait in the queue before giving up with 429.

    A timeout of 0 means "fail fast if all slots are taken." Production
    GPU deploys might want 30-60s; CPU-only Demucs deploys might want
    fail-fast to spare the client a long hang.
    """
    raw = os.environ.get("CHART_QUEUE_TIMEOUT_S", "0")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


# Single process-wide semaphore. The chart-generate route awaits it; on
# success it builds the chart inside the `async with` block; on timeout
# the route raises HTTPException(429).
_CHART_SEMAPHORE: asyncio.Semaphore | None = None
_TIMEOUT_S: float = 0.0
_INIT_LOCK = asyncio.Lock()


async def _ensure_semaphore() -> tuple[asyncio.Semaphore, float]:
    """Lazy-init so we read MAX_CONCURRENT_CHARTS at first request, not at
    module import (matters for tests that monkeypatch env)."""
    global _CHART_SEMAPHORE, _TIMEOUT_S
    if _CHART_SEMAPHORE is not None:
        return _CHART_SEMAPHORE, _TIMEOUT_S
    async with _INIT_LOCK:
        if _CHART_SEMAPHORE is None:
            slots = _max_concurrent()
            _CHART_SEMAPHORE = asyncio.Semaphore(slots)
            _TIMEOUT_S = _acquire_timeout_s()
            logger.info(
                "chart-generate concurrency=%d queue_timeout=%.1fs",
                slots, _TIMEOUT_S,
            )
    assert _CHART_SEMAPHORE is not None  # for type-checker
    return _CHART_SEMAPHORE, _TIMEOUT_S


class ChartQueueFull(Exception):
    """All slots taken and the wait timeout elapsed. Caller should 429."""


class chart_slot:  # noqa: N801  (deliberately lowercase for the async-with idiom)
    """Async context manager for the chart-generate semaphore.

    Usage::

        async with chart_slot():
            chart = build_chart_from_audio(...)

    Raises ChartQueueFull if the slot can't be acquired within the
    configured timeout. The route handler catches this and 429s.
    """

    def __init__(self) -> None:
        self._sem: asyncio.Semaphore | None = None
        self._acquired: bool = False

    async def __aenter__(self) -> "chart_slot":
        sem, timeout = await _ensure_semaphore()
        self._sem = sem
        if timeout <= 0:
            # Fail-fast mode: try once, raise if no slot is available.
            if not sem.locked() and sem._value > 0:  # type: ignore[attr-defined]
                await sem.acquire()
                self._acquired = True
                return self
            # Probe with acquire-then-release pattern can deadlock under load;
            # just attempt acquire with a 0.0 timeout via wait_for.
            try:
                await asyncio.wait_for(sem.acquire(), timeout=0.001)
                self._acquired = True
            except asyncio.TimeoutError:
                raise ChartQueueFull(
                    "All chart-generate slots are busy. Try again shortly.",
                )
            return self
        try:
            await asyncio.wait_for(sem.acquire(), timeout=timeout)
            self._acquired = True
        except asyncio.TimeoutError:
            raise ChartQueueFull(
                f"Chart-generate queue full after {timeout:.1f}s wait. "
                "Try again shortly.",
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._acquired and self._sem is not None:
            self._sem.release()
            self._acquired = False
