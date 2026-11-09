import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.models import Payment, PaymentAuditEvent, PaymentStatus, User
from app.services.event_bus import EventBus, make_event

_state = {"mode": None, "last_error": None, "checked_at": None}


def watcher_state() -> dict:
    return dict(_state)


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _credits_from_index(rpc, addresses: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {a: [] for a in addresses}
    deltas = await rpc.call("getaddressdeltas", {"addresses": addresses})
    for d in deltas or []:
        if int(d.get("satoshis", 0)) > 0 and d.get("address") in out:
            out[d["address"]].append({
                "txid": d["txid"], "vout": int(d.get("index", 0)),
                "sats": int(d["satoshis"]), "height": d.get("height"), "time": None,
            })
    mem = await rpc.call("getaddressmempool", {"addresses": addresses})
    for d in mem or []:
        if int(d.get("satoshis", 0)) > 0 and d.get("address") in out:
            out[d["address"]].append({
                "txid": d["txid"], "vout": int(d.get("index", 0)),
                "sats": int(d["satoshis"]), "height": None, "time": d.get("timestamp"),
            })
    return out


async def _credits_from_wallet(rpc, addresses: list[str], tip: int | None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {a: [] for a in addresses}
    rows = await rpc.call("listunspent", 0, 9999999, addresses, True)
    for u in rows or []:
        addr = u.get("address")
        if addr not in out:
            continue
        confs = int(u.get("confirmations", 0))
        height = (tip - confs + 1) if (tip is not None and confs > 0) else None
        out[addr].append({
            "txid": u["txid"], "vout": int(u.get("vout", 0)),
            "sats": int(round(float(u.get("amount", 0)) * 1e8)), "height": height, "time": None,
        })
    return out


async def _apply(db, payment: Payment, credits: list[dict], tip: int | None, now: datetime) -> None:
    from app.core.payment_policy import resolve_tolerance_firo
    from app.services.payment_monitor import _confirm_payment, _write_audit_log

    created = _aware(payment.created_at)
    valid = []
    for c in credits:
        if c["height"] is not None and payment.start_block_height is not None and c["height"] < payment.start_block_height:
            continue
        if c["height"] is None and c["time"] and created and datetime.fromtimestamp(int(c["time"]), tz=timezone.utc) < created:
            continue
        valid.append(c)
    if not valid:
        return

    seen = {}
    for c in valid:
        seen[(c["txid"], c["vout"])] = c
    valid = list(seen.values())
    received = round(sum(c["sats"] for c in valid) / 1e8, 8)
    heights = [c["height"] for c in valid]
    if tip is not None and all(h is not None for h in heights):
        confs = max(0, tip - max(heights) + 1)
    else:
        confs = 0
    latest = max(valid, key=lambda c: (c["height"] is None, c["height"] or 0))

    prev_t = round(payment.transparent_received or 0, 8)
    changed = (prev_t != received) or (payment.confirmations != confs) or (payment.txid != latest["txid"])
    if not changed and payment.status != PaymentStatus.pending:
        return

    first_seen = not prev_t
    other = max(0.0, round((payment.amount_received or 0) - prev_t, 8))
    payment.transparent_received = received
    received = round(other + received, 8)
    payment.amount_received = received
    payment.confirmations = confs
    payment.txid = latest["txid"]
    payment.vout = latest["vout"]
    db.add(payment)

    merchant = (await db.execute(select(User).where(User.id == payment.merchant_id))).scalar_one_or_none()
    tolerance = resolve_tolerance_firo(merchant)
    diff = round(received - payment.amount_firo, 8)

    if diff < 0 and abs(diff) > tolerance:
        if first_seen or changed:
            await _write_audit_log(db, payment, PaymentAuditEvent.payment_detected,
                                   detail=f"transparent partial received={received:.8f} expected={payment.amount_firo:.8f}")
        await db.commit()
        asyncio.create_task(EventBus.publish_payment(str(payment.id), make_event(
            "payment.detected", payment_id=str(payment.id),
            amount_received=float(received), confirmations=int(confs),
        )))
        return

    if first_seen:
        await _write_audit_log(db, payment, PaymentAuditEvent.payment_detected,
                               detail=f"transparent txid={latest['txid'][:16]} received={received:.8f}")
    req = payment.required_confirmations if payment.required_confirmations is not None else get_settings().REQUIRED_CONFIRMATIONS
    if confs >= req:
        await _confirm_payment(db, payment, now)
        return
    payment.status = PaymentStatus.confirming
    db.add(payment)
    await db.commit()
    asyncio.create_task(EventBus.publish_payment(str(payment.id), make_event(
        "payment.detected", payment_id=str(payment.id),
        amount_received=float(received), confirmations=int(confs),
    )))


async def check_transparent_payments() -> None:
    from app.services.firo_rpc import get_rpc
    now = datetime.now(timezone.utc)
    _state["checked_at"] = now.isoformat()
    try:
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(Payment).where(
                Payment.transparent_address.is_not(None),
                Payment.status.in_([PaymentStatus.pending, PaymentStatus.confirming]),
            ))
            payments = res.scalars().all()
            if not payments:
                return
            rpc = get_rpc()
            try:
                tip = await rpc.get_block_count()
            except Exception:
                tip = None
            addresses = sorted({p.transparent_address for p in payments if p.transparent_address})
            credits = None
            if _state["mode"] == "wallet" and (now.timestamp() - _state.get("mode_at", 0)) > 600:
                _state["mode"] = None
            if _state["mode"] in (None, "index"):
                try:
                    credits = await _credits_from_index(rpc, addresses)
                    _state["mode"] = "index"
                except Exception as e:
                    _state["last_error"] = str(e)[:160]
                    _state["mode"] = "wallet"
                    _state["mode_at"] = now.timestamp()
            if credits is None:
                credits = await _credits_from_wallet(rpc, addresses, tip)
            _state["last_error"] = None
            for p in payments:
                found = credits.get(p.transparent_address) or []
                if found:
                    try:
                        await _apply(db, p, found, tip, now)
                    except Exception as e:
                        await db.rollback()
                        logger.warning(f"[xpub] payment {p.id[:8]} update failed: {e}")
    except Exception as e:
        _state["last_error"] = str(e)[:160]
        logger.warning(f"[xpub] watcher error: {e}")
