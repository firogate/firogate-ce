from fastapi import APIRouter, Depends, HTTPException, Request
from app.core.rate_limit import rate_limit_auth, rate_limit_moderate
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import get_settings

settings = get_settings()
from app.core.security import verify_access_token, generate_api_key, generate_webhook_secret, encrypt_field, decrypt_field, verify_password
from app.core.validators import validate_url, validate_password, sanitize_str
from app.models.models import User, UserRole, Payment, PaymentStatus

router = APIRouter(prefix="/api/users", tags=["users"])


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.cookies.get("access_token") or \
            request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    uid = verify_access_token(token)
    if not uid: raise HTTPException(401)
    res = await db.execute(select(User).where(User.id == uid))
    u = res.scalar_one_or_none()
    if not u or not u.is_active: raise HTTPException(401)

    # Admin-email auto-promotion: if this user's email is listed in the
    # OPERATOR_EMAILS env var but the DB still has them as a merchant, flip
    # the role to admin once. Happens lazily on the next request (e.g.
    # the dashboard's /profile call) so brand new admins don't need a
    # restart or manual DB edit.
    from app.core.config import get_settings
    if u.role != UserRole.admin and get_settings().is_admin_email(u.email):
        u.role = UserRole.admin
        db.add(u)
        await db.commit()
        await db.refresh(u)
    return u


@router.get("/profile")
async def profile(request: Request, user: User = Depends(get_current_user)):
    from app.services.privacy_service import is_onion_request
    
    is_onion = is_onion_request(request)
    
    return {
        "id":               user.id,
        "username":         user.username,
        "email":            user.email,
        "app_name":         user.app_name,
        "app_name_locked":  bool(user.app_name_locked),
        "app_name_change_allowed": bool(user.app_name_change_allowed),
        "plan":             user.plan,
        "requests_total":   user.requests_total,
        "requests_used":    user.requests_used,
        "requests_left":    max(0, (user.requests_total or 0) - (user.requests_used or 0)),
        "balance_firo":     round(user.balance_firo or 0, 8),
        "balance_pending":  round(user.balance_pending or 0, 8),
        "balance_withdrawn": round(user.balance_withdrawn or 0, 8),
        "total_earned":     round(user.total_earned_firo or 0, 8),
        "total_fees_paid":  round(user.total_fees_firo or 0, 8),
        "plan_expires_at":  user.plan_expires_at.isoformat() if user.plan_expires_at else None,
        "api_key":          user.api_key,
        "webhook_url":      user.webhook_url,
        "has_webhook_secret": bool(user.webhook_secret_enc),
        "totp_enabled":     bool(user.totp_enabled),
        "role":               user.role.value if hasattr(user.role, "value") else str(user.role),
        "is_admin":           user.role == UserRole.admin,
        # Privacy mode info
        "privacy_mode":       user.privacy_mode,
        "created_via_onion":  user.created_via_onion,
        "is_onion_session":   is_onion,
        # ─ Network info (auto-detected from RPC port) ─
        "is_testnet":         settings.is_testnet,
        "network":            settings.network_name,
        "network_label":      settings.network_label,
        "network_warning":    settings.network_warning,
    }


@router.get("/webhook-secret")
async def get_webhook_secret(user: User = Depends(get_current_user)):
    if not user.webhook_secret_enc:
        return {"secret": None}
    try:
        return {"secret": decrypt_field(user.webhook_secret_enc)}
    except Exception:
        return {"secret": None}


class WebhookUpdate(BaseModel):
    webhook_url: str | None = None

