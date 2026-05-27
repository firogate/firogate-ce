import json
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from app.core.rate_limit import rate_limit_moderate
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.core.database import get_db
from app.core.security import verify_access_token
from app.core.config import get_settings
from app.core.validators import validate_amount, validate_clean, validate_url, sanitize_str
from app.models.models import Payment, PaymentStatus, User, UserRole, AuditLog

settings      = get_settings()
router        = APIRouter(prefix="/api/payments", tags=["payments"])
public_router = APIRouter(prefix="/api/payments", tags=["public"])


async def get_merchant(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_api_key: str | None = Header(default=None),
) -> User:
    if x_api_key:
        # Check new multi-key table first, then legacy single-key fallback
        from app.api.api_keys import get_merchant_by_api_key
        u = await get_merchant_by_api_key(x_api_key, db)
        if u and u.is_active: return u

    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token: token = request.cookies.get("access_token", "")
    uid = verify_access_token(token)
    if uid:
        res = await db.execute(select(User).where(User.id == uid))
        u = res.scalar_one_or_none()
        if u and u.is_active: return u

    raise HTTPException(401, "missing credentials")


def _check_quota(m: User):
    pass  # Community Edition — no quota limits


class CreatePaymentIn(BaseModel):
    amount_firo:            float
    order_id:               str | None = None
    order_description:      str | None = None
    customer_email:         str | None = None
    success_url:            str | None = None
    cancel_url:             str | None = None
    required_confirmations: int = 2
    timeout_minutes:        int = 20
    collect_email:          bool = True
    metadata:               dict | None = None


@router.post("/create", status_code=201, dependencies=[Depends(rate_limit_moderate)])
async def create_payment(
    body: CreatePaymentIn,
    request: Request,
    merchant: User = Depends(get_merchant),
    db: AsyncSession = Depends(get_db),
):
    _check_quota(merchant)

    try:
        body.amount_firo       = validate_amount(body.amount_firo)
        body.order_id          = sanitize_str(body.order_id, 256)
        body.order_description = sanitize_str(body.order_description, 512)
        body.customer_email    = sanitize_str(body.customer_email, 254)
        body.success_url       = validate_url(body.success_url, "success_url")
        body.cancel_url        = validate_url(body.cancel_url, "cancel_url")
    except ValueError as e:
        raise HTTPException(422, str(e))

    from app.services.firo_rpc import get_rpc
    rpc = get_rpc()

    payment = Payment(
        merchant_id=merchant.id,
        receiving_address="",
        amount_firo=body.amount_firo,
        platform_fee_pct=settings.PLATFORM_FEE_PCT,
        order_id=body.order_id,
        order_description=body.order_description,
        customer_email=body.customer_email,
        customer_ip=request.client.host if request.client else None,
        collect_email=body.collect_email,
        success_url=body.success_url,
        cancel_url=body.cancel_url,
        required_confirmations=body.required_confirmations,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=body.timeout_minutes),
        metadata_json=json.dumps(body.metadata) if body.metadata else None,
    )
    db.add(payment)
    await db.flush()

    try:
        address = await rpc.get_new_address_for_payment(payment.id)
    except Exception as e:
        raise HTTPException(503, f"Cannot generate address: {e}")

    payment.receiving_address    = address
    merchant.balance_pending     = round((merchant.balance_pending or 0) + body.amount_firo, 8)

    db.add(AuditLog(
        user_id=merchant.id, action="payment.created",
        entity_type="payment", entity_id=payment.id,
        detail=f"amount={body.amount_firo} addr={address[:20]}…",
        ip_address=request.client.host if request.client else None,
    ))

    _host   = request.headers.get("host", "")
    _origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    _src    = _host or _origin
    _base   = settings.get_checkout_base_url(_src)
    # Generate HMAC token for this payment — protects public endpoints from enumeration
    from app.core.security import generate_checkout_token as _gct
    _tok = _gct(str(payment.id), payment.created_at.isoformat() if payment.created_at else "")
    return {
        "payment_id":             payment.id,
        "checkout_url":           f"{_base}/invoice/{payment.id}?t={_tok}",
        "receiving_address":      address,
        "amount_firo":            body.amount_firo,
        "platform_fee_pct":       settings.PLATFORM_FEE_PCT,
        "order_id":               body.order_id,
        "expires_at":             payment.expires_at.isoformat(),
        "required_confirmations": body.required_confirmations,
        "status":                 payment.status,
    }


