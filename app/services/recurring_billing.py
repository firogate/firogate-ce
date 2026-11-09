import asyncio
import calendar
import html as _html
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from app.core.config import get_settings
from app.models.models import PaymentLink, RecurringPlan, User

INTERVALS = ("week", "month", "year")


def advance(dt: datetime, interval: str, anchor_day: int | None = None) -> datetime:
    if interval == "week":
        return dt + timedelta(days=7)
    if interval == "year":
        try:
            return dt.replace(year=dt.year + 1)
        except ValueError:
            return dt.replace(year=dt.year + 1, day=28)
    month = dt.month + 1
    year = dt.year + (1 if month > 12 else 0)
    month = 1 if month > 12 else month
    day = min(anchor_day or dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _wallet_ready(db, merchant_id: str) -> bool:
    from app.api.spark_connect_helpers import merchant_can_receive
    return await merchant_can_receive(db, merchant_id)


async def _email_customer(merchant: User, plan: RecurringPlan, url: str, due_by: datetime) -> bool:
    from app.services.mailer import _fmt_firo, _is_configured, _wrap, send_email
    if not _is_configured():
        return False
    store = (merchant.app_name or merchant.username or "Store").strip()
    e = _html.escape
    greet = f"Hi {e(plan.customer_name)}," if plan.customer_name else "Hi,"
    body_html = (
        f"<p style='margin:0 0 12px 0'>{greet}</p>"
        f"<p style='margin:0 0 12px 0'>Your payment for <strong>{e(plan.title)}</strong> is ready.</p>"
        f"<p style='margin:0 0 4px 0'><strong>Amount:</strong> {_fmt_firo(plan.amount_firo)} FIRO</p>"
        f"<p style='margin:0 0 4px 0'><strong>Pay by:</strong> {due_by.strftime('%Y-%m-%d')}</p>"
    )
    html, plain = _wrap(
        title=f"{e(store)} payment due",
        heading="Payment due",
        body_html=body_html,
        cta_label="Pay now",
        cta_url=e(url, quote=True),
    )
    plain = (
        f"{greet}\n\nYour payment for {plan.title} is ready.\n"
        f"Amount: {_fmt_firo(plan.amount_firo)} FIRO\nPay by: {due_by.strftime('%Y-%m-%d')}\n\nPay now: {url}\n"
    )
    return await send_email(plan.customer_email, f"{store}: payment due for {plan.title}", html, text=plain, from_name=store)


async def bill_plan(db, plan: RecurringPlan, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    merchant = (await db.execute(select(User).where(User.id == plan.merchant_id))).scalar_one_or_none()
    if not merchant or not merchant.is_active:
        plan.last_error = "Account is not active."
        return {"ok": False, "error": plan.last_error}
    if not await _wallet_ready(db, merchant.id):
        plan.last_error = "Connect your wallet to send invoices."
        return {"ok": False, "error": plan.last_error}

    from app.api.payment_links import _slug
    slug = None
    for _ in range(6):
        cand = _slug()
        if not (await db.execute(select(PaymentLink.id).where(PaymentLink.slug == cand))).scalar_one_or_none():
            slug = cand
            break
    if not slug:
        plan.last_error = "Could not create a pay link. Will retry."
        return {"ok": False, "error": plan.last_error}

    due_by = advance(now, plan.interval)
    link = PaymentLink(
        merchant_id=merchant.id,
        slug=slug,
        title=plan.title,
        description=plan.description,
        amount_firo=plan.amount_firo,
        fixed_amount=True,
        collect_email=True,
        is_active=True,
        max_uses=None,
        expires_at=due_by,
        created_at=now,
    )
    db.add(link)
    await db.flush()

    url = f"{get_settings().get_checkout_base_url('')}/pay/{slug}"
    plan.last_link_id = link.id
    plan.last_run_at = now
    plan.runs_count = (plan.runs_count or 0) + 1
    if plan.max_runs and plan.runs_count >= plan.max_runs:
        plan.is_active = False
    plan.last_error = None
    await db.commit()

    sent = False
    try:
        sent = await _email_customer(merchant, plan, url, due_by)
    except Exception as e:
        logger.warning(f"[recurring] email failed for plan {plan.id[:8]}: {e}")
    if not sent:
        plan.last_error = "Email is off or failed. Share the link yourself."
        await db.commit()

    from app.services.webhook import fire_event_webhook
    asyncio.create_task(fire_event_webhook(merchant.id, "recurring.invoice_created", {
        "plan_id": plan.id,
        "title": plan.title,
        "amount_firo": plan.amount_firo,
        "interval": plan.interval,
        "customer_email": plan.customer_email,
        "pay_url": url,
        "due_by": due_by.isoformat(),
        "run": plan.runs_count,
    }))
    return {"ok": True, "url": url, "emailed": sent, "due_by": due_by.isoformat()}


async def run_recurring_billing() -> None:
    from app.core.database import AsyncSessionLocal
    now = datetime.now(timezone.utc)
    try:
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(RecurringPlan).where(
                RecurringPlan.is_active == True,
                RecurringPlan.next_run_at <= now,
            ).limit(100))
            for plan in res.scalars().all():
                nxt = _aware(plan.next_run_at)
                while nxt <= now:
                    nxt = advance(nxt, plan.interval, plan.anchor_day)
                try:
                    result = await bill_plan(db, plan, now)
                except Exception as e:
                    await db.rollback()
                    logger.warning(f"[recurring] plan {plan.id[:8]} failed: {e}")
                    result = {"ok": False}
                plan.next_run_at = nxt if result.get("ok") else now + timedelta(hours=1)
                await db.commit()
    except Exception as e:
        logger.warning(f"[recurring] run failed: {e}")
