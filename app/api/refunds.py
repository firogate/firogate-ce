import asyncio
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.payments import get_merchant, require_api_permission
from app.core.database import get_db
from app.core.validators import sanitize_str
from app.models.models import Payment, PaymentStatus, Refund, User

router = APIRouter(prefix="/api", tags=["refunds"])

_TXID_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_EPS = 1e-8


class RefundIn(BaseModel):
    amount_firo: float
    txid: str | None = None
    reason: str | None = None


def _refund_dict(r: Refund) -> dict:
    return {
        "id": r.id,
        "payment_id": r.payment_id,
        "amount_firo": r.amount_firo,
        "txid": r.txid,
        "reason": r.reason,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def refund_totals(db: AsyncSession, payment_ids: list[str]) -> dict[str, float]:
    if not payment_ids:
        return {}
    res = await db.execute(
        select(Refund.payment_id, func.sum(Refund.amount_firo))
        .where(Refund.payment_id.in_(payment_ids))
        .group_by(Refund.payment_id)
    )
    return {pid: round(float(total or 0), 8) for pid, total in res.all()}


async def _own_payment(db: AsyncSession, merchant: User, payment_id: str) -> Payment:
    res = await db.execute(select(Payment).where(Payment.id == payment_id, Payment.merchant_id == merchant.id))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Payment not found")
    return p


def _paid_amount(p: Payment) -> float:
    return float(p.amount_received or p.amount_firo or 0)


@router.get("/payments/{payment_id}/refunds")
async def list_payment_refunds(
    payment_id: str,
    merchant: User = Depends(require_api_permission("read_invoice")),
    db: AsyncSession = Depends(get_db),
):
    p = await _own_payment(db, merchant, payment_id)
    res = await db.execute(select(Refund).where(Refund.payment_id == p.id).order_by(Refund.created_at.desc()))
    items = [_refund_dict(r) for r in res.scalars().all()]
    refunded = round(sum(i["amount_firo"] for i in items), 8)
    paid = _paid_amount(p)
    return {
        "payment_id": p.id,
        "paid_firo": paid,
        "refunded_firo": refunded,
        "refundable_firo": max(0.0, round(paid - refunded, 8)),
        "refunds": items,
    }


@router.post("/payments/{payment_id}/refunds", status_code=201)
async def record_refund(
    payment_id: str,
    body: RefundIn,
    merchant: User = Depends(require_api_permission("manage_refunds")),
    db: AsyncSession = Depends(get_db),
):
    p = await _own_payment(db, merchant, payment_id)
    status = p.status.value if hasattr(p.status, "value") else p.status
    if status != PaymentStatus.confirmed.value:
        raise HTTPException(409, "Only confirmed payments can be refunded.")
    try:
        amount = round(float(body.amount_firo), 8)
    except (TypeError, ValueError):
        raise HTTPException(422, "Enter a valid amount.")
    if not amount > 0:
        raise HTTPException(422, "Refund amount must be more than 0.")
    txid = (body.txid or "").strip() or None
    if txid and not _TXID_RE.match(txid):
        raise HTTPException(422, "TX hash must be 64 hex characters.")
    reason = sanitize_str((body.reason or "").strip(), 280) or None

    already = (await refund_totals(db, [p.id])).get(p.id, 0.0)
    paid = _paid_amount(p)
    left = round(paid - already, 8)
    if amount > left + _EPS:
        raise HTTPException(422, f"You can refund up to {max(left, 0):.8f} FIRO for this payment.")

    r = Refund(payment_id=p.id, merchant_id=merchant.id, amount_firo=amount, txid=txid, reason=reason)
    db.add(r)
    await db.commit()
    await db.refresh(r)

    total = round(already + amount, 8)
    full = total >= paid - _EPS
    from app.services.webhook import fire_event_webhook
    asyncio.create_task(fire_event_webhook(merchant.id, "payment.refunded", {
        "payment_id": p.id,
        "order_id": p.order_id,
        "refund_id": r.id,
        "amount_firo": amount,
        "refunded_total_firo": total,
        "fully_refunded": full,
        "txid": txid,
        "reason": reason,
        "refunded_at": r.created_at.isoformat() if r.created_at else datetime.now(timezone.utc).isoformat(),
    }))
    return dict(_refund_dict(r), refunded_total_firo=total, fully_refunded=full)


@router.delete("/refunds/{refund_id}", status_code=204)
async def delete_refund(
    refund_id: str,
    merchant: User = Depends(require_api_permission("manage_refunds")),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Refund).where(Refund.id == refund_id, Refund.merchant_id == merchant.id))
    r = res.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Refund not found")
    await db.delete(r)
    await db.commit()


@router.get("/refunds/summary")
async def refunds_summary(
    merchant: User = Depends(require_api_permission("read_analytics")),
    db: AsyncSession = Depends(get_db),
    days: int = 30,
):
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 3650)))
    total, count = (await db.execute(
        select(func.coalesce(func.sum(Refund.amount_firo), 0), func.count())
        .where(Refund.merchant_id == merchant.id)
    )).one()
    period, pcount = (await db.execute(
        select(func.coalesce(func.sum(Refund.amount_firo), 0), func.count())
        .where(Refund.merchant_id == merchant.id, Refund.created_at >= since)
    )).one()
    return {
        "total_firo": round(float(total or 0), 8),
        "count": int(count or 0),
        "period_days": days,
        "period_firo": round(float(period or 0), 8),
        "period_count": int(pcount or 0),
    }
