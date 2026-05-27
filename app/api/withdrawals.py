from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import verify_access_token
from app.core.config import get_settings
from app.core.validators import validate_amount, validate_address
from app.core.rate_limit import rate_limit_moderate
from app.models.models import User, Withdrawal, WithdrawalStatus, AuditLog

settings = get_settings()
router   = APIRouter(prefix="/api/withdrawals", tags=["withdrawals"])


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = (request.cookies.get("access_token") or
             request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
    uid = verify_access_token(token)
    if not uid:
        raise HTTPException(401, "Not authenticated")
    res = await db.execute(select(User).where(User.id == uid))
    u = res.scalar_one_or_none()
    if not u or not u.is_active:
        raise HTTPException(401, "User not found")
    return u


class WithdrawalRequest(BaseModel):
    amount_firo:         float
    destination_address: str
    nonce:               str = ""  # Client nonce for replay protection


def _is_address_trusted(user: User, addr: str) -> bool:
    """Check if an address has been used for a successful withdrawal before."""
    import json as _j
    raw = getattr(user, 'trusted_addresses_json', None)
    if not raw:
        return False
    try:
        return addr in _j.loads(raw)
    except Exception:
        return False


def _mark_address_trusted(user: User, addr: str) -> None:
    """Add an address to the user's trusted list (call after successful withdrawal)."""
    import json as _j
    raw = getattr(user, 'trusted_addresses_json', None)
    try:
        addrs = _j.loads(raw) if raw else []
    except Exception:
        addrs = []
    if addr not in addrs:
        addrs.append(addr)
        # Keep last 50 trusted addresses
        if len(addrs) > 50:
            addrs = addrs[-50:]
        user.trusted_addresses_json = _j.dumps(addrs)


@router.post("/request", status_code=201, dependencies=[Depends(rate_limit_moderate)])
async def request_withdrawal(
    body:     WithdrawalRequest,
    request:  Request,
    merchant: User = Depends(get_current_user),
    db:       AsyncSession = Depends(get_db),
):
    # ─ Nonce replay protection ─
    if body.nonce:
        from app.core.nonce_tracker import is_nonce_used, mark_nonce_used
        if await is_nonce_used(body.nonce):
            raise HTTPException(409, "Duplicate request detected (replay)")
        await mark_nonce_used(body.nonce)

    try:
        amount = validate_amount(body.amount_firo, min_val=settings.MIN_WITHDRAWAL_FIRO)
    except ValueError as e:
        raise HTTPException(422, str(e))

    addr = (body.destination_address or "").strip()
    if not addr:
        raise HTTPException(422, "Destination address is required")
    from app.services.firo_rpc import get_rpc as _get_rpc
    _rpc = _get_rpc()
    if _rpc.is_spark_address(addr):
 
        if len(addr) < 100:
            raise HTTPException(422, "Invalid address")
    else:
        try:
            addr = validate_address(addr)
        except ValueError as e:
            raise HTTPException(422, str(e))

    res = await db.execute(
        select(User).where(User.id == merchant.id).with_for_update()
    )
    merchant = res.scalar_one_or_none()
    if not merchant:
        raise HTTPException(404, "Merchant not found")

    available = round(merchant.balance_firo or 0, 8)
    if available < amount:
        raise HTTPException(400, f"Insufficient balance. Available: {available:.8f} FIRO")

    from app.core.fees import calc_fee as _cf
    _fee = _cf(amount)
    if amount - _fee <= 0.0001:
        raise HTTPException(400, f"Amount too small after fee ({_fee:.4f} FIRO)")

    res2 = await db.execute(
        select(Withdrawal).where(
            Withdrawal.merchant_id == merchant.id,
            Withdrawal.status.in_([
                WithdrawalStatus.pending,
                WithdrawalStatus.processing,
                WithdrawalStatus.approved,
            ]),
        ).limit(1)
    )
    if res2.scalars().first():
        raise HTTPException(400, "Withdrawal in progress. Wait until complete.")

    import json as _wjson
    _wl_raw = getattr(merchant, 'withdrawal_whitelist_json', None)
    if _wl_raw:
        try:
            _allowed = _wjson.loads(_wl_raw)
            if _allowed and addr not in _allowed:
                raise HTTPException(
                    403,
                    "Address not in your whitelist. "
                    "Add it in → Security → Whitelist before withdrawing."
                )
        except HTTPException:
            raise
        except Exception:
            pass

    try:
        from app.services.firo_rpc import get_rpc
        rpc = get_rpc()
        if rpc.is_spark_address(addr):

            if not settings.SPARK_ENABLED:
                raise HTTPException(422, "Spark (sm/st) withdrawals are not enabled. Use a transparent address.")
            if not await rpc.validate_spark_address(addr):
                raise HTTPException(422, "Invalid Spark address")
        else:
            if not await rpc.validate_address_rpc(addr):
                raise HTTPException(422, "Invalid t-address")
    except HTTPException:
        raise
    except Exception:
        pass

    ip = request.client.host if request.client else None

    # ─ Untrusted address detection ──
    # If address has never been used for a successful withdrawal by this user,
    # flag it as untrusted. This elevates risk scoring so the withdrawal
    # service may require 2FA even for smaller amounts.
    _addr_trusted = _is_address_trusted(merchant, addr)
    _extra_risk = 0
    if not _addr_trusted:
        _extra_risk = 30  # New address: +30 risk points

    # Check if IP changed since last withdrawal (session anomaly)
    if merchant.last_withdrawal_at and ip:
        # Get last withdrawal IP from most recent completed/sent withdrawal
        _last_wd_res = await db.execute(
            select(Withdrawal).where(
                Withdrawal.merchant_id == merchant.id,
                Withdrawal.status.in_([WithdrawalStatus.completed, WithdrawalStatus.sent]),
            ).order_by(Withdrawal.created_at.desc()).limit(1)
        )
        _last_wd = _last_wd_res.scalar_one_or_none()
        if _last_wd and _last_wd.ip_address and _last_wd.ip_address != ip:
            _extra_risk += 15  # Different IP: +15 risk points

    from app.services.withdrawal_service import create_withdrawal_request, WithdrawalSoftError
    try:
        w = await create_withdrawal_request(
            db=db,
            merchant=merchant,
            amount=amount,
            addr=addr,
            ip=ip,
            fee_pct=0.0,
            extra_risk=_extra_risk,
            addr_trusted=_addr_trusted,
        )
    except WithdrawalSoftError as se:
        # Known soft failures we want to surface with structured metadata so
        # the UI can react (e.g. nudge the user to enable 2FA).
        if str(se) == "2fa_required_to_enable":
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "2fa_required",
                    "message": (
                        "This amount requires two-factor authentication. "
                        "Enable 2FA from Security → 2FA and try again."
                    ),
                    "requires_2fa_enable": True,
                },
            )
        raise HTTPException(400, str(se))
    except Exception as e:
        raise HTTPException(400, str(e))

    delay = settings.WITHDRAWAL_DELAY_SECONDS
    response = {
        "withdrawal_id":    w.id,
        "amount_requested": w.amount_requested,
        "fee_pct":          w.withdrawal_fee_pct,
        "fee_firo":         w.withdrawal_fee_firo,
        "amount_net":       w.amount_net,
        "destination":      addr,
        "status":           w.status,
        "tier":             w.tier,
        "risk_score":       w.risk_score,
        "process_after":    w.process_after.isoformat() if w.process_after else None,
        "withdrawal_type":  w.tier,
    }

    if w.tier == "auto":
        response["message"] = "Withdrawal queued — it will be sent shortly."
    elif w.tier == "soft":
        response["message"] = "Enter your 2FA code to approve this withdrawal."
        response["requires_2fa"] = True
    elif w.tier == "email":
        # Mask the email for the UI: a***@example.com
        e = (merchant.email or "")
        masked = ""
        if "@" in e:
            local, dom = e.split("@", 1)
            masked = (local[:1] + "***" if local else "***") + "@" + dom
        response["message"] = (
            f"A one-time code has been sent to {masked or 'your email'}. "
            "It expires in 5 minutes."
        )
        response["requires_email_code"] = True
        response["email_masked"] = masked
        response["code_expires_at"] = (
            w.email_code_expires_at.isoformat() if w.email_code_expires_at else None
        )
    else:
        response["message"] = "Under review — you'll be notified once processed."
        response["requires_review"] = True

    return response


