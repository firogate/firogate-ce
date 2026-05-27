"""
FiroGate Event Bus — production-hardened with future distributed support.

Current mode: in-process asyncio (single uvicorn worker).

Multi-worker / distributed migration path:
  The _BackendBase abstract class defines the interface.
  To migrate to Redis/NATS/RabbitMQ, implement _RedisBackend or _NatsBackend
  and set EventBus = _EventBus(backend=_RedisBackend()).
  No changes needed in events.py or payment_monitor.py.

  WARNING: In-process asyncio bus does NOT work across multiple Gunicorn workers.
  If you scale to multiple workers, you MUST switch to a distributed backend.
  Single worker (default uvicorn setup) is safe and recommended for FiroGate.
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from loguru import logger


# ═══════════════════════════════════════════════════════════════════════════════
# Event type
# ═══════════════════════════════════════════════════════════════════════════════
class Event(dict):
    pass


def make_event(event_type: str, **data) -> Event:
    return Event(type=event_type, ts=int(time.time()), **data)


# ═══════════════════════════════════════════════════════════════════════════════
# Internal metrics — lightweight counters for production visibility
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class _Metrics:
    connections_total:   int   = 0   # all-time connects
    disconnections_total: int  = 0   # all-time disconnects
    events_published:    int   = 0   # all-time events published
    events_delivered:    int   = 0   # all-time successful deliveries
    events_dropped:      int   = 0   # queue-full drops
    queue_overflows:     int   = 0   # times QueueFull was raised
    reconnect_storms:    int   = 0   # times IP rate limit was hit
    gc_pruned_total:     int   = 0   # zombie entries pruned by GC
    errors_total:        int   = 0   # unexpected errors in bus
    started_at:          float = field(default_factory=time.time)

    def snapshot(self) -> dict:
        uptime = int(time.time() - self.started_at)
        return {
            "uptime_seconds":       uptime,
            "connections_total":    self.connections_total,
            "disconnections_total": self.disconnections_total,
            "events_published":     self.events_published,
            "events_delivered":     self.events_delivered,
            "events_dropped":       self.events_dropped,
            "queue_overflows":      self.queue_overflows,
            "reconnect_storms":     self.reconnect_storms,
            "gc_pruned_total":      self.gc_pruned_total,
            "errors_total":         self.errors_total,
            "delivery_rate_pct": (
                round(self.events_delivered / self.events_published * 100, 1)
                if self.events_published > 0 else 100.0
            ),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Backend abstraction — enables future distributed swap
# ═══════════════════════════════════════════════════════════════════════════════
class _BackendBase:
    """
    Interface for event bus backends.

    To implement a distributed backend (Redis pub/sub, NATS, RabbitMQ):
    1. Subclass _BackendBase
    2. Implement subscribe(), unsubscribe(), publish()
    3. Set EventBus = _EventBus(backend=YourBackend())

    Example Redis backend signature:
        class _RedisBackend(_BackendBase):
            async def subscribe(self, channel: str) -> "_Entry": ...
            async def unsubscribe(self, entry: "_Entry") -> None: ...
            async def publish(self, channel: str, event: Event) -> int: ...

    Note: When using multiple Gunicorn workers, you MUST use a distributed
    backend — in-process asyncio queues are NOT shared between OS processes.
    """
    async def subscribe(self, channel: str) -> "_Entry":
        raise NotImplementedError

    async def unsubscribe(self, entry: "_Entry") -> None:
        raise NotImplementedError

    async def publish(self, channel: str, event: Event) -> int:
        raise NotImplementedError

    def stats(self) -> dict:
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
# Subscriber entry
# ═══════════════════════════════════════════════════════════════════════════════
class _Entry:
    """
    Represents one SSE subscriber connection.
    Uses __slots__ for minimal memory footprint.
    """
    __slots__ = ("queue", "channel", "created_at", "last_active", "alive")

    def __init__(self, channel: str, queue_size: int = 64):
        self.queue       = asyncio.Queue(maxsize=queue_size)
        self.channel     = channel
        self.created_at  = time.monotonic()
        self.last_active = time.monotonic()
        self.alive       = True


# ═══════════════════════════════════════════════════════════════════════════════
# In-process asyncio backend (default — single worker)
# ═══════════════════════════════════════════════════════════════════════════════
class _LocalBackend(_BackendBase):
    """
    Pure asyncio in-process pub/sub.
    Works correctly with a single uvicorn worker.

    Limits:
    - MAX_PER_CHANNEL: prevents one payment from having 100 tabs open
    - MAX_TOTAL: prevents server resource exhaustion
    - IDLE_TIMEOUT: drops subscribers silent for > 5 min
    - QUEUE_SIZE: bounded queue prevents memory growth
    """
    MAX_PER_CHANNEL = 8
    MAX_TOTAL       = 1024
    QUEUE_SIZE      = 64
    IDLE_TIMEOUT    = 300   # seconds
    GC_INTERVAL     = 60    # seconds

    def __init__(self, metrics: _Metrics):
        self._channels: dict[str, list[_Entry]] = defaultdict(list)
        self._lock     = asyncio.Lock()
        self._total    = 0
        self._gc_task  = None
        self._m        = metrics

    def _ensure_gc(self) -> None:
        if self._gc_task is None or self._gc_task.done():
            self._gc_task = asyncio.create_task(self._gc_loop())
            def _on_gc_done(t):
                if t.cancelled():
                    return  # normal on shutdown
                exc = t.exception()
                if exc:
                    logger.warning(f"[bus] GC task error: {exc}")
            self._gc_task.add_done_callback(_on_gc_done)

    async def _gc_loop(self) -> None:
        while True:
            await asyncio.sleep(self.GC_INTERVAL)
            await self._gc_once()

    async def _gc_once(self) -> None:
        now = time.monotonic()
        pruned = 0
        async with self._lock:
            dead_channels = []
            for ch, entries in self._channels.items():
                before = len(entries)
                live   = [e for e in entries if e.alive and (now - e.last_active) < self.IDLE_TIMEOUT]
                removed = before - len(live)
                if removed:
                    pruned += removed
                    self._total = max(0, self._total - removed)
                    self._channels[ch] = live
                if not live:
                    dead_channels.append(ch)
            for ch in dead_channels:
                del self._channels[ch]
        if pruned:
            self._m.gc_pruned_total += pruned
            logger.info(f"[bus] GC pruned {pruned} zombie subscriber(s) | total={self._total}")

    async def subscribe(self, channel: str) -> _Entry:
        self._ensure_gc()
        async with self._lock:
            ch_count = len(self._channels.get(channel, []))
            if ch_count >= self.MAX_PER_CHANNEL:
                self._m.reconnect_storms += 1
                raise RuntimeError(f"Channel '{channel}' at max subscribers ({self.MAX_PER_CHANNEL})")
            if self._total >= self.MAX_TOTAL:
                self._m.reconnect_storms += 1
                raise RuntimeError(f"Server at subscriber capacity ({self.MAX_TOTAL})")
            entry = _Entry(channel, self.QUEUE_SIZE)
            self._channels[channel].append(entry)
            self._total += 1
        self._m.connections_total += 1
        logger.info(f"[bus] +sub channel={channel} total={self._total}")
        return entry

    async def unsubscribe(self, entry: _Entry) -> None:
        if not entry.alive:
            return
        entry.alive = False
        ch = entry.channel
        async with self._lock:
            try:
                self._channels[ch].remove(entry)
                self._total = max(0, self._total - 1)
            except ValueError:
                pass
            if ch in self._channels and not self._channels[ch]:
                del self._channels[ch]
        self._m.disconnections_total += 1
        age = round(time.monotonic() - entry.created_at, 1)
        logger.info(f"[bus] -sub channel={ch} age={age}s total={self._total}")

    async def publish(self, channel: str, event: Event) -> int:
        async with self._lock:
            entries = list(self._channels.get(channel, []))

        delivered = 0
        dead      = []
        now       = time.monotonic()

        for entry in entries:
            if not entry.alive:
                dead.append(entry)
                continue
            if (now - entry.last_active) > self.IDLE_TIMEOUT:
                entry.alive = False
                dead.append(entry)
                continue
            try:
                entry.queue.put_nowait(event)
                entry.last_active = now
                delivered += 1
            except asyncio.QueueFull:
                self._m.queue_overflows += 1
                self._m.events_dropped  += 1
                logger.warning(f"[bus] queue full on {channel} — client too slow")

        if dead:
            async with self._lock:
                for entry in dead:
                    try:
                        self._channels[channel].remove(entry)
                        self._total = max(0, self._total - 1)
                    except (ValueError, KeyError):
                        pass

        self._m.events_published += 1
        self._m.events_delivered += delivered
        if delivered:
            logger.debug(f"[bus] {event.get('type')} → {channel} ({delivered}/{len(entries)} delivered)")
        return delivered

    def stats(self) -> dict:
        return {
            "total_subscribers": self._total,
            "channels": {ch: len(subs) for ch, subs in self._channels.items()},
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Public EventBus façade
# ═══════════════════════════════════════════════════════════════════════════════
class _EventBus:
    """
    Public interface. Delegates to pluggable backend.

    Scale path:
      Single worker (now):  _LocalBackend   — zero config
      Multiple workers:     _RedisBackend   — set REDIS_URL in .env
      Microservices:        _NatsBackend    — set NATS_URL in .env
    """
    def __init__(self, backend: _BackendBase | None = None):
        self._metrics = _Metrics()
        self._backend = backend or _LocalBackend(self._metrics)

    # ─ Core operations ──
    async def subscribe(self, channel: str) -> _Entry:
        return await self._backend.subscribe(channel)

    async def unsubscribe(self, entry: _Entry) -> None:
        await self._backend.unsubscribe(entry)

    async def publish(self, channel: str, event: Event) -> int:
        return await self._backend.publish(channel, event)

    # ─ Shortcuts ─
    async def publish_payment(self, payment_id: str, event: Event) -> None:
        await self.publish(f"payment:{payment_id}", event)

    async def publish_merchant(self, merchant_id: str, event: Event) -> None:
        await self.publish(f"merchant:{str(merchant_id)}", event)

    # ─ Observability ─
    def metrics(self) -> dict:
        return {**self._metrics.snapshot(), **self._backend.stats()}

    @property
    def _lock(self):
        """Compatibility shim for events.py direct access."""
        return self._backend._lock if hasattr(self._backend, "_lock") else asyncio.Lock()

    @property
    def _channels(self):
        """Compatibility shim."""
        return self._backend._channels if hasattr(self._backend, "_channels") else {}

    @property
    def _total_subs(self):
        return self._backend._total if hasattr(self._backend, "_total") else 0

    @property
    def MAX_SUBS(self):
        return getattr(self._backend, "MAX_TOTAL", 1024)


EventBus = _EventBus()


# ═══════════════════════════════════════════════════════════════════════════════
# Migration guide (read before scaling)
# ═══════════════════════════════════════════════════════════════════════════════
"""
HOW TO MIGRATE TO REDIS PUB/SUB (when you need multiple workers):

1. Install:  pip install redis[asyncio]
2. Add to .env:  REDIS_URL=redis://localhost:6379/0
3. Implement:

    import redis.asyncio as aioredis
    from app.core.config import get_settings

    class _RedisBackend(_BackendBase):
        def __init__(self):
            self._redis = aioredis.from_url(get_settings().REDIS_URL)
            self._local = {}   # local queues per subscriber

        async def subscribe(self, channel: str) -> _Entry:
            entry = _Entry(channel)
            pubsub = self._redis.pubsub()
            await pubsub.subscribe(channel)
            self._local[id(entry)] = pubsub
            asyncio.create_task(self._pump(pubsub, entry))
            return entry

        async def publish(self, channel: str, event: Event) -> int:
            payload = json.dumps(event)
            return await self._redis.publish(channel, payload)

        async def _pump(self, pubsub, entry):
            async for msg in pubsub.listen():
                if msg["type"] == "message":
                    data = json.loads(msg["data"])
                    await entry.queue.put(Event(data))

4. Replace:
    EventBus = _EventBus(backend=_RedisBackend())
"""