@router.patch("/webhook", dependencies=[Depends(rate_limit_moderate)])
async def update_webhook(
    body: WebhookUpdate,
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    if body.webhook_url is not None:
        try:
            user.webhook_url = validate_url(body.webhook_url, "Webhook URL")
        except ValueError as e:
            raise HTTPException(422, str(e))
    db.add(user)
    await db.commit()
    return {"message": "Webhook URL updated"}


@router.post("/regenerate-api-key", dependencies=[Depends(rate_limit_moderate)])
async def regenerate_api_key(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    user.api_key = generate_api_key()
    db.add(user)
    await db.commit()
    return {"api_key": user.api_key, "message": "API key regenerated."}


@router.post("/regenerate-webhook-secret", dependencies=[Depends(rate_limit_moderate)])
async def regenerate_webhook_secret(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    secret = generate_webhook_secret()
    user.webhook_secret_enc = encrypt_field(secret)
    db.add(user)
    await db.commit()
    return {"webhook_secret": secret, "message": "Save this secret it won't be shown again."}


class PasswordChange(BaseModel):
    current_password: str | None = None
    new_password:     str

@router.get("/password-status")
async def password_status(user: User = Depends(get_current_user)):
    """Tells the frontend whether this account has a local password set.
    Google-only / Firebase accounts created by registering with an unusable
    local password will have has_password=False — the security page should
    show the 'Create password' variant in that case."""
    from app.core.security import verify_password
    # A local password is considered "set" if the user can authenticate with a
    # well-known empty/random string AND hashed_password is non-empty.
    # We have no direct flag, so infer it heuristically:
    #   - Firebase-only accounts were created with a random unusable hash.
    #   - Heuristic: treat any account with firebase_uid AND password_changed_at
    #     is None AND the user was created via Firebase as "no local password".
    has_password = not (
        user.firebase_uid
        and user.password_changed_at is None
    )
    return {
        "has_password":         bool(has_password),
        "has_firebase":         bool(user.firebase_uid),
        "password_changed_at":  user.password_changed_at.isoformat() if user.password_changed_at else None,
    }


@router.post("/change-password", dependencies=[Depends(rate_limit_auth)])
async def change_password(
    body: PasswordChange,
    request: Request,
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Unified endpoint:
      * user has a password (has_password=True) → current_password is REQUIRED.
      * user has no password yet (Google-only)  → current_password is ignored;
        just sets the local password for the first time.

    When the user has a Firebase account, the password is also mirrored to
    Firebase so both backends stay in sync.
    """
    from app.core.security import hash_password
    from datetime import datetime, timezone

    has_password = not (user.firebase_uid and user.password_changed_at is None)

    if has_password:
        if not body.current_password:
            raise HTTPException(400, "Current password is required.")
        if not verify_password(body.current_password, user.hashed_password):
            raise HTTPException(400, "Password incorrect")

    try:
        validate_password(body.new_password)
    except ValueError as e:
        raise HTTPException(422, str(e))

    # 1) local hash
    user.hashed_password     = hash_password(body.new_password)
    user.password_changed_at = datetime.now(timezone.utc)
    db.add(user)
    await db.commit()

    # 2) mirror to Firebase if account is linked there
    if user.firebase_uid:
        try:
            from app.core import firebase_auth as _fb
            _fb.set_password(user.firebase_uid, body.new_password)
            _fb.revoke_refresh_tokens(user.firebase_uid)
        except Exception:
            pass  # best-effort — local password still updated

    return {
        "message": "Password set." if not has_password else "Password changed successfully",
        "has_password": True,
    }


@router.get("/sales")
async def sales_summary(
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
    limit: int = 50,
):
    res = await db.execute(
        select(Payment)
        .where(Payment.merchant_id == user.id, Payment.status == PaymentStatus.confirmed)
        .order_by(Payment.confirmed_at.desc())
        .limit(limit)
    )
    payments = res.scalars().all()
    return {
        "summary": {
            "balance_available": round(user.balance_firo or 0, 8),
            "balance_pending":   round(user.balance_pending or 0, 8),
            "total_earned":      round(user.total_earned_firo or 0, 8),
            "total_fees_paid":   round(user.total_fees_firo or 0, 8),
            "total_withdrawn":   round(user.balance_withdrawn or 0, 8),
            "platform_fee_pct":  1.5,
        },
        "sales": [
            {
                "payment_id":  p.id,
                "order_id":    p.order_id,
                "amount_firo": p.amount_firo,
                "fee_firo":    p.platform_fee_firo,
                "net_firo":    p.merchant_net_firo,
                "txid":        p.txid,
                "confirmed_at": p.confirmed_at.isoformat() if p.confirmed_at else None,
            }
            for p in payments
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Privacy Mode Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/privacy-status")
async def get_privacy_status(request: Request, user: User = Depends(get_current_user)):
    """
    Get current privacy status for the user.
    Returns privacy mode settings and current session info.
    """
    from app.services.privacy_service import is_onion_request, check_privacy_mode_access
    
    is_onion = is_onion_request(request)
    access_check = check_privacy_mode_access(user, request)
    
    return {
        "privacy_mode": user.privacy_mode,
        "created_via_onion": user.created_via_onion,
        "is_onion_session": is_onion,
        "warning": access_check.get("warning"),
        "recommendations": access_check.get("recommendations", []),
    }


class PrivacyModeUpdate(BaseModel):
    privacy_mode: bool


@router.patch("/privacy-mode")
async def update_privacy_mode(
    body: PrivacyModeUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Toggle privacy mode for the user.
    
    When enabled:
    - IP addresses won't be logged
    - Minimal session metadata stored
    - Enhanced privacy protections
    
    Note: created_via_onion cannot be changed (historical record).
    """
    from app.services.privacy_service import is_onion_request
    
    is_onion = is_onion_request(request)
    
    user.privacy_mode = body.privacy_mode
    await db.commit()
    
    message = "Privacy mode enabled" if body.privacy_mode else "Privacy mode disabled"
    
    return {
        "message": message,
        "privacy_mode": user.privacy_mode,
        "created_via_onion": user.created_via_onion,
        "is_onion_session": is_onion,
    }



# ─
# Merchant branding — App Name
# Rules:
#   * User sets it ONCE on first use (when app_name is empty).
#   * After first set, app_name_locked flips to True — direct changes are refused.
#   * To change later, user must submit a `change_app_name` report; admin
#     approves which sets app_name_change_allowed=True, which then permits
#     exactly one further change (permission cleared after the change is saved).
# ─

import re as _re_app

APP_NAME_RE = _re_app.compile(r"^[A-Za-z0-9 ._\-&'()]{2,40}$")


class AppNameIn(BaseModel):
    app_name: str


@router.get("/app-name")
async def get_app_name(user: User = Depends(get_current_user)):
    has_name = bool((user.app_name or "").strip())
    can_change = (not has_name) or bool(user.app_name_change_allowed)
    return {
        "app_name":                user.app_name or None,
        "has_app_name":            has_name,
        "locked":                  bool(user.app_name_locked),
        "change_allowed":          bool(user.app_name_change_allowed),
        "can_set_now":             can_change,
    }


@router.post("/app-name")
async def set_app_name(
    body: AppNameIn,
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    name = sanitize_str(body.app_name, 40).strip() if body.app_name else ""
    # Strip ALL HTML tags and dangerous characters from app_name
    # app_name is rendered in checkout page — must be plain text only
    import re as _re_name
    name = _re_name.sub(r'<[^>]+>', '', name)          # strip HTML tags
    name = _re_name.sub(r'[<>&"\'\\/]', '', name)       # strip dangerous chars
    name = _re_name.sub(r'javascript:|data:|vbscript:', '', name, flags=_re_name.IGNORECASE)
    name = name.strip()
    if not APP_NAME_RE.match(name):
        raise HTTPException(422, "App name must be 2–40 characters: letters, numbers, spaces and ._-&'() only.")

    has_name = bool((user.app_name or "").strip())

    if has_name and not user.app_name_change_allowed:
        raise HTTPException(
            403,
            "Your app name is locked. Please submit a report to request a change."
        )

    user.app_name = name
    user.app_name_locked = True
    # Consume the permission so the user can't rename again without another approval
    user.app_name_change_allowed = False
    db.add(user)
    await db.commit()
    return {
        "ok":        True,
        "app_name":  user.app_name,
        "locked":    True,
        "message":   "App name saved." if not has_name else "App name updated.",
    }


# ─ Brand Colors ─
import re as _re

_HEX_RE = _re.compile(r'^#[0-9A-Fa-f]{6}$')

def _valid_hex(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip()
    if not _HEX_RE.match(v):
        raise HTTPException(422, f"Invalid hex color: {v!r}. Must be #RRGGBB format.")
    return v.upper()


class BrandColorsIn(BaseModel):
    brand_primary: str | None = None   # buttons, accents
    brand_bg:      str | None = None   # page background
    brand_text:    str | None = None   # body text


@router.get("/brand-colors")
async def get_brand_colors(user: User = Depends(get_current_user)):
    return {
        "brand_primary": getattr(user, "brand_primary", None),
        "brand_bg":      getattr(user, "brand_bg",      None),
        "brand_text":    getattr(user, "brand_text",    None),
    }


@router.patch("/brand-colors")
async def save_brand_colors(
    body: BrandColorsIn,
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    user.brand_primary = _valid_hex(body.brand_primary)
    user.brand_bg      = _valid_hex(body.brand_bg)
    user.brand_text    = _valid_hex(body.brand_text)
    db.add(user)
    await db.commit()
    return {
        "ok":            True,
        "brand_primary": user.brand_primary,
        "brand_bg":      user.brand_bg,
        "brand_text":    user.brand_text,
    }


@router.delete("/brand-colors", status_code=204)
async def reset_brand_colors(
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    user.brand_primary = None
    user.brand_bg      = None
    user.brand_text    = None
    db.add(user)
    await db.commit()


@router.post("/test-webhook", dependencies=[Depends(rate_limit_moderate)])
async def test_webhook(
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Send a test webhook event to the merchant's configured endpoint."""
    if not user.webhook_url:
        raise HTTPException(400, "No webhook URL configured. Set one first.")

    import httpx, json, time, hmac, hashlib
    from app.core.security import decrypt_field

    payload = {
        "event":      "test",
        "payment_id": "test-00000000-0000-0000-0000-000000000000",
        "status":     "test",
        "amount":     0.0,
        "timestamp":  int(time.time()),
        "message":    "FiroGate webhook test — your endpoint is working.",
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    headers = {"Content-Type": "application/json", "X-FiroGate-Event": "test"}
    if user.webhook_secret_enc:
        try:
            secret = decrypt_field(user.webhook_secret_enc)
            sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
            headers["X-FiroGate-Signature"] = sig
        except Exception:
            pass

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(user.webhook_url, content=body, headers=headers)
        return {
            "ok":          True,
            "status_code": resp.status_code,
            "message":     f"Test delivered — HTTP {resp.status_code}",
        }
    except Exception as e:
        return {"ok": False, "message": f"Delivery failed: {str(e)[:100]}"}