class TotpVerifyRequest(BaseModel):
    withdrawal_id: str
    totp_code:     str


@router.post("/verify-2fa")
async def verify_withdrawal_2fa(
    body:     TotpVerifyRequest,
    merchant: User = Depends(get_current_user),
    db:       AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(Withdrawal).where(
            Withdrawal.id == body.withdrawal_id,
            Withdrawal.merchant_id == merchant.id,
        )
    )
    w = res.scalar_one_or_none()
    if not w:
        raise HTTPException(404, "Withdrawal not found")

    from app.services.withdrawal_service import verify_withdrawal_totp
    ok, msg = await verify_withdrawal_totp(db, w, merchant, body.totp_code)
    if not ok:
        raise HTTPException(400, msg)

    return {"message": msg, "status": w.status}


class EmailCodeVerifyIn(BaseModel):
    withdrawal_id: str
    code:          str


@router.post("/verify-email-code", dependencies=[Depends(rate_limit_moderate)])
async def verify_withdrawal_email_code(
    body:     EmailCodeVerifyIn,
    merchant: User = Depends(get_current_user),
    db:       AsyncSession = Depends(get_db),
):
    """
    Verify the alphanumeric one-time code emailed to the user for a large
    withdrawal. Enforces expiry, max-5 attempts, and constant-time compare.
    On success the withdrawal is marked pending and the worker will send.
    """
    res = await db.execute(
        select(Withdrawal).where(
            Withdrawal.id == body.withdrawal_id,
            Withdrawal.merchant_id == merchant.id,
        )
    )
    w = res.scalar_one_or_none()
    if not w:
        raise HTTPException(404, "Withdrawal not found")

    from app.services.withdrawal_service import verify_withdrawal_email_code as _verify
    ok, msg = await _verify(db, w, merchant, body.code)
    if not ok:
        # Distinguish lock from plain bad-code so the UI can show a dedicated
        # "withdrawal locked" screen with a Start-Over button.
        locked = (w.status == WithdrawalStatus.locked)
        raise HTTPException(
            400,
            detail={"error": "invalid_code", "message": msg, "locked": locked},
        )
    return {
        "message": msg,
        "status":  w.status,
        "tier":    w.tier,
    }


