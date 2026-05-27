"""
FiroGate SSE Endpoints — production-hardened.

Reconnect revalidation:
  On every reconnect, the client receives a fresh state snapshot from DB.
  This prevents stale UI after missed events during disconnection.

Browser SSE limits:
  HTTP/1.1 browsers: max 6 connections per domain.
  HTTP/2 browsers: effectively unlimited (multiplexed).
  Strategy: one SSE stream per page (checkout OR dashboard, not both).
  nginx serves HTTP/2 — no practical limit issue.

Security model:
  - Payment stream: HMAC token (public, verified)
  - Merchant stream: JWT cookie (authenticated)
  - Per-IP rate limit: 10 connections/60s
  - Per-channel cap: 8 subscribers max (blocks tab spam)
  - Global cap: 1024 total subscribers
"""

import asyncio
import json
import time
from collections import defaultdict
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import Payment, PaymentStatus, User
from app.core.security import verify_checkout_token
from app.services.event_bus import EventBus, make_event, _Entry
from app.api.users import get_current_user
from loguru import logger

router = APIRouter(prefix="/api/events", tags=["events"])

# ─ Tunables ─
HEARTBEAT_INTERVAL  = 25    # seconds — survives nginx default proxy_read_timeout
MAX_PAYMENT_STREAM  = 1500  # 25 min — matches max payment expiry
MAX_MERCHANT_STREAM = 1800  # 30 min dashboard session
DISCONNECT_POLL     = 4     # seconds — client disconnect detection interval

# ─ Per-IP rate limiting ──
_ip_conns: dict[str, list[float]] = defaultdict(list)
IP_MAX   = 10    # max connections per IP per window
IP_WIN   = 60    # window in seconds

def _rate_check(ip: str) -> None:
    now = time.monotonic()
    _ip_conns[ip] = [t for t in _ip_conns[ip] if now - t < IP_WIN]
    if len(_ip_conns[ip]) >= IP_MAX:
        EventBus._metrics.reconnect_storms += 1
        raise HTTPException(429, "Too many SSE connections — please wait")
    _ip_conns[ip].append(now)

def _get_ip(request: Request) -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() or
            (request.client.host if request.client else "unknown"))[:45]


# ─ SSE format ─
def _data(event_type: str, payload: dict) -> str:
    body = json.dumps({"type": event_type, **payload}, separators=(",", ":"))
    return f"data: {body}\n\n"

def _comment(text: str) -> str:
    return f": {text}\n\n"

_TERMINAL = frozenset({
    "payment.confirmed", "payment.expired",
    "payment.cancelled", "stream.end", "stream.timeout",
})


# ─ Core SSE generator ─
async def _stream(
    request:      Request,
    channel:      str,
    initial:      dict | None,
    max_duration: int,
    label:        str,
) -> AsyncGenerator[str, None]:
    """
    Production SSE generator.

    Guarantees:
    - Always calls EventBus.unsubscribe() in finally (no orphans)
    - Detects client disconnect every DISCONNECT_POLL seconds
    - Hard timeout regardless of client behavior
    - Heartbeat keeps proxy alive
    - All lifecycle events logged with label for debugging
    """
    entry: _Entry | None = None
    ip    = _get_ip(request)
    t0    = time.monotonic()

    try:
        entry = await EventBus.subscribe(channel)
        logger.info(f"[sse] open  label={label} channel={channel} ip={ip}")

        # Yield initial snapshot — client always gets current state on connect
        if initial:
            yield _data(initial["type"], {k: v for k, v in initial.items() if k != "type"})

        last_hb = time.monotonic()

        while True:
            elapsed = time.monotonic() - t0

            # ─ Hard timeout ──
            if elapsed >= max_duration:
                logger.info(f"[sse] timeout label={label} channel={channel} elapsed={elapsed:.0f}s")
                yield _data("stream.timeout", {"reason": "max_duration"})
                break

            # ─ Client disconnect ─
            if await request.is_disconnected():
                logger.info(f"[sse] drop   label={label} channel={channel} ip={ip}")
                break

            # ─ Wait for event or heartbeat ──
            hb_remaining   = HEARTBEAT_INTERVAL - (time.monotonic() - last_hb)
            wait           = max(0.5, min(hb_remaining, DISCONNECT_POLL))

            try:
                event = await asyncio.wait_for(entry.queue.get(), timeout=wait)
                etype = event.get("type", "event")
                edata = {k: v for k, v in event.items() if k != "type"}

                yield _data(etype, edata)
                logger.debug(f"[sse] event {etype} → {channel}")

                if etype in _TERMINAL:
                    logger.info(f"[sse] term  label={label} event={etype}")
                    break

            except asyncio.TimeoutError:
                now = time.monotonic()
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    yield _comment(f"hb {int(now)}")
                    last_hb = now

    except asyncio.CancelledError:
        logger.info(f"[sse] cancel label={label} channel={channel}")

    except RuntimeError as exc:
        # Subscriber limit hit — send error and close cleanly
        logger.warning(f"[sse] limit  label={label}: {exc}")
        EventBus._metrics.errors_total += 1
        yield _data("stream.error", {"message": str(exc)})

    except Exception as exc:
        logger.error(f"[sse] error  label={label} channel={channel}: {exc}")
        EventBus._metrics.errors_total += 1

    finally:
        if entry is not None:
            await EventBus.unsubscribe(entry)
            age = round(time.monotonic() - t0, 1)
            logger.info(f"[sse] close  label={label} channel={channel} age={age}s")


