import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.payments import require_api_permission
from app.core.config import get_settings
from app.core.database import get_db
from app.core.validators import sanitize_str, validate_amount
from app.models.models import PaymentLink, RecurringPlan, User
from app.services.recurring_billing import INTERVALS, bill_plan

router = APIRouter(prefix="/api/recurring", tags=["recurring"])

_EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{1,63}$")
_MAX_PLANS = 500


class PlanIn(BaseModel):
    title: str
    amount_firo: float
    interval: str = "month"
    customer_email: str
    customer_name: str | None = None
    description: str | None = None
    start_at: str | None = None
    max_runs: int | None = None


class PlanPatch(BaseModel):
    is_active: bool | None = None


async def _plan_dict(db: AsyncSession, p: RecurringPlan) -> dict:
    url = None
    if p.last_link_id:
        slug = (await db.execute(select(PaymentLink.slug).where(PaymentLink.id == p.last_link_id))).scalar_one_or_none()
        if slug:
            url = f"{get_settings().get_checkout_base_url('')}/pay/{slug}"
    iso = lambda d: d.isoformat() if d else None
    return {
        "id": p.id,
        "title": p.title,
        "description": p.description,
        "amount_firo": p.amount_firo,
        "interval": p.interval,
        "customer_email": p.customer_email,
        "customer_name": p.customer_name,
        "is_active": bool(p.is_active),
        "next_run_at": iso(p.next_run_at),
        "last_run_at": iso(p.last_run_at),
        "runs_count": p.runs_count or 0,
        "max_runs": p.max_runs,
        "last_error": p.last_error,
        "last_pay_url": url,
        "created_at": iso(p.created_at),
    }


async def _own_plan(db: AsyncSession, merchant: User, plan_id: str) -> RecurringPlan:
    res = await db.execute(select(RecurringPlan).where(RecurringPlan.id == plan_id, RecurringPlan.merchant_id == merchant.id))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Recurring payment not found")
    return p


@router.get("")
async def list_plans(
    merchant: User = Depends(require_api_permission("manage_recurring")),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(RecurringPlan).where(RecurringPlan.merchant_id == merchant.id).order_by(RecurringPlan.created_at.desc()))
    return [await _plan_dict(db, p) for p in res.scalars().all()]


@router.post("", status_code=201)
async def create_plan(
    body: PlanIn,
    merchant: User = Depends(require_api_permission("manage_recurring")),
    db: AsyncSession = Depends(get_db),
):
    title = sanitize_str((body.title or "").strip(), 128)
    if not title:
        raise HTTPException(422, "Add a name for this payment.")
    try:
        amount = validate_amount(body.amount_firo)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if body.interval not in INTERVALS:
        raise HTTPException(422, "Choose weekly, monthly or yearly.")
    email = (body.customer_email or "").strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(422, "Enter a valid customer email.")
    if body.max_runs is not None and not (1 <= body.max_runs <= 1000):
        raise HTTPException(422, "Number of payments must be between 1 and 1000.")
    start = datetime.now(timezone.utc)
    if body.start_at:
        try:
            start = datetime.fromisoformat(body.start_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(422, "Invalid start date.")
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    count = len((await db.execute(select(RecurringPlan.id).where(RecurringPlan.merchant_id == merchant.id))).all())
    if count >= _MAX_PLANS:
        raise HTTPException(409, "You have reached the limit of recurring payments.")
    p = RecurringPlan(
        merchant_id=merchant.id,
        title=title,
        description=sanitize_str((body.description or "").strip(), 512) or None,
        amount_firo=amount,
        interval=body.interval,
        customer_email=email,
        customer_name=sanitize_str((body.customer_name or "").strip(), 128) or None,
        max_runs=body.max_runs,
        anchor_day=start.day,
        next_run_at=start,
        is_active=True,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return await _plan_dict(db, p)


@router.patch("/{plan_id}")
async def update_plan(
    plan_id: str,
    body: PlanPatch,
    merchant: User = Depends(require_api_permission("manage_recurring")),
    db: AsyncSession = Depends(get_db),
):
    p = await _own_plan(db, merchant, plan_id)
    if body.is_active is not None:
        if body.is_active and p.max_runs and (p.runs_count or 0) >= p.max_runs:
            raise HTTPException(409, "All payments in this plan were already sent.")
        p.is_active = body.is_active
        if body.is_active:
            now = datetime.now(timezone.utc)
            nxt = p.next_run_at if p.next_run_at.tzinfo else p.next_run_at.replace(tzinfo=timezone.utc)
            if nxt < now:
                p.next_run_at = now
    await db.commit()
    await db.refresh(p)
    return await _plan_dict(db, p)


@router.post("/{plan_id}/send-now")
async def send_now(
    plan_id: str,
    merchant: User = Depends(require_api_permission("manage_recurring")),
    db: AsyncSession = Depends(get_db),
):
    p = await _own_plan(db, merchant, plan_id)
    if not p.is_active:
        raise HTTPException(409, "Turn this payment on first.")
    result = await bill_plan(db, p)
    await db.commit()
    if not result.get("ok"):
        raise HTTPException(409, result.get("error") or "Could not send the invoice.")
    await db.refresh(p)
    return dict(await _plan_dict(db, p), emailed=result.get("emailed", False))


@router.delete("/{plan_id}", status_code=204)
async def delete_plan(
    plan_id: str,
    merchant: User = Depends(require_api_permission("manage_recurring")),
    db: AsyncSession = Depends(get_db),
):
    p = await _own_plan(db, merchant, plan_id)
    await db.delete(p)
    await db.commit()