class EmailCodeResendIn(BaseModel):
    withdrawal_id: str


@router.post("/resend-email-code", dependencies=[Depends(rate_limit_moderate)])
async def resend_withdrawal_email_code(
    body:     EmailCodeResendIn,
    merchant: User = Depends(get_current_user),
    db:       AsyncSession = Depends(get_db),
):
    """
    Issue a new code and invalidate the old one. Rate-limited to once per
    60 seconds at the service layer; rate-limited to moderate global limit
    at the API layer too.
    """
    res = await db.execute(
        select(Withdrawal).where(
            Withdrawal.id == body.withdrawal_id,
            Withdrawal.merchant_id == merchant.id,
        )
    )
    w = res.scalar_one_or_none()
    if not w:
        raise HTTPException(404, "Withdrawal not found")

    from app.services.withdrawal_service import resend_withdrawal_email_code as _resend
    ok, msg = await _resend(db, w, merchant)
    if not ok:
        raise HTTPException(400, msg)
    return {
        "message":         msg,
        "code_expires_at": w.email_code_expires_at.isoformat() if w.email_code_expires_at else None,
    }


class WhitelistUpdateIn(BaseModel):
    addresses: list[str]

@router.get("/whitelist")
async def get_whitelist(
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):

    import json as _j
    raw = getattr(user, 'withdrawal_whitelist_json', None)
    try:
        addresses = _j.loads(raw) if raw else []
    except Exception:
        addresses = []
    return {"addresses": addresses, "enabled": bool(addresses)}


@router.put("/whitelist")
async def update_whitelist(
    body: WhitelistUpdateIn,
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):

    import json as _j
    from app.services.firo_rpc import get_rpc
    from app.core.validators import validate_address

    if len(body.addresses) > 20:
        raise HTTPException(400, "Max 20 addresses allowed")

    rpc       = get_rpc()
    validated = []
    for raw_addr in body.addresses:
        addr = raw_addr.strip()
        if not addr:
            continue
        if rpc.is_spark_address(addr):
            if len(addr) < 100:
                raise HTTPException(422, f"Invalid address: {addr[:30]}")
        else:
            try:
                addr = validate_address(addr)
            except ValueError as e:
                raise HTTPException(422, f"Invalid address '{addr[:20]}': {e}")
        if addr not in validated:
            validated.append(addr)

    # Merge user into current session to ensure changes are tracked
    user = await db.merge(user)
    user.withdrawal_whitelist_json = _j.dumps(validated) if validated else None
    db.add(user)
    await db.flush()
    await db.commit()
    await db.refresh(user)

    # Verify saved correctly
    saved = user.withdrawal_whitelist_json
    try:
        saved_list = _j.loads(saved) if saved else []
    except Exception:
        saved_list = validated

    return {
        "addresses": saved_list,
        "enabled":   bool(saved_list),
        "message": (
            f"Whitelist updated: {len(saved_list)} address(es)."
            if saved_list else
            "Whitelist cleared — any address is now allowed."
        ),
    }