@router.get("/")
async def list_payments(
    merchant: User = Depends(get_merchant),
    db: AsyncSession = Depends(get_db),
    limit: int = 50, offset: int = 0, status: str | None = None,
):
    q = select(Payment).where(
        Payment.merchant_id == merchant.id
    ).order_by(Payment.created_at.desc()).limit(min(limit, 200)).offset(offset)
    if status: q = q.where(Payment.status == status)
    res = await db.execute(q)
    return [_fmt(p) for p in res.scalars().all()]


@router.get("/{payment_id}")
async def get_payment(
    payment_id: str,
    merchant: User = Depends(get_merchant),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(Payment).where(Payment.id == payment_id, Payment.merchant_id == merchant.id)
    )
    p = res.scalar_one_or_none()
    if not p: raise HTTPException(404, "Payment not found")
    return _fmt(p, full=True)


def _fmt(p: Payment, full: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    exp = p.expires_at
    if exp and exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)

    status = p.status
    if status in (PaymentStatus.pending, PaymentStatus.confirming) and exp and exp < now:
        status = PaymentStatus.expired
    if status == PaymentStatus.confirmed and not p.txid:
        status = PaymentStatus.pending

    d = {
        "payment_id":             p.id,
        "status":                 status,
        "amount_firo":            p.amount_firo,
        "amount_received":        p.amount_received,
        "txid":                   p.txid,
        "confirmations":          p.confirmations,
        "required_confirmations": p.required_confirmations,
        "receiving_address":      p.receiving_address,
        "order_id":               p.order_id,
        "order_description":      p.order_description,
        "collect_email":          p.collect_email,
        # Boolean flag only — never expose the actual email in public endpoint
        "email_collected":        bool(p.customer_email),
        # cancel_url and success_url needed by checkout for redirect
        "cancel_url":             p.cancel_url,
        "success_url":            p.success_url,
        "created_at":             p.created_at.isoformat(),
        "expires_at":             p.expires_at.isoformat() if p.expires_at else None,
        "confirmed_at":           p.confirmed_at.isoformat() if p.confirmed_at else None,
    }
    if full:
        # Only for authenticated merchant — includes sensitive fields
        d.update({
            "customer_email":    p.customer_email,
            "platform_fee_pct":  p.platform_fee_pct,
            "platform_fee_firo": p.platform_fee_firo,
            "merchant_net_firo": p.merchant_net_firo,
            "vout":              p.vout,
            "webhook_sent":      p.webhook_sent,
        })
    return d


@public_router.get("/public/{payment_id}")
async def public_status(payment_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Payment).where(Payment.id == payment_id))
    p = res.scalar_one_or_none()
    if not p: raise HTTPException(404, "Payment not found")

    # ─ HMAC checkout token verification ─
    # Token passed as ?t= query param — generated at payment creation time.
    # Prevents enumeration: even a guessed UUID is useless without the token.
    token = request.query_params.get("t", "")
    created_ts = p.created_at.isoformat() if p.created_at else ""
    from app.core.security import verify_checkout_token
    # Token is optional for backwards compatibility (old payments have no token)
    # Enforce only when token is present but wrong (prevents forged tokens)
    if token and not verify_checkout_token(payment_id, created_ts, token):
        raise HTTPException(403, "Invalid checkout token")

    result = _fmt(p)
    # Expose merchant's branded app/store name (NOT username) so the checkout
    # page can show proper branding. Falls back to generic label.
    app_name = None
    try:
        mres = await db.execute(select(User).where(User.id == p.merchant_id))
        mu = mres.scalar_one_or_none()
        if mu and (mu.app_name or "").strip():
            app_name = mu.app_name.strip()
    except Exception:
        pass
    result["merchant_app_name"] = app_name or ""
    # Flag for checkout UI to show correct context label
    result["is_plan_purchase"] = bool(p.order_id and "plan" in str(p.order_id).lower())
    # Brand colors for checkout customization
    result["brand_primary"] = None
    result["brand_bg"]      = None
    result["brand_text"]    = None
    result["theme"]         = None
    try:
        if mu:
            result["brand_primary"] = getattr(mu, "brand_primary", None)
            result["brand_bg"]      = getattr(mu, "brand_bg",      None)
            result["brand_text"]    = getattr(mu, "brand_text",    None)
            from app.services.themes import theme_from_user
            result["theme"] = theme_from_user(mu)
    except Exception:
        pass
    try:
        from app.core.config import get_settings as _gs
        result["is_testnet"] = bool(_gs().is_testnet)
    except Exception:
        result["is_testnet"] = False
    return result


