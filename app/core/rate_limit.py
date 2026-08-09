
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Optional, Tuple

from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException, Request
from loguru import logger


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if not parts.password and not parts.username:
            return url
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, "***@" + netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return "***"


def get_client_ip(request: Request) -> str:
    """
    Get client IP for rate limiting.
    Privacy-aware: uses hashed identifier for Tor/privacy-mode users.
    """
    from app.services.privacy_service import get_session_privacy_state, is_onion_request

    state = get_session_privacy_state(request)
    is_onion = state.get("is_onion") or is_onion_request(request)
    privacy_mode = state.get("privacy_mode", False)

    if is_onion or privacy_mode:
        import hashlib
        # Hash of user-agent + salt: rate-limits privacy users without storing their IP.
        ua = request.headers.get("user-agent", "unknown")
        identifier = hashlib.sha256(f"privacy:{ua}".encode()).hexdigest()[:24]
        return f"priv_{identifier}"

    try:
        from app.core.config import get_settings
        trust = get_settings().TRUST_PROXY_HEADERS
    except Exception:
        trust = False

    if trust:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        real = request.headers.get("X-Real-IP", "")
        if real:
            return real.strip()

    return request.client.host if request.client else "unknown"


class RedisRateLimiter:

    def __init__(
        self,
        url: str,
        password: Optional[str] = None,
        ssl: bool = False,
        max_connections: int = 20,
        key_prefix: str = "firogate:rl:",
        key_ttl_multiplier: int = 3,
    ):
        self._url        = url
        self._pw         = password
        self._ssl        = ssl
        self._pool_sz    = max_connections
        self._prefix     = key_prefix
        self._ttl_mult   = key_ttl_multiplier
        self._conn_pool  = None
        self._conn_lock  = asyncio.Lock()
        self._ok         = True


    async def _get_conn_pool(self):
        if self._conn_pool is not None:
            return self._conn_pool

        async with self._conn_lock:
            if self._conn_pool is not None:
                return self._conn_pool
            try:
                import redis.asyncio as aioredis


                pool_kwargs: dict = {
                    "max_connections":       self._pool_sz,
                    "decode_responses":      True,
                    "socket_connect_timeout": 2,
                    "socket_timeout":         2,
                    "retry_on_timeout":       False,
                }
                if self._pw:
                    pool_kwargs["password"] = self._pw
                if self._ssl:
                    pool_kwargs["ssl"] = True
                    pool_kwargs["ssl_certfile"] = None
                pool = aioredis.ConnectionPool.from_url(
                    self._url,
                    **pool_kwargs,
                )
                self._conn_pool = pool
                logger.success(f"[rate_limit] Redis pool ready → {_redact_url(self._url)}")
            except ImportError:
                logger.error(
                    "[rate_limit] redis[asyncio] not installed. "
                    "Run: pip install 'redis[asyncio]'"
                )
                self._ok = False
                self._conn_pool = None
            except Exception as exc:
                logger.error(f"[rate_limit] Redis pool creation failed: {type(exc).__name__}")
                self._ok = False
                self._conn_pool = None

        return self._conn_pool

    async def _get_client(self):
        pool = await self._get_conn_pool()
        if pool is None:
            return None
        try:
            import redis.asyncio as aioredis
            return aioredis.Redis(connection_pool=pool)
        except Exception:
            return None


    async def check(self, key: str, max_requests: int, window: int) -> Tuple[bool, int, int]:
        if not self._ok:
            return True, max_requests, int(time.time()) + window

        r = await self._get_client()
        if r is None:
            return True, max_requests, int(time.time()) + window

        rkey = f"{self._prefix}{key}"
        try:
            async with r:
                pipe = r.pipeline()
                pipe.incr(rkey)
                pipe.ttl(rkey)
                count, ttl = await pipe.execute()


            if count == 1 or ttl < 0:
                r2 = await self._get_client()
                if r2:
                    async with r2:
                        await r2.expire(rkey, window * self._ttl_mult)
                ttl = window

            reset_at  = int(time.time()) + max(ttl, 0)
            remaining = max(0, max_requests - count)
            allowed   = count <= max_requests

            if not allowed:
                logger.warning(
                    f"[rate_limit] EXCEEDED key={key!r} count={count}/{max_requests}"
                )
            return allowed, remaining, reset_at

        except Exception as exc:
            logger.error(f"[rate_limit] Redis error (fail-closed): {exc}")
            self._ok = False
            asyncio.create_task(self._reconnect_probe())
            return False, 0, int(time.time()) + window

    async def _reconnect_probe(self):
        await asyncio.sleep(30)
        r = await self._get_client()
        if r:
            try:
                async with r:
                    await r.ping()
                self._ok = True
                logger.success("[rate_limit] Redis reconnected ✅")
            except Exception:
                pass

    async def is_blocked(self, ip: str) -> Tuple[bool, int]:
        if not self._ok:
            return False, 0
        r = await self._get_client()
        if r is None:
            return False, 0
        bkey = f"{self._prefix}block:{ip}"
        try:
            async with r:
                pipe = r.pipeline()
                pipe.exists(bkey)
                pipe.ttl(bkey)
                exists, ttl = await pipe.execute()
            if exists:
                return True, max(0, int(ttl))
            return False, 0
        except Exception:
            return False, 0

    async def block(self, ip: str, duration: int, reason: str = "") -> None:
        if not self._ok:
            return
        r = await self._get_client()
        if r is None:
            return
        bkey = f"{self._prefix}block:{ip}"
        try:
            async with r:
                await r.setex(bkey, duration, reason or "blocked")
            logger.warning(
                f"[rate_limit] IP blocked: {ip} for {duration}s reason={reason!r}"
            )
        except Exception as exc:
            logger.warning(f"[rate_limit] Redis block failed: {exc}")

    async def unblock(self, ip: str) -> bool:
        if not self._ok:
            return False
        r = await self._get_client()
        if r is None:
            return False
        bkey = f"{self._prefix}block:{ip}"
        try:
            async with r:
                removed = await r.delete(bkey)
            return bool(removed)
        except Exception:
            return False

    async def ping(self) -> bool:
        r = await self._get_client()
        if r is None:
            return False
        try:
            async with r:
                return bool(await r.ping())
        except Exception:
            return False

    async def get_stats(self, key: str) -> dict:
        r = await self._get_client()
        if r is None:
            return {"backend": "unavailable"}
        rkey = f"{self._prefix}{key}"
        try:
            async with r:
                pipe = r.pipeline()
                pipe.get(rkey)
                pipe.ttl(rkey)
                count, ttl = await pipe.execute()
            return {
                "backend": "redis",
                "key":     rkey,
                "count":   int(count or 0),
                "ttl":     ttl,
            }
        except Exception as exc:
            return {"backend": "error", "error": str(exc)}