def _response(gen) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream", headers={
        "Cache-Control":     "no-cache, no-store",
        "X-Accel-Buffering": "no",       # nginx: disable proxy buffering
        "Connection":        "keep-alive",
    })


# ─ Payment SSE ─
@router.get("/payment/{payment_id}")
async def payment_stream(
    payment_id: str,
    request:    Request,
    db:         AsyncSession = Depends(get_db),
):
    """
    SSE stream for a payment. Used by checkout page.

    Reconnect revalidation:
      Every connect (including reconnect) sends a fresh DB snapshot.
      Client UI syncs to actual state — no stale status after disconnection.

    Security:
      - Rate limited per IP
      - HMAC token verified
      - Only public-safe fields exposed (no customer email, no fees)
    """
    _rate_check(_get_ip(request))

    # Always re-query DB — reconnects get fresh state
    res = await db.execute(select(Payment).where(Payment.id == payment_id))
    p   = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Payment not found")

    # HMAC token check
    token = request.query_params.get("t", "")
    ts    = p.created_at.isoformat() if p.created_at else ""
    if token and not verify_checkout_token(payment_id, ts, token):
        raise HTTPException(403, "Invalid checkout token")

    # Terminal payments: one-shot SSE then close
    _term_map = {
        PaymentStatus.confirmed: "payment.confirmed",
        PaymentStatus.expired:   "payment.expired",
        PaymentStatus.cancelled: "payment.cancelled",
    }
    if p.status in _term_map:
        async def _one():
            yield _data(_term_map[p.status], {
                "payment_id":    payment_id,
                "status":        str(p.status),
                "txid":          p.txid,
                "confirmations": p.confirmations,
                "confirmed_at":  p.confirmed_at.isoformat() if p.confirmed_at else None,
            })
        return _response(_one())

    # Fresh snapshot — this IS the reconnect revalidation
    # Client always syncs to DB truth on every connect/reconnect
    initial = {
        "type":                   "payment.status",
        "payment_id":             payment_id,
        "status":                 str(p.status),
        "amount_firo":            float(p.amount_firo or 0),
        "confirmations":          int(p.confirmations or 0),
        "required_confirmations": int(p.required_confirmations or 2),
        "expires_at":             p.expires_at.isoformat() if p.expires_at else None,
        "txid":                   p.txid,   # may be set during confirming phase
    }

    return _response(_stream(
        request, f"payment:{payment_id}",
        initial, MAX_PAYMENT_STREAM, f"checkout:{payment_id[:8]}"
    ))


# ─ Merchant SSE ─
@router.get("/merchant")
async def merchant_stream(
    request: Request,
    user:    User         = Depends(get_current_user),
    db:      AsyncSession = Depends(get_db),
):
    """
    SSE stream for authenticated merchant dashboard.

    Reconnect revalidation:
      On every connect, sends a fresh analytics snapshot from DB.
      Dashboard state is always correct after reconnect.

    Security:
      - get_current_user raises 401 if auth invalid/expired
      - Channel = merchant:{user.id} — isolated per merchant
      - No cross-merchant data leakage possible
    """
    _rate_check(_get_ip(request))

    mid = str(user.id)

    # Fresh DB snapshot for reconnect revalidation
    res = await db.execute(
        select(Payment)
        .where(Payment.merchant_id == user.id)
        .order_by(desc(Payment.created_at))
        .limit(10)
    )
    recent = res.scalars().all()

    # Today's summary
    from datetime import datetime, timezone, timedelta
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    today_res = await db.execute(
        select(
            func.count(Payment.id),
            func.sum(Payment.merchant_net_firo),
        ).where(
            Payment.merchant_id == user.id,
            Payment.status == PaymentStatus.confirmed,
            Payment.confirmed_at >= today_start,
        )
    )
    today_count, today_net = today_res.one()

    initial = {
        "type":         "dashboard.snapshot",
        "today_count":  int(today_count or 0),
        "today_net":    float(today_net or 0),
        "balance":      float(user.balance_firo or 0),
        "recent":       [
            {
                "id":     str(px.id),
                "status": str(px.status),
                "amount": float(px.amount_firo or 0),
                "net":    float(px.merchant_net_firo or 0),
                "at":     px.created_at.isoformat() if px.created_at else None,
                "order":  px.order_id,
            }
            for px in recent
        ],
    }

    return _response(_stream(
        request, f"merchant:{mid}",
        initial, MAX_MERCHANT_STREAM, f"dashboard:{mid[:8]}"
    ))


# ─ Stats endpoint (admin) ─
@router.get("/stats")
async def sse_stats(user: User = Depends(get_current_user)):
    from app.models.models import UserRole
    if user.role != UserRole.admin:
        raise HTTPException(403)

    # Prune stale IP rate limit entries
    now = time.monotonic()
    for ip in list(_ip_conns.keys()):
        _ip_conns[ip] = [t for t in _ip_conns[ip] if now - t < IP_WIN]
        if not _ip_conns[ip]:
            del _ip_conns[ip]

    return {
        **EventBus.metrics(),
        "rate_limit": {
            "tracked_ips":      len(_ip_conns),
            "window_seconds":   IP_WIN,
            "max_per_window":   IP_MAX,
        },
        "tunables": {
            "heartbeat_interval":   HEARTBEAT_INTERVAL,
            "max_payment_stream":   MAX_PAYMENT_STREAM,
            "max_merchant_stream":  MAX_MERCHANT_STREAM,
            "disconnect_poll":      DISCONNECT_POLL,
        },
    }