@router.get("/")
async def list_withdrawals(
    merchant: User = Depends(get_current_user),
    db:       AsyncSession = Depends(get_db),
    limit:    int = 20,
):
    res = await db.execute(
        select(Withdrawal)
        .where(Withdrawal.merchant_id == merchant.id)
        .order_by(Withdrawal.created_at.desc())
        .limit(min(limit, 100))
    )
    return [_fmt(w) for w in res.scalars().all()]


@router.get("/{withdrawal_id}")
async def get_withdrawal(
    withdrawal_id: str,
    merchant: User = Depends(get_current_user),
    db:       AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(Withdrawal).where(
            Withdrawal.id == withdrawal_id,
            Withdrawal.merchant_id == merchant.id,
        )
    )
    w = res.scalar_one_or_none()
    if not w:
        raise HTTPException(404, "Withdrawal not found")
    return _fmt(w)


@router.post("/{withdrawal_id}/cancel")
async def cancel_withdrawal(
    withdrawal_id: str,
    request:  Request,
    merchant: User = Depends(get_current_user),
    db:       AsyncSession = Depends(get_db),
):
    res_m = await db.execute(
        select(User).where(User.id == merchant.id).with_for_update()
    )
    merchant = res_m.scalar_one()

    res_w = await db.execute(
        select(Withdrawal).where(
            Withdrawal.id == withdrawal_id,
            Withdrawal.merchant_id == merchant.id,
        ).with_for_update()
    )
    w = res_w.scalar_one_or_none()
    if not w:
        raise HTTPException(404, "Withdrawal not found")

    if w.status != WithdrawalStatus.pending:
        raise HTTPException(
            400,
            f"Cannot cancel withdrawal is '{w.status}'. "
            f"Only pending withdrawals can be cancelled."
        )

    if w.process_after:
        pa = w.process_after
        if pa.tzinfo is None:
            pa = pa.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > pa:
            raise HTTPException(
                400,
                "Cancellation window has passed."
            )

    # Refund
    merchant.balance_firo = round((merchant.balance_firo or 0) + w.amount_requested, 8)
    merchant.balance_withdrawn = round(
        (merchant.balance_withdrawn or 0) - w.amount_requested, 8
    )
    w.status = WithdrawalStatus.cancelled
    db.add(w)
    db.add(merchant)
    db.add(AuditLog(
        user_id=merchant.id,
        action="withdrawal.cancelled",
        entity_id=w.id,
        ip_address=request.client.host if request.client else None,
    ))
    await db.flush()
    await db.commit()
    return {"message": "Withdrawal cancelled."}


def _fmt(w: Withdrawal) -> dict:
    now = datetime.now(timezone.utc)
    pa = w.process_after
    if pa and pa.tzinfo is None:
        pa = pa.replace(tzinfo=timezone.utc)
    is_testnet = settings.is_testnet

    seconds_left = max(0, int((pa - now).total_seconds())) if pa else 0
    can_cancel = (
        w.status == WithdrawalStatus.pending and
        pa is not None and
        now < pa
    )

    return {
        "id":               w.id,
        "amount_requested": w.amount_requested,
        "fee_pct":          w.withdrawal_fee_pct,
        "fee_firo":         w.withdrawal_fee_firo,
        "amount_net":       w.amount_net,
        "destination":      w.destination_address,
        "status":           w.status,
        "tier":             w.tier,
        "risk_score":       w.risk_score,
        "requires_2fa":     (w.tier == "soft" and not w.totp_verified and
                             w.status == WithdrawalStatus.pending),
        "sent_txid":        w.sent_txid,
        "admin_note":       w.admin_note,
        "rejection_reason": w.rejection_reason,
        "processing_error": w.processing_error,
        "can_cancel":       can_cancel,
        "seconds_left":     seconds_left,
        "process_after":    pa.isoformat() if pa else None,
        "withdrawal_type":  getattr(w, 'withdrawal_type', 'transparent'),
        "spark_op_status":  getattr(w, 'spark_op_status', None),
        "created_at":       w.created_at.isoformat(),
        "sent_at":          w.sent_at.isoformat() if w.sent_at else None,
        "explorer_url":     (
            f"https://testexplorer.firo.org/tx/{w.sent_txid}" if (w.sent_txid and is_testnet)
            else f"https://explorer.firo.org/tx/{w.sent_txid}" if w.sent_txid
            else None
        ),
    }