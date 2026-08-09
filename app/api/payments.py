import json
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, Header, Response
from app.core.rate_limit import rate_limit_moderate
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.core.database import get_db
from app.core.security import verify_access_token
from app.core.config import get_settings
from app.core.validators import validate_amount, validate_url, sanitize_str
from app.models.models import Payment, PaymentStatus, User, PaymentAuditLog, PaymentAuditEvent

settings      = get_settings()
router        = APIRouter(prefix="/api/payments", tags=["payments"])
public_router = APIRouter(prefix="/api/payments", tags=["public"])


def _checkout_token(request: Request) -> str:
    return request.headers.get("x-checkout-token") or request.query_params.get("t", "")


async def get_merchant(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_api_key: str | None = Header(default=None),
) -> User:
    if x_api_key:
        from app.api.api_keys import get_merchant_by_api_key_full
        result = await get_merchant_by_api_key_full(x_api_key, db)
        if result:
            user, api_key_row = result
            if user and user.is_active:
                # Store the key row on request state so permission deps can read it
                request.state.api_key_row = api_key_row
                return user

    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token: token = request.cookies.get("access_token", "")
    uid = verify_access_token(token)
    if uid:
        res = await db.execute(select(User).where(User.id == uid))
        u = res.scalar_one_or_none()
        if u and u.is_active: return u

    raise HTTPException(401, "missing credentials")


def require_api_permission(permission: str):
    async def _check(request: Request, db: AsyncSession = Depends(get_db)):
        api_key_row = getattr(request.state, "api_key_row", None)
        if api_key_row is None:
            return
        from app.core.api_permissions import check_api_permission
        await check_api_permission(api_key_row, permission, db)
    return _check


class CreatePaymentIn(BaseModel):
    amount_firo:            float
    order_id:               str | None = None
    order_description:      str | None = None
    customer_email:         str | None = None
    success_url:            str | None = None
    cancel_url:             str | None = None
    required_confirmations: int | None = None
    timeout_minutes:        int = 20
    collect_email:          bool = True
    metadata:               dict | None = None


@router.post("/create", status_code=201, dependencies=[Depends(rate_limit_moderate), Depends(require_api_permission("create_invoice"))])
async def create_payment(
    body: CreatePaymentIn,
    request: Request,
    merchant: User = Depends(get_merchant),
    db: AsyncSession = Depends(get_db),
):
    from app.services.firo_rpc import node_is_online
    if not await node_is_online():
        raise HTTPException(503, "Payment processor is under maintenance. Please try again shortly.")

    try:
        body.amount_firo       = validate_amount(body.amount_firo)
        body.order_id          = sanitize_str(body.order_id, 256)
        body.order_description = sanitize_str(body.order_description, 512)
        body.customer_email    = sanitize_str(body.customer_email, 254)
        body.success_url       = validate_url(body.success_url, "success_url")
        body.cancel_url        = validate_url(body.cancel_url, "cancel_url")
    except ValueError as e:
        raise HTTPException(422, str(e))

    # Idempotency: never create a duplicate for the same order_id. If one
    # already exists for this merchant (pending/confirming/confirmed), return
    # it so retries from the merchant's store don't double-charge.
    if body.order_id:
        from app.models.models import PaymentStatus as _PS
        dup_res = await db.execute(
            select(Payment).where(
                Payment.merchant_id == merchant.id,
                Payment.order_id == body.order_id,
                Payment.status.in_([_PS.pending, _PS.confirming, _PS.confirmed]),
            ).order_by(Payment.created_at.desc()).limit(1)
        )
        dup = dup_res.scalar_one_or_none()
        if dup:
            _host = request.headers.get("host", "")
            _src = _host or request.headers.get("Origin") or ""
            _base = settings.get_checkout_base_url(_src)
            from app.core.security import generate_checkout_token as _gct_dup
            _tok_dup = _gct_dup(str(dup.id), dup.created_at.isoformat() if dup.created_at else "")
            return {
                "payment_id":        dup.id,
                "checkout_url":      f"{_base}/invoice/{dup.id}?t={_tok_dup}",
                "receiving_address": dup.receiving_address,
                "address_type":      dup.address_type,
                "amount_firo":       dup.amount_firo,
                "status":            dup.status.value if hasattr(dup.status, "value") else dup.status,
                "expires_at":        dup.expires_at.isoformat() if dup.expires_at else None,
                "idempotent":        True,
            }

    if body.required_confirmations is None:
        from app.core.payment_policy import resolve_required_confirmations
        body.required_confirmations = await resolve_required_confirmations(db, merchant)

    payment = Payment(
        merchant_id=merchant.id,
        receiving_address="",
        amount_firo=body.amount_firo,
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
        address_type="spark",
    )
    db.add(payment)
    await db.flush()

    from app.api.spark_connect_helpers import get_next_spark_address
    address = await get_next_spark_address(db, merchant.id, payment)
    payment.receiving_address = address

    db.add(PaymentAuditLog(
        payment_id=payment.id, merchant_id=merchant.id,
        event=PaymentAuditEvent.payment_created,
        amount_firo=body.amount_firo,
        detail=f"addr={address[:20]}…",
    ))

    _host   = request.headers.get("host", "")
    _origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    _src    = _host or _origin
    _base   = settings.get_checkout_base_url(_src)
    # HMAC token for this payment protects public endpoints from enumeration
    from app.core.security import generate_checkout_token as _gct
    _tok = _gct(str(payment.id), payment.created_at.isoformat() if payment.created_at else "")
    return {
        "payment_id":             payment.id,
        "checkout_url":           f"{_base}/invoice/{payment.id}?t={_tok}",
        "receiving_address":      address,
        "address_type":           payment.address_type,
        "amount_firo":            body.amount_firo,
        "order_id":               body.order_id,
        "expires_at":             payment.expires_at.isoformat(),
        "required_confirmations": body.required_confirmations,
        "status":                 payment.status,
    }


@router.get("/", dependencies=[Depends(require_api_permission("read_invoice"))])
async def list_payments(
    response: Response,
    merchant: User = Depends(get_merchant),
    db: AsyncSession = Depends(get_db),
    limit: int = 50, offset: int = 0, status: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
    search: str | None = None,
):
    base = select(Payment).where(Payment.merchant_id == merchant.id)
    if status:
        base = base.where(Payment.status == status)
    if date_from:
        try:
            base = base.where(Payment.created_at >= _parse_date(date_from))
        except Exception:
            raise HTTPException(422, "invalid date_from")
    if date_to:
        try:
            base = base.where(Payment.created_at <= _parse_date(date_to, end_of_day=True))
        except Exception:
            raise HTTPException(422, "invalid date_to")
    if search:
        s = f"%{search.strip()}%"
        base = base.where(
            (Payment.id.like(s))
            | (Payment.order_id.like(s))
            | (Payment.customer_email.like(s))
            | (Payment.txid.like(s))
        )

    # Total matching row count (same filters, no limit/offset), exposed via a
    # response header rather than the body — this endpoint's body is a bare
    # JSON array and is a documented public API, so changing it to
    # {items, total} would break every existing integration. A header is
    # additive and non-breaking.
    count_res = await db.execute(select(func.count()).select_from(base.subquery()))
    response.headers["X-Total-Count"] = str(count_res.scalar() or 0)

    q = base.order_by(Payment.created_at.desc()).limit(min(limit, 200)).offset(offset)
    res = await db.execute(q)
    # This list is always scoped to the authenticated merchant's own payments
    # (merchant_id == merchant.id above), so full=True (email/payer_note) is
    # not a cross-merchant exposure risk.
    return [_fmt(p, full=True) for p in res.scalars().all()]


@router.get("/search", dependencies=[Depends(require_api_permission("read_invoice"))])
async def search_payments(
    q: str,
    merchant: User = Depends(get_merchant),
    db: AsyncSession = Depends(get_db),
):
    """Fast search endpoint for the dashboard search modal. Returns max 10 results."""
    term = q.strip()
    if len(term) < 2:
        return []
    s = f"%{term}%"
    stmt = (
        select(Payment)
        .where(Payment.merchant_id == merchant.id)
        .where(
            (Payment.id.like(s))
            | (Payment.txid.like(s))
            | (Payment.order_id.like(s))
        )
        .order_by(Payment.created_at.desc())
        .limit(10)
    )
    res = await db.execute(stmt)
    results = []
    for p in res.scalars().all():
        status = p.status.value if hasattr(p.status, "value") else str(p.status)
        match_on = "id"
        if term.lower() in (p.txid or "").lower():
            match_on = "txid"
        elif term.lower() in (p.order_id or "").lower():
            match_on = "order"
        results.append({
            "payment_id":     p.id,
            "txid":           p.txid,
            "order_id":       p.order_id,
            "amount_firo":    p.amount_firo,
            "status":         status,
            "created_at":     p.created_at.isoformat() if p.created_at else None,
            "match_on":       match_on,
        })
    return results


def _parse_date(s: str, end_of_day: bool = False):
    """Parse YYYY-MM-DD or full ISO datetime into a tz-aware datetime."""
    from datetime import datetime as _dt, timezone as _tz
    s = s.strip()
    try:
        d = _dt.fromisoformat(s)
    except ValueError:
        d = _dt.strptime(s, "%Y-%m-%d")
    if d.tzinfo is None:
        d = d.replace(tzinfo=_tz.utc)
    if end_of_day and len(s) <= 10:
        d = d.replace(hour=23, minute=59, second=59)
    return d


@router.get("/export", dependencies=[Depends(require_api_permission("read_invoice"))])
async def export_payments_csv(
    merchant: User = Depends(get_merchant),
    db: AsyncSession = Depends(get_db),
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """Export payments as CSV for accounting. Honors the same filters as list."""
    import csv, io
    q = select(Payment).where(Payment.merchant_id == merchant.id)
    if status:
        q = q.where(Payment.status == status)
    if date_from:
        try: q = q.where(Payment.created_at >= _parse_date(date_from))
        except Exception: raise HTTPException(422, "invalid date_from")
    if date_to:
        try: q = q.where(Payment.created_at <= _parse_date(date_to, end_of_day=True))
        except Exception: raise HTTPException(422, "invalid date_to")
    q = q.order_by(Payment.created_at.desc()).limit(10000)
    res = await db.execute(q)
    rows = res.scalars().all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "payment_id", "created_at", "confirmed_at", "status",
        "amount_firo", "amount_received",
        "txid", "confirmations", "order_id", "order_description",
        "customer_email", "vendor_id",
    ])
    for p in rows:
        meta = {}
        if p.metadata_json:
            try: meta = json.loads(p.metadata_json)
            except Exception: meta = {}
        st = p.status.value if hasattr(p.status, "value") else p.status
        w.writerow([
            p.id,
            p.created_at.isoformat() if p.created_at else "",
            p.confirmed_at.isoformat() if p.confirmed_at else "",
            st,
            p.amount_firo, p.amount_received or "",
            p.txid or "", p.confirmations or 0,
            p.order_id or "", (p.order_description or "").replace("\n", " "),
            p.customer_email or "",
            meta.get("vendor_id", ""),
        ])
    from fastapi.responses import Response
    from datetime import datetime, timezone
    csv_bytes = buf.getvalue().encode("utf-8-sig")  # BOM for Excel compatibility
    fname = f"payments_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/summary", dependencies=[Depends(require_api_permission("read_analytics"))])
async def payments_summary(
    merchant: User = Depends(get_merchant),
    db: AsyncSession = Depends(get_db),
    date_from: str | None = None,
    date_to: str | None = None,
):
    """Accounting summary: totals over a period (confirmed payments only)."""
    from sqlalchemy import func as _f
    q = select(
        _f.count(Payment.id),
        _f.coalesce(_f.sum(Payment.amount_firo), 0.0),
        _f.coalesce(_f.sum(Payment.amount_received), 0.0),
    ).where(
        Payment.merchant_id == merchant.id,
        Payment.status == PaymentStatus.confirmed,
    )
    if date_from:
        try: q = q.where(Payment.created_at >= _parse_date(date_from))
        except Exception: raise HTTPException(422, "invalid date_from")
    if date_to:
        try: q = q.where(Payment.created_at <= _parse_date(date_to, end_of_day=True))
        except Exception: raise HTTPException(422, "invalid date_to")
    row = (await db.execute(q)).one()

    sq = select(Payment.status, _f.count(Payment.id)).where(
        Payment.merchant_id == merchant.id
    ).group_by(Payment.status)
    by_status = {}
    for st, cnt in (await db.execute(sq)).all():
        key = st.value if hasattr(st, "value") else str(st)
        by_status[key] = cnt

    return {
        "confirmed_count":      row[0],
        "total_gross_sales":    round(row[1], 8),
        "total_received":       round(row[2], 8),
        "by_status":            by_status,
    }