@public_router.post("/public/{payment_id}/email")
async def submit_email(payment_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    body  = await request.json()
    email = sanitize_str((body.get("email") or "").strip(), 254)
    if not email or "@" not in email:
        raise HTTPException(422, "Valid email required")

    res = await db.execute(select(Payment).where(Payment.id == payment_id))
    p = res.scalar_one_or_none()
    if not p: raise HTTPException(404)
    # ─ HMAC token verification ─
    _tok = request.query_params.get("t", "")
    _ts  = p.created_at.isoformat() if p.created_at else ""
    from app.core.security import verify_checkout_token as _vct
    # Token optional for old payments; enforce only when present but wrong
    if _tok and not _vct(payment_id, _ts, _tok):
        raise HTTPException(403, "Invalid checkout token")

    now = datetime.now(timezone.utc)
    exp = p.expires_at
    if exp and exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
    if exp and exp < now: raise HTTPException(400, "Payment expired")
    if p.status in (PaymentStatus.confirmed, PaymentStatus.expired):
        raise HTTPException(400, f"Payment is {p.status}")

    p.customer_email     = email
    p.email_collected_at = now
    await db.commit()
    return {"message": "Email saved", "step": "payment"}


@public_router.post("/public/{payment_id}/verify-hash")
async def verify_hash(payment_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    body   = await request.json()
    txhash = sanitize_str((body.get("txhash") or "").strip(), 64)
    if not txhash or len(txhash) < 32:
        raise HTTPException(422, "Valid TX hash required")
    from app.services.payment_monitor import verify_manual_txhash
    return await verify_manual_txhash(payment_id, txhash)


@public_router.post("/public/{payment_id}/extend")
async def extend_expiry(payment_id: str, request: Request, db: AsyncSession = Depends(get_db)):

    from datetime import timedelta

    res = await db.execute(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Payment not found")

    # ─ HMAC token verification ─
    _tok = request.query_params.get("t", "")
    _ts  = p.created_at.isoformat() if p.created_at else ""
    from app.core.security import verify_checkout_token as _vct
    # Token optional for old payments; enforce only when present but wrong
    if _tok and not _vct(payment_id, _ts, _tok):
        raise HTTPException(403, "Invalid checkout token")

    if p.status not in (PaymentStatus.pending, PaymentStatus.confirming):
        raise HTTPException(400, f"Cannot extend payment is {p.status}")

    MAX_EXTENSIONS = 3
    current_extensions = p.extend_count or 0

    if current_extensions >= MAX_EXTENSIONS:
        raise HTTPException(400, f"Maximum {MAX_EXTENSIONS} extensions allowed per payment")

    now = datetime.now(timezone.utc)
    expires = p.expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)

    base = max(expires, now) if expires else now
    p.expires_at   = base + timedelta(minutes=10)
    p.extend_count = current_extensions + 1
    db.add(p)
    await db.commit()

    extensions_left = MAX_EXTENSIONS - p.extend_count
    return {
        "extended":         True,
        "new_expires_at":   p.expires_at.isoformat(),
        "extensions_used":  p.extend_count,
        "extensions_left":  extensions_left,
        "message":          f"+10 minutes added. {extensions_left} extension(s) remaining.",
    }


@public_router.get("/public/{payment_id}/receipt")
async def get_receipt(payment_id: str, request: Request, db: AsyncSession = Depends(get_db)):

    res = await db.execute(select(Payment).where(Payment.id == payment_id))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Payment not found")

    # ─ HMAC token verification ─
    _tok = request.query_params.get("t", "")
    _ts  = p.created_at.isoformat() if p.created_at else ""
    from app.core.security import verify_checkout_token as _vct
    # Token optional for old payments; enforce only when present but wrong
    if _tok and not _vct(payment_id, _ts, _tok):
        raise HTTPException(403, "Invalid checkout token")

    if p.status != PaymentStatus.confirmed:
        raise HTTPException(400, "Receipt only available for confirmed payments")

    from app.core.config import get_settings as _gs
    s = _gs()
    is_testnet = s.is_testnet
    explorer_url = (
        f"https://testexplorer.firo.org/tx/{p.txid}" if (p.txid and is_testnet)
        else f"https://explorer.firo.org/tx/{p.txid}" if p.txid
        else None
    )

    return {
        "payment_id":       p.id,
        "order_id":         p.order_id,
        "order_description":p.order_description,
        "amount_firo":      p.amount_firo,
        "amount_received":  p.amount_received,
        "platform_fee":     p.platform_fee_firo,
        "merchant_net":     p.merchant_net_firo,
        "txid":             p.txid,
        "confirmations":    p.confirmations,
        "confirmed_at":     p.confirmed_at.isoformat() if p.confirmed_at else None,
        "customer_email":   p.customer_email,
        "explorer_url":     explorer_url,
        "is_testnet":       is_testnet,
        "network":          "testnet" if is_testnet else "mainnet",
    }



@public_router.get("/public/{payment_id}/qr")
async def get_payment_qr(payment_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Generate QR code for payment - compatible with Firo wallets."""
    res = await db.execute(select(Payment).where(Payment.id == payment_id))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Payment not found")

    # ─ HMAC token verification ─
    _tok = request.query_params.get("t", "")
    _ts  = p.created_at.isoformat() if p.created_at else ""
    from app.core.security import verify_checkout_token as _vct
    # Token optional for old payments; enforce only when present but wrong
    if _tok and not _vct(payment_id, _ts, _tok):
        raise HTTPException(403, "Invalid checkout token")

    if p.status in (PaymentStatus.confirmed, PaymentStatus.expired, PaymentStatus.cancelled):
        raise HTTPException(400, f"QR code not available - payment is {p.status}")

    # Build Firo URI (BIP-21 style): firo:ADDRESS?amount=AMOUNT&label=LABEL
    # Format: firo:<address>?amount=<amount>&label=<description>
    import urllib.parse
    
    address = p.receiving_address
    amount = f"{p.amount_firo:.8f}"
    
    # Build URI
    params = {"amount": amount}
    if p.order_description:
        params["label"] = p.order_description[:50]
    if p.order_id:
        params["message"] = f"Order: {p.order_id[:30]}"
    
    query = urllib.parse.urlencode(params)
    firo_uri = f"firo:{address}?{query}"
    
    # Generate QR code as base64 PNG
    import qrcode
    import qrcode.constants
    from io import BytesIO
    import base64
    
    qr = qrcode.QRCode(
        version=None,  # Auto-size
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(firo_uri)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    qr_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    
    return {
        "qr_data": f"data:image/png;base64,{qr_base64}",
        "firo_uri": firo_uri,
        "address": address,
        "amount": p.amount_firo,
        "payment_id": payment_id,
    }



@public_router.post("/public/{payment_id}/cancel")
async def cancel_payment(payment_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Cancel a pending/active payment. Returns redirect URL."""
    # Rate limit cancellations
    from app.core.rate_limit import rate_limit_check
    await rate_limit_check(request, max_requests=5, window_seconds=60, key_prefix="cancel")
    
    res = await db.execute(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Payment not found")

    # ─ HMAC token verification ─
    _tok = request.query_params.get("t", "")
    _ts  = p.created_at.isoformat() if p.created_at else ""
    from app.core.security import verify_checkout_token as _vct
    # Token optional for old payments; enforce only when present but wrong
    if _tok and not _vct(payment_id, _ts, _tok):
        raise HTTPException(403, "Invalid checkout token")

    # Only allow cancellation of pending or confirming payments (before full confirmation)
    if p.status not in (PaymentStatus.pending, PaymentStatus.confirming):
        raise HTTPException(400, f"Cannot cancel payment - status is {p.status}")

    now = datetime.now(timezone.utc)
    p.status = PaymentStatus.cancelled
    db.add(p)

    # If this is a plan purchase, also cancel the linked PlanOrder
    if p.metadata_json:
        try:
            import json as _json
            from app.models.models import PlanOrder
            meta = _json.loads(p.metadata_json)
            if meta.get("plan_purchase"):
                plan_order_id = meta.get("plan_order_id")
                if plan_order_id:
                    res_order = await db.execute(
                        select(PlanOrder).where(PlanOrder.id == plan_order_id)
                    )
                    linked_order = res_order.scalar_one_or_none()
                    if linked_order and linked_order.status in (
                        PaymentStatus.pending, PaymentStatus.confirming
                    ):
                        linked_order.status = PaymentStatus.cancelled
                        db.add(linked_order)
                else:
                    # Fallback: cancel any active PlanOrder for this merchant
                    from app.models.models import PlanOrder
                    res_order = await db.execute(
                        select(PlanOrder).where(
                            PlanOrder.merchant_id == p.merchant_id,
                            PlanOrder.status.in_([PaymentStatus.pending, PaymentStatus.confirming]),
                        ).order_by(PlanOrder.created_at.desc()).limit(1)
                    )
                    fallback_order = res_order.scalar_one_or_none()
                    if fallback_order:
                        fallback_order.status = PaymentStatus.cancelled
                        db.add(fallback_order)
        except Exception:
            pass

    logger.info(f"Payment {payment_id[:8]} cancelled by user from IP {request.client.host if request.client else 'unknown'}")

    # Commit first so the cancelled status is persisted
    await db.commit()

    # Fire cancellation webhook after commit (best-effort, non-blocking)
    try:
        from app.services.webhook import fire_cancellation_webhook
        await fire_cancellation_webhook(db, p, now)
    except Exception as e:
        logger.warning(f"Cancellation webhook failed for {payment_id[:8]}: {e}")

    # Build redirect URL after cancellation.
    # Rules:
    #   - Plan purchase                      → /dashboard  (never redirect to webhook URL)
    #   - Payment link (has link slug)       → /pay/{slug}?cancelled=1
    #   - Regular + cancel_url set           → cancel_url?status=cancelled&payment_id=
    #   - Regular + no cancel_url            → /  (webhook already fired above — never use webhook_url as browser redirect)
    # NOTE: fire_cancellation_webhook fires unconditionally above — all paths get it.
    is_plan = False
    if p.metadata_json:
        try:
            import json as _json
            _meta = _json.loads(p.metadata_json)
            is_plan = bool(_meta.get("plan_purchase"))
        except Exception:
            pass

    if is_plan:
        # Onion-aware: if the request came via a .onion host, keep the user
        # on the hidden service — never force them to the clearnet dashboard URL.
        _req_host = (
            request.headers.get("x-forwarded-host", "")
            or request.headers.get("host", "")
        )
        _is_onion = (
            ".onion" in _req_host
            or request.headers.get("x-onion-request", "").lower() == "true"
        )
        if _is_onion:
            # Stay on the .onion domain — use ONION_URL if configured,
            # otherwise fall back to a relative path (stays on same origin).
            if settings.ONION_URL:
                redirect_url = settings.ONION_URL.rstrip("/") + "/dashboard"
            else:
                redirect_url = "/dashboard"
        else:
            redirect_url = settings.dashboard_base_url
    else:
        raw_cancel = (p.cancel_url or "").strip()
        # Check if payment came from a payment link
        link_slug = None
        if p.metadata_json:
            try:
                import json as _json2
                _meta2 = _json2.loads(p.metadata_json)
                link_slug = _meta2.get("slug")
            except Exception:
                pass

        if link_slug:
            # Payment came from a payment link — increment cancel_count
            try:
                from app.models.models import PaymentLink as _PL
                res_lnk = await db.execute(
                    select(_PL).where(_PL.slug == link_slug)
                )
                lnk = res_lnk.scalar_one_or_none()
                if lnk:
                    lnk.cancel_count = (lnk.cancel_count or 0) + 1
                    db.add(lnk)
                    await db.commit()
            except Exception:
                pass
            # Send buyer back to the payment link page
            redirect_url = f"{settings.BASE_URL.rstrip('/')}/pay/{link_slug}?cancelled=1"

        elif raw_cancel:
            # Merchant set a cancel_url — use it (API or normal payment)
            sep = "&" if "?" in raw_cancel else "?"
            redirect_url = f"{raw_cancel}{sep}status=cancelled&payment_id={p.id}"

        else:
            # No cancel_url and not a payment link.
            # webhook_url is a server endpoint, NOT a browser redirect target —
            # never send the buyer there. The webhook already fired above.
            redirect_url = "/"

    return {
        "cancelled": True,
        "redirect_url": redirect_url,
        "message": "Payment cancelled"
    }
