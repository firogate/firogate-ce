"""
Payment Links API — /api/payment-links
Merchants create reusable checkout links from the dashboard without any API knowledge.
"""
import secrets, string
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import verify_access_token
from app.core.validators import validate_amount, sanitize_str, validate_url
from app.models.models import User, PaymentLink, Payment, PaymentStatus

router = APIRouter(prefix="/api/payment-links", tags=["payment-links"])


# ─ Auth helper ─
async def _merchant(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        token = request.cookies.get("access_token", "")
    uid = verify_access_token(token)
    if not uid:
        raise HTTPException(401, "Not authenticated")
    res = await db.execute(select(User).where(User.id == uid))
    u = res.scalar_one_or_none()
    if not u or not u.is_active:
        raise HTTPException(401, "Account inactive")
    return u


def _slug() -> str:
    """Generate a short URL-friendly slug."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _link_dict(lnk: PaymentLink, base_url: str) -> dict:
    return {
        "id":           lnk.id,
        "slug":         lnk.slug,
        "url":          f"{base_url.rstrip('/')}/pay/{lnk.slug}",
        "title":        lnk.title,
        "description":  lnk.description,
        "amount_firo":  lnk.amount_firo,
        "fixed_amount": lnk.fixed_amount,
        "collect_email":lnk.collect_email,
        "success_url":  lnk.success_url,
        "cancel_url":   lnk.cancel_url,
        "is_active":    lnk.is_active,
        "uses_count":   lnk.uses_count,
        "cancel_count": lnk.cancel_count or 0,
        "max_uses":     lnk.max_uses,
        "created_at":   lnk.created_at.isoformat() if lnk.created_at else None,
        "expires_at":   lnk.expires_at.isoformat() if lnk.expires_at else None,
    }


# ─ List ─
@router.get("/")
async def list_links(
    request: Request,
    db: AsyncSession = Depends(get_db),
    merchant: User = Depends(_merchant),
):
    res = await db.execute(
        select(PaymentLink)
        .where(PaymentLink.merchant_id == merchant.id)
        .order_by(PaymentLink.created_at.desc())
    )
    links = res.scalars().all()
    from app.core.config import get_settings
    base = get_settings().BASE_URL or str(request.base_url)
    return {"links": [_link_dict(lnk, base) for lnk in links]}


# ─ Create ──
class CreateLinkIn(BaseModel):
    title:         str
    description:   str | None = None
    amount_firo:   float | None = None
    fixed_amount:  bool = True
    collect_email: bool = True
    success_url:   str | None = None
    cancel_url:    str | None = None
    max_uses:      int | None = None
    expires_at:    str | None = None   # ISO 8601
    custom_slug:   str | None = None   # paid plans only


@router.post("/", status_code=201)
async def create_link(
    body: CreateLinkIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    merchant: User = Depends(_merchant),
):
    title = sanitize_str(body.title, max_len=128)
    if not title:
        raise HTTPException(422, "title is required")

    if body.amount_firo is not None:
        validate_amount(body.amount_firo)

    if body.success_url:
        validate_url(body.success_url)
    if body.cancel_url:
        validate_url(body.cancel_url)

    # ─ Custom slug — paid plans only ─
    # Community Edition — all users can use custom slugs
    custom_slug_val = None
    if body.custom_slug:
        raw = body.custom_slug.strip().lower()
        # Validate: 3-32 chars, letters/digits/hyphens only, no leading/trailing hyphens
        import re as _re
        if not _re.match(r'^[a-z0-9][a-z0-9\-]{1,30}[a-z0-9]$', raw) and not _re.match(r'^[a-z0-9]{3,32}$', raw):
            raise HTTPException(422, "Custom slug must be 3–32 characters: letters, digits, hyphens only. Cannot start or end with a hyphen.")
        # Check availability
        existing = await db.execute(select(PaymentLink).where(PaymentLink.slug == raw))
        if existing.scalar_one_or_none():
            raise HTTPException(409, f"The slug '{raw}' is already taken. Please choose a different name.")
        custom_slug_val = raw

    # Unique slug with collision retry
    if custom_slug_val:
        slug = custom_slug_val
    else:
        for _ in range(5):
            slug = _slug()
            exists = await db.execute(select(PaymentLink).where(PaymentLink.slug == slug))
            if not exists.scalar_one_or_none():
                break

    expires = None
    if body.expires_at:
        try:
            expires = datetime.fromisoformat(body.expires_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(422, "Invalid expires_at format")

    lnk = PaymentLink(
        merchant_id   = merchant.id,
        slug          = slug,
        title         = title,
        description   = sanitize_str(body.description or "", max_len=512) or None,
        amount_firo   = body.amount_firo,
        fixed_amount  = body.fixed_amount,
        collect_email = body.collect_email,
        success_url   = body.success_url,
        cancel_url    = body.cancel_url,
        max_uses      = body.max_uses,
        expires_at    = expires,
        created_at    = datetime.now(timezone.utc),
    )
    db.add(lnk)
    await db.commit()
    await db.refresh(lnk)

    from app.core.config import get_settings
    base = get_settings().BASE_URL or str(request.base_url)
    return _link_dict(lnk, base)


# ─ Toggle active ──
@router.patch("/{link_id}/toggle")
async def toggle_link(
    link_id: str,
    db: AsyncSession = Depends(get_db),
    merchant: User = Depends(_merchant),
):
    res = await db.execute(
        select(PaymentLink).where(PaymentLink.id == link_id, PaymentLink.merchant_id == merchant.id)
    )
    lnk = res.scalar_one_or_none()
    if not lnk:
        raise HTTPException(404, "Link not found")
    lnk.is_active = not lnk.is_active
    db.add(lnk)
    await db.commit()
    return {"id": lnk.id, "is_active": lnk.is_active}


# ─ Delete ──
@router.delete("/{link_id}", status_code=204)
async def delete_link(
    link_id: str,
    db: AsyncSession = Depends(get_db),
    merchant: User = Depends(_merchant),
):
    res = await db.execute(
        select(PaymentLink).where(PaymentLink.id == link_id, PaymentLink.merchant_id == merchant.id)
    )
    lnk = res.scalar_one_or_none()
    if not lnk:
        raise HTTPException(404, "Link not found")
    await db.delete(lnk)
    await db.commit()


# ─ Public: open a payment link → generate a Payment ─
@router.get("/public/{slug}")
async def get_public_link(slug: str, db: AsyncSession = Depends(get_db)):
    """Returns link metadata for the payment link landing page."""
    res = await db.execute(select(PaymentLink).where(PaymentLink.slug == slug))
    lnk = res.scalar_one_or_none()
    if not lnk or not lnk.is_active:
        raise HTTPException(404, "Payment link not found or inactive")
    now = datetime.now(timezone.utc)
    if lnk.expires_at and lnk.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(410, "Payment link has expired")
    if lnk.max_uses and lnk.uses_count >= lnk.max_uses:
        raise HTTPException(410, "This payment link has reached its usage limit.")
    return {
        "title":        lnk.title,
        "description":  lnk.description,
        "amount_firo":  lnk.amount_firo,
        "fixed_amount": lnk.fixed_amount,
        "collect_email":lnk.collect_email,
        "merchant_id":  lnk.merchant_id,
        "slug":         lnk.slug,
    }


@router.post("/public/{slug}/checkout", status_code=201)
async def checkout_from_link(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Creates a real Payment from a payment link and returns the checkout URL."""
    from datetime import timedelta
    import json as _json

    res = await db.execute(select(PaymentLink).where(PaymentLink.slug == slug))
    lnk = res.scalar_one_or_none()
    if not lnk or not lnk.is_active:
        raise HTTPException(404, "Payment link not found or inactive")

    now = datetime.now(timezone.utc)
    if lnk.expires_at:
        exp = lnk.expires_at if lnk.expires_at.tzinfo else lnk.expires_at.replace(tzinfo=timezone.utc)
        if exp < now:
            raise HTTPException(410, "Payment link has expired")
    # Check max_uses against confirmed payments only (not attempts)
    # This prevents false "Usage limit reached" when user hits back and retries
    if lnk.max_uses:
        from sqlalchemy import text as _text
        count_res = await db.execute(
            _text("SELECT COUNT(*) FROM payments WHERE metadata_json LIKE :pat AND merchant_id = :mid AND status = 'confirmed'"),
            {"pat": f'%"slug": "{lnk.slug}"%', "mid": str(lnk.merchant_id)}
        )
        confirmed_count = count_res.scalar() or 0
        if confirmed_count >= lnk.max_uses:
            raise HTTPException(410, "This payment link has reached its usage limit.")

    # Parse body
    try:
        body = await request.json()
    except Exception:
        body = {}

    amount = float(body.get("amount_firo") or lnk.amount_firo or 0)
    if amount <= 0:
        raise HTTPException(422, "amount_firo required")

    customer_email = (body.get("customer_email") or body.get("email") or "").strip() or None

    # Get merchant
    res2 = await db.execute(select(User).where(User.id == lnk.merchant_id))
    merchant = res2.scalar_one_or_none()
    if not merchant or not merchant.is_active:
        raise HTTPException(503, "Merchant unavailable")

    # Check quota
    # Community Edition — no quota limits

    # Get HD address from Firo node
    from app.services.firo_rpc import get_rpc
    from app.core.config import get_settings
    settings = get_settings()
    rpc = get_rpc()
    address = await rpc.get_new_address()

    payment = Payment(
        merchant_id            = merchant.id,
        receiving_address      = address,
        amount_firo            = amount,
        platform_fee_pct       = settings.PLATFORM_FEE_PCT,
        order_id               = f"LINK-{lnk.slug}",
        order_description      = lnk.title,
        customer_email         = customer_email,
        customer_ip            = request.client.host if request.client else None,
        collect_email          = lnk.collect_email,
        success_url            = lnk.success_url,
        cancel_url             = lnk.cancel_url,
        required_confirmations = 2,
        expires_at             = now + timedelta(minutes=30),
        metadata_json          = _json.dumps({"link_id": str(lnk.id), "slug": lnk.slug}),
    )
    db.add(payment)

    # uses_count is a display counter — increment on each attempt (not for blocking)
    lnk.uses_count = (lnk.uses_count or 0) + 1
    db.add(lnk)

    # Increment merchant quota usage
    # Community Edition — no request counting
    db.add(merchant)

    await db.commit()
    await db.refresh(payment)

    from app.core.security import generate_checkout_token as _gct
    _tok = _gct(str(payment.id), payment.created_at.isoformat() if payment.created_at else "")
    checkout_url = settings.BASE_URL.rstrip("/") + f"/invoice/{payment.id}?t={_tok}"
    return {
        "checkout_url":      checkout_url,
        "payment_id":        payment.id,
        "receiving_address": address,
        "amount_firo":       amount,
        "expires_at":        payment.expires_at.isoformat() if payment.expires_at else None,
    }


# ─ CSV Export ─
@router.get("/export/csv")
async def export_payments_csv(
    request: Request,
    db: AsyncSession = Depends(get_db),
    merchant: User = Depends(_merchant),
):
    """Download all confirmed payments as CSV."""
    res = await db.execute(
        select(Payment)
        .where(Payment.merchant_id == merchant.id)
        .order_by(Payment.created_at.desc())
    )
    payments = res.scalars().all()

    import csv, io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Payment ID", "Order ID", "Order Description",
        "Amount FIRO", "Fee FIRO", "Net FIRO",
        "Status", "Customer Email", "TXID",
        "Created At", "Confirmed At",
    ])
    for p in payments:
        writer.writerow([
            p.id,
            p.order_id or "",
            p.order_description or "",
            f"{p.amount_firo:.8f}" if p.amount_firo else "",
            f"{p.platform_fee_firo:.8f}" if p.platform_fee_firo else "",
            f"{p.merchant_net_firo:.8f}" if p.merchant_net_firo else "",
            p.status.value if p.status else "",
            p.customer_email or "",
            p.txid or "",
            p.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if p.created_at else "",
            p.confirmed_at.strftime("%Y-%m-%d %H:%M:%S UTC") if p.confirmed_at else "",
        ])

    csv_bytes = buf.getvalue().encode("utf-8-sig")  # utf-8-sig for Excel compatibility
    filename = f"payments_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─ Notification settings ─
@router.get("/notifications/settings")
async def get_notification_settings(
    merchant: User = Depends(_merchant),
):
    return {
        "notify_on_payment": getattr(merchant, "notify_on_payment", True),
        "notify_email":      getattr(merchant, "notify_email", None) or merchant.email,
    }


@router.patch("/notifications/settings")
async def update_notification_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    merchant: User = Depends(_merchant),
):
    body = await request.json()
    if "notify_on_payment" in body:
        merchant.notify_on_payment = bool(body["notify_on_payment"])
    if "notify_email" in body:
        email = str(body["notify_email"] or "").strip()
        merchant.notify_email = email if email else None
    db.add(merchant)
    await db.commit()
    return {"ok": True}