@router.get("/by-vendor", dependencies=[Depends(require_api_permission("read_analytics"))])
async def payments_by_vendor(
    merchant: User = Depends(get_merchant),
    db: AsyncSession = Depends(get_db),
    date_from: str | None = None,
    date_to: str | None = None,
):
    """Group confirmed payments by vendor_id (from metadata) for multi-vendor stores."""
    q = select(Payment).where(
        Payment.merchant_id == merchant.id,
        Payment.status == PaymentStatus.confirmed,
    )
    if date_from:
        try: q = q.where(Payment.created_at >= _parse_date(date_from))
        except Exception: raise HTTPException(422, "invalid date_from")
    if date_to:
        try: q = q.where(Payment.created_at <= _parse_date(date_to, end_of_day=True))
        except Exception: raise HTTPException(422, "invalid date_to")
    res = await db.execute(q)
    vendors: dict = {}
    for p in res.scalars().all():
        vid = "unassigned"
        if p.metadata_json:
            try:
                vid = json.loads(p.metadata_json).get("vendor_id") or "unassigned"
            except Exception:
                vid = "unassigned"
        v = vendors.setdefault(str(vid), {"vendor_id": str(vid), "count": 0, "total_gross_sales": 0.0, "total_received": 0.0})
        v["count"] += 1
        v["total_gross_sales"] += (p.amount_firo or 0.0)
        v["total_received"] += (p.amount_received or 0.0)
    for v in vendors.values():
        v["total_gross_sales"] = round(v["total_gross_sales"], 8)
        v["total_received"] = round(v["total_received"], 8)
    return {"vendors": list(vendors.values())}


@router.get("/{payment_id}", dependencies=[Depends(require_api_permission("read_invoice"))])
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

    from app.core.payment_policy import display_status as _display_status
    disp_status = _display_status(p, status=status)

    from app.core.security import generate_checkout_token as _gct_fmt
    d = {
        "payment_id":             p.id,
        "status":                 status,
        "display_status":         disp_status,
        "amount_firo":            p.amount_firo,
        "amount_received":        p.amount_received,
        "txid":                   p.txid,
        "confirmations":          p.confirmations,
        "required_confirmations": p.required_confirmations,
        "receiving_address":      p.receiving_address,
        "address_type":           p.address_type or "spark",
        "order_id":               p.order_id,
        "order_description":      p.order_description,
        "collect_email":          p.collect_email,
        # Boolean flag only - never expose the actual email on the public endpoint
        "email_collected":        bool(p.customer_email),
        "cancel_url":             p.cancel_url,
        "success_url":            p.success_url,
        "created_at":             p.created_at.isoformat(),
        "expires_at":             p.expires_at.isoformat() if p.expires_at else None,
        "confirmed_at":           p.confirmed_at.isoformat() if p.confirmed_at else None,
        "amount_usd":             getattr(p, "price_usd", None) or None,
        # Lets the merchant dashboard deep-link into the public checkout page,
        # which requires a valid token on every request.
        "checkout_token":         _gct_fmt(str(p.id), p.created_at.isoformat() if p.created_at else ""),
    }
    if full:
        payer_note = None
        if p.metadata_json:
            try:
                import json as _json_note
                payer_note = _json_note.loads(p.metadata_json).get("note") or None
            except Exception:
                pass
        d.update({
            "customer_email": p.customer_email,
            "vout":           p.vout,
            "webhook_sent":   p.webhook_sent,
            # Payer-supplied free text, merchant-only, never exposed on the
            # public checkout endpoint. Render with textContent/escHtml only
            # this is untrusted input from the payer.
            "payer_note":     payer_note,
        })
    return d