class InMemoryRateLimiter:

    def __init__(self):
        self._windows: dict[str, list[float]]       = defaultdict(list)
        self._blocks:  dict[str, tuple[float, str]] = {}
        self._last_cleanup = time.monotonic()
        self._lock = asyncio.Lock()

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup < 300:
            return
        cutoff = now - 3600
        for k in list(self._windows):
            self._windows[k] = [t for t in self._windows[k] if t > cutoff]
            if not self._windows[k]:
                del self._windows[k]
        for k in list(self._blocks):
            if self._blocks[k][0] <= now:
                del self._blocks[k]
        self._last_cleanup = now

    async def check(self, key: str, max_requests: int, window: int) -> Tuple[bool, int, int]:
        now    = time.monotonic()
        wall   = int(time.time())
        cutoff = now - window
        async with self._lock:
            self._cleanup(now)
            hits = self._windows[key]
            hits[:] = [t for t in hits if t > cutoff]
            if len(hits) >= max_requests:
                oldest   = min(hits) if hits else now
                reset_at = wall + int(oldest - now + window)
                return False, 0, reset_at
            hits.append(now)
            return True, max_requests - len(hits), wall + window

    async def is_blocked(self, ip: str) -> Tuple[bool, int]:
        now = time.monotonic()
        if ip in self._blocks:
            until, _ = self._blocks[ip]
            if now < until:
                return True, int(until - now)
            del self._blocks[ip]
        return False, 0

    async def block(self, ip: str, duration: int, reason: str = "") -> None:
        self._blocks[ip] = (time.monotonic() + duration, reason)
        logger.warning(f"[rate_limit] In-memory block: {ip} for {duration}s reason={reason!r}")

    async def unblock(self, ip: str) -> bool:
        if ip in self._blocks:
            del self._blocks[ip]
            return True
        return False

    async def ping(self) -> bool:
        return True

    async def get_stats(self, key: str) -> dict:
        now  = time.monotonic()
        hits = [t for t in self._windows.get(key, []) if now - t < 60]
        return {"backend": "in-memory", "key": key, "count": len(hits)}


_limiter: Optional[RedisRateLimiter | InMemoryRateLimiter] = None


