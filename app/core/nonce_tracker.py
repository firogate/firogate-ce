"""
app/core/nonce_tracker.py

Server-side nonce deduplication to prevent replay attacks.
Uses Redis when available, falls back to in-memory dict with TTL cleanup.

Usage:
    from app.core.nonce_tracker import is_nonce_used, mark_nonce_used

    if await is_nonce_used(nonce):
        raise HTTPException(409, "Replay detected")
    await mark_nonce_used(nonce)
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from loguru import logger

NONCE_TTL = 300  # 5 minutes


# ─ In-memory fallback ─

class _InMemoryNonceStore:
    """Thread-safe in-memory nonce store with automatic TTL cleanup."""

    def __init__(self, ttl: int = NONCE_TTL, max_size: int = 50_000):
        self._store: OrderedDict[str, float] = OrderedDict()
        self._ttl = ttl
        self._max = max_size
        self._lock = asyncio.Lock()

    async def exists(self, nonce: str) -> bool:
        async with self._lock:
            self._evict()
            return nonce in self._store

    async def add(self, nonce: str) -> None:
        async with self._lock:
            self._evict()
            self._store[nonce] = time.time()
            # Hard cap: drop oldest if over max
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def _evict(self):
        cutoff = time.time() - self._ttl
        while self._store:
            oldest_key, oldest_ts = next(iter(self._store.items()))
            if oldest_ts < cutoff:
                self._store.popitem(last=False)
            else:
                break


_mem_store = _InMemoryNonceStore()


# ─ Redis-backed store ─

async def _get_redis():
    """Try to get Redis client from rate limiter pool."""
    try:
        from app.core.rate_limit import get_rate_limiter
        limiter = get_rate_limiter()
        if hasattr(limiter, '_get_client'):
            client = await limiter._get_client()
            return client
    except Exception:
        pass
    return None


_REDIS_PREFIX = "firogate:nonce:"


async def is_nonce_used(nonce: str) -> bool:
    """Check if nonce was already seen. Returns True if REPLAY detected."""
    if not nonce:
        return False  # No nonce = not applicable

    # Try Redis first
    try:
        r = await _get_redis()
        if r:
            val = await r.get(f"{_REDIS_PREFIX}{nonce}")
            return val is not None
    except Exception:
        pass

    # Fall back to in-memory
    return await _mem_store.exists(nonce)


async def mark_nonce_used(nonce: str, ttl: int = NONCE_TTL) -> None:
    """Record nonce as used. Expires after TTL."""
    if not nonce:
        return

    # Try Redis first
    try:
        r = await _get_redis()
        if r:
            await r.setex(f"{_REDIS_PREFIX}{nonce}", ttl, "1")
            return
    except Exception:
        pass

    # Fall back to in-memory
    await _mem_store.add(nonce)