@public_router.get("/public/{payment_id}")
async def public_status(payment_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Payment).where(Payment.id == payment_id))
    p = res.scalar_one_or_none()
    if not p: raise HTTPException(404, "Payment not found")

    # Token passed as ?t= query param, generated at payment creation time.
    # Mandatory: prevents enumeration/hijack via a guessed or leaked UUID
    # with no accompanying token.
    token = _checkout_token(request)
    created_ts = p.created_at.isoformat() if p.created_at else ""
    from app.core.security import verify_checkout_token
    if not token or not verify_checkout_token(payment_id, created_ts, token):
        raise HTTPException(403, "Invalid checkout token")

    result = _fmt(p)
    result["is_plan_purchase"] = bool(p.order_id and "plan" in str(p.order_id).lower())

    # Expose merchant's branded app/store name (NOT username) so the checkout
    # page can show proper branding. Falls back to generic label.
    # Plan purchases are billed BY FiroGate (the operator); the buyer here is
    # the merchant purchasing a plan, so their own store branding must NOT be
    # shown - every checkout layout (stripe or v2) should read "FiroGate".
    app_name = None
    app_logo = None
    mu = None
    try:
        mres = await db.execute(select(User).where(User.id == p.merchant_id))
        mu = mres.scalar_one_or_none()
    except Exception:
        pass
    if result["is_plan_purchase"]:
        app_name = "FiroGate"
    else:
        if mu and (mu.app_name or "").strip():
            app_name = mu.app_name.strip()
        if mu and (mu.app_logo or "").strip():
            app_logo = mu.app_logo.strip()
    result["merchant_app_name"] = app_name or ""
    result["merchant_logo"]     = app_logo or ""
    result["show_market_price"] = bool(mu and mu.show_market_price)
    result["brand_primary"] = None
    result["brand_bg"]      = None
    result["brand_text"]    = None
    result["theme"]         = None
    try:
        theme_user = mu
        # Plan purchases: use the operator's theme, not the buyer's
        if result["is_plan_purchase"]:
            from app.core.config import get_settings as _gs2
            from app.models.models import UserRole as _UR
            _s = _gs2()
            _emails = _s.operator_email_set
            _op_rows = (await db.execute(
                select(User).where(
                    (User.role == _UR.operator) | (User.email.in_(list(_emails)))
                )
            )).scalars().all()
            if _op_rows:
                theme_user = _op_rows[0]
        if theme_user:
            result["brand_primary"] = getattr(theme_user, "brand_primary", None)
            result["brand_bg"]      = getattr(theme_user, "brand_bg",      None)
            result["brand_text"]    = getattr(theme_user, "brand_text",    None)
            from app.services.themes import theme_from_user
            result["theme"] = theme_from_user(theme_user)
    except Exception as _e:
        import logging as _log
        _log.getLogger(__name__).error("theme_from_user failed: %s", _e, exc_info=True)
    try:
        from app.core.config import get_settings as _gs
        result["is_testnet"] = bool(_gs().is_testnet)
    except Exception:
        result["is_testnet"] = False
    # Let the checkout page show a maintenance banner when the node is offline.
    # Uses the cached ping result (15 s TTL) so this doesn't add latency.
    try:
        from app.services.firo_rpc import node_is_online
        result["node_online"] = await node_is_online()
    except Exception:
        result["node_online"] = True  # assume online if we can't check
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
    _tok = _checkout_token(request)
    _ts  = p.created_at.isoformat() if p.created_at else ""
    from app.core.security import verify_checkout_token as _vct
    if not _tok or not _vct(payment_id, _ts, _tok):
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
    # Each call costs a node RPC lookup - cap it even for valid token holders.
    from app.core.rate_limit import rate_limit_check
    await rate_limit_check(request, max_requests=10, window_seconds=60, key_prefix="vhash")
    res = await db.execute(select(Payment).where(Payment.id == payment_id))
    p = res.scalar_one_or_none()
    if not p: raise HTTPException(404)
    _tok = _checkout_token(request)
    _ts  = p.created_at.isoformat() if p.created_at else ""
    from app.core.security import verify_checkout_token as _vct
    if not _tok or not _vct(payment_id, _ts, _tok):
        raise HTTPException(403, "Invalid checkout token")

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

    _tok = _checkout_token(request)
    _ts  = p.created_at.isoformat() if p.created_at else ""
    from app.core.security import verify_checkout_token as _vct
    if not _tok or not _vct(payment_id, _ts, _tok):
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

    _tok = _checkout_token(request)
    _ts  = p.created_at.isoformat() if p.created_at else ""
    from app.core.security import verify_checkout_token as _vct
    if not _tok or not _vct(payment_id, _ts, _tok):
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

    _tok = _checkout_token(request)
    _ts  = p.created_at.isoformat() if p.created_at else ""
    from app.core.security import verify_checkout_token as _vct
    if not _tok or not _vct(payment_id, _ts, _tok):
        raise HTTPException(403, "Invalid checkout token")

    if p.status in (PaymentStatus.confirmed, PaymentStatus.expired, PaymentStatus.cancelled):
        raise HTTPException(400, f"QR code not available - payment is {p.status}")

    # Build Firo URI (BIP-21 style): firo:ADDRESS?amount=AMOUNT&label=LABEL
    import urllib.parse

    address = p.receiving_address
    amount = f"{p.amount_firo:.8f}"

    params = {"amount": amount}
    if p.order_description:
        params["label"] = p.order_description[:50]
    if p.order_id:
        params["message"] = f"Order: {p.order_id[:30]}"
    
    query = urllib.parse.urlencode(params)
    firo_uri = f"firo:{address}?{query}"

    import base64
    from app.services.qr_service import make_payment_qr_png

    qr_base64 = base64.b64encode(make_payment_qr_png(firo_uri)).decode("utf-8")

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
    from app.core.rate_limit import rate_limit_check
    await rate_limit_check(request, max_requests=5, window_seconds=60, key_prefix="cancel")
    
    res = await db.execute(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Payment not found")

    _tok = _checkout_token(request)
    _ts  = p.created_at.isoformat() if p.created_at else ""
    from app.core.security import verify_checkout_token as _vct
    if not _tok or not _vct(payment_id, _ts, _tok):
        raise HTTPException(403, "Invalid checkout token")

    if p.status not in (PaymentStatus.pending, PaymentStatus.confirming):
        raise HTTPException(400, f"Cannot cancel payment - status is {p.status}")

    now = datetime.now(timezone.utc)
    p.status = PaymentStatus.cancelled
    db.add(p)

    logger.info(f"Payment {payment_id[:8]} cancelled by user from IP {request.client.host if request.client else 'unknown'}")

    # Commit first so the cancelled status is persisted before the
    # best-effort webhook fire below.
    await db.commit()

    try:
        from app.services.webhook import fire_cancellation_webhook
        await fire_cancellation_webhook(db, p, now)
    except Exception as e:
        logger.warning(f"Cancellation webhook failed for {payment_id[:8]}: {e}")

    # Redirect precedence:
    #   - Plan purchase                → /dashboard  (never redirect to webhook URL)
    #   - Payment link (has link slug) → /pay/{slug}?cancelled=1
    #   - Regular + cancel_url set     → cancel_url?status=cancelled&payment_id=
    #   - Regular + no cancel_url      → /dashboard
    # fire_cancellation_webhook already fired above, unconditionally, for all paths.
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
        # on the hidden service - never force them to the clearnet dashboard URL.
        _req_host = (
            request.headers.get("x-forwarded-host", "")
            or request.headers.get("host", "")
        )
        _is_onion = (
            ".onion" in _req_host
            or request.headers.get("x-onion-request", "").lower() == "true"
        )
        if _is_onion:
            if settings.ONION_URL:
                redirect_url = settings.ONION_URL.rstrip("/") + "/dashboard?tab=plan&plan_result=cancelled"
            else:
                redirect_url = "/dashboard?tab=plan&plan_result=cancelled"
        else:
            base = settings.dashboard_base_url.rstrip("/")
            sep = "&" if "?" in base else "?"
            redirect_url = f"{base}{sep}tab=plan&plan_result=cancelled"
    else:
        raw_cancel = (p.cancel_url or "").strip()
        link_slug = None
        if p.metadata_json:
            try:
                import json as _json2
                _meta2 = _json2.loads(p.metadata_json)
                link_slug = _meta2.get("slug")
            except Exception:
                pass

        if link_slug:
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
            redirect_url = f"{settings.BASE_URL.rstrip('/')}/pay/{link_slug}?cancelled=1"

        elif raw_cancel:
            sep = "&" if "?" in raw_cancel else "?"
            redirect_url = f"{raw_cancel}{sep}status=cancelled&payment_id={p.id}"

        else:
            # webhook_url is a server endpoint, NOT a browser redirect target -
            # never send the buyer there. The webhook already fired above.
            redirect_url = "/dashboard"

    return {
        "cancelled": True,
        "redirect_url": redirect_url,
        "message": "Payment cancelled"
    }