def _build_limiter() -> RedisRateLimiter | InMemoryRateLimiter:
    try:
        from app.core.config import get_settings
        s         = get_settings()
        redis_url = (getattr(s, "REDIS_URL", "") or "").strip()
    except Exception:
        redis_url = ""

    if redis_url:
        s = get_settings()
        return RedisRateLimiter(
            url=redis_url,
            password=(getattr(s, "REDIS_PASSWORD", "") or "") or None,
            ssl=bool(getattr(s, "REDIS_SSL", False)),
            max_connections=int(getattr(s, "REDIS_MAX_CONNECTIONS", 20)),
            key_prefix=str(getattr(s, "REDIS_KEY_PREFIX", "firogate:rl:")),
            key_ttl_multiplier=int(getattr(s, "REDIS_KEY_TTL_MULTIPLIER", 3)),
        )
    return InMemoryRateLimiter()


def get_rate_limiter() -> RedisRateLimiter | InMemoryRateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = _build_limiter()
    return _limiter


async def rate_limit_check(
    request: Request,
    max_requests: int,
    window_seconds: int,
    key_prefix: str = "ip",
    block_on_exceed: bool = False,
    block_duration_seconds: int = 300,
) -> None:
    limiter   = get_rate_limiter()
    client_ip = get_client_ip(request)
    path      = request.url.path

    # On TESTNET we keep rate limiting active (so the code path is exercised and
    # abuse is still slowed) but we DO NOT hard-block IPs and we raise the
    # ceilings generously. Developers hammer the API/login repeatedly while
    # building, and a 5-minute IP block would lock them out. This relaxation
    # applies to testnet only; mainnet keeps the strict limits + blocking.
    try:
        from app.core.config import get_settings
        _testnet = get_settings().is_testnet
    except Exception:
        _testnet = False
    if _testnet:
        block_on_exceed = False
        max_requests = max_requests * 20


    blocked, secs = await limiter.is_blocked(client_ip)
    if blocked:
        raise HTTPException(
            status_code=429,
            detail={"error": "IP temporarily blocked", "retry_after": secs},
            headers={
                "Retry-After":           str(secs),
                "X-RateLimit-Limit":     str(max_requests),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset":     str(int(time.time()) + secs),
            },
        )


    key = f"{key_prefix}:{client_ip}:{path}"
    allowed, remaining, reset_at = await limiter.check(key, max_requests, window_seconds)
    retry_after = max(0, reset_at - int(time.time()))

    if not allowed:
        if block_on_exceed:
            await limiter.block(client_ip, block_duration_seconds,
                                reason=f"rate-exceeded:{path}")
        raise HTTPException(
            status_code=429,
            detail={
                "error":          "Rate limit exceeded",
                "max_requests":   max_requests,
                "window_seconds": window_seconds,
                "retry_after":    retry_after,
            },
            headers={
                "Retry-After":           str(retry_after),
                "X-RateLimit-Limit":     str(max_requests),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset":     str(reset_at),
                "X-RateLimit-Window":    str(window_seconds),
            },
        )


async def rate_limit_auth(request: Request) -> None:
    # Raised from 20 -> 60 req/min per IP: deployments sitting behind
    # Cloudflare/a proxy were tripping the old ceiling for legitimate users
    # on shared/NAT'd IPs. Brute-force protection on /login itself is the
    # account-based LoginAttempt lockout (10 failed attempts / 15 min per-
    # username, see app/api/auth.py), not this IP ceiling - this stays only
    # to blunt basic scripted abuse, matching BTCPayServer's dual-layer
    # approach (IP rate limit + account lockout, neither replaced by a
    # front-end WAF/CDN).
    await rate_limit_check(request, 60, 60, "auth")

async def rate_limit_strict(request: Request) -> None:
    await rate_limit_check(request, 60, 60, "strict")

async def rate_limit_moderate(request: Request) -> None:
    await rate_limit_check(request, 120, 60, "moderate")

async def rate_limit_relaxed(request: Request) -> None:
    await rate_limit_check(request, 300, 60, "relaxed")


def log_rate_limiter_info() -> None:
    limiter = get_rate_limiter()
    if isinstance(limiter, RedisRateLimiter):
        logger.success(
            f"[rate_limit] ✅ Redis backend url={limiter._url} "
            f"prefix={limiter._prefix!r} pool_size={limiter._pool_sz}"
        )
    else:
        logger.info(
            "[rate_limit] In-memory backend active "
            "(set REDIS_URL in .env for production use)"
        )
