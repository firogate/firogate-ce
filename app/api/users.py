from fastapi import APIRouter, Depends, HTTPException, Request
from app.core.rate_limit import rate_limit_auth, rate_limit_moderate
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import get_settings

settings = get_settings()
from app.core.security import verify_access_token, generate_api_key, generate_webhook_secret, encrypt_field, decrypt_field
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

    # Operator-email auto-promotion: if this user's email is listed in the
    # OPERATOR_EMAILS env var but the DB still has them as a merchant, flip
    # the role to operator once. Happens lazily on the next request (e.g.
    # the dashboard's /profile call) so brand new operators don't need a
    # restart or manual DB edit.
    from app.core.config import get_settings
    _s = get_settings()
    if u.role != UserRole.operator and (_s.is_operator_email(u.email) or _s.is_operator_username(u.username)):
        u.role = UserRole.operator
        db.add(u)
        await db.commit()
        await db.refresh(u)
    return u


@router.get("/profile")
async def profile(request: Request, user: User = Depends(get_current_user)):
    from app.services.privacy_service import is_onion_request
    
    is_onion = is_onion_request(request)
    
    auth_provider = ("telegram" if getattr(user, "telegram_id", None)
                     else "google" if getattr(user, "firebase_uid", None)
                     else "wallet" if getattr(user, "wallet_address", None)
                     else "account_number" if getattr(user, "account_number_hash", None)
                     else "password")
    return {
        "id":               user.id,
        "username":         user.username,
        "email":            user.email,
        "auth_provider":    auth_provider,
        "app_name":         user.app_name,
        "app_name_locked":  bool(user.app_name_locked),
        "app_name_change_allowed": bool(user.app_name_change_allowed),
        "plan":             user.plan,
        "requests_total":   user.requests_total,
        "requests_used":    user.requests_used,
        "requests_left":    max(0, (user.requests_total or 0) - (user.requests_used or 0)),
        "rollover_requests":   int(user.rollover_requests or 0),
        "rollover_expires_at": user.rollover_expires_at.isoformat() if user.rollover_expires_at else None,
        "cycle_start_at":      user.cycle_start_at.isoformat() if user.cycle_start_at else None,
        # monthly_allowance = total available minus rolled-over portion
        "monthly_allowance":   max(0, (user.requests_total or 0) - (user.rollover_requests or 0)),
        "lifetime_gross_sales_firo":   round(user.lifetime_gross_sales_firo or 0, 8),
        "lifetime_received_firo":      round(user.lifetime_received_firo or 0, 8),
        "lifetime_confirmed_payments": int(user.lifetime_confirmed_payments or 0),
        "lifetime_completed_orders":   int(user.lifetime_completed_orders or 0),
        "plan_expires_at":  user.plan_expires_at.isoformat() if user.plan_expires_at else None,
        "api_key":          user.api_key,
        "webhook_url":      user.webhook_url,
        "has_webhook_secret": bool(user.webhook_secret_enc),
        "required_confirmations_policy": user.required_confirmations_policy,
        "payment_tolerance_firo":        user.payment_tolerance_firo,
        "totp_enabled":     bool(user.totp_enabled),
        "role":               user.role.value if hasattr(user.role, "value") else str(user.role),
        "is_operator":           user.role == UserRole.operator,
        "privacy_mode":       user.privacy_mode,
        "created_via_onion":  user.created_via_onion,
        "is_onion_session":   is_onion,
        "show_market_price":  bool(user.show_market_price),
        "has_seen_onboarding": bool(user.has_seen_onboarding),
        # Network info is auto-detected from the RPC port
        "is_testnet":         settings.is_testnet,
        "network":            settings.network_name,
        "network_label":      settings.network_label,
        "network_warning":    settings.network_warning,
    }


@router.post("/onboarding/seen")
async def mark_onboarding_seen(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    user.has_seen_onboarding = True
    db.add(user)
    await db.commit()
    return {"ok": True}


@router.post("/onboarding/restart")
async def restart_onboarding(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    user.has_seen_onboarding = False
    db.add(user)
    await db.commit()
    return {"ok": True}


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


class PaymentPolicyUpdate(BaseModel):
    required_confirmations_policy: int | None = None   # None = revert to instance default
    payment_tolerance_firo:        float | None = None  # None = revert to default tolerance

@router.patch("/payment-policy", dependencies=[Depends(rate_limit_moderate)])
async def update_payment_policy(
    body: PaymentPolicyUpdate,
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    from app.core.payment_policy import VALID_CONFIRMATION_POLICIES
    if body.required_confirmations_policy is not None and body.required_confirmations_policy not in VALID_CONFIRMATION_POLICIES:
        raise HTTPException(422, "required_confirmations_policy must be one of 0, 1, 3, 6")
    if body.payment_tolerance_firo is not None and not (0 <= body.payment_tolerance_firo <= 1):
        raise HTTPException(422, "payment_tolerance_firo must be between 0 and 1 FIRO")
    user.required_confirmations_policy = body.required_confirmations_policy
    user.payment_tolerance_firo = body.payment_tolerance_firo
    db.add(user)
    await db.commit()
    return {
        "message": "Payment policy updated",
        "required_confirmations_policy": user.required_confirmations_policy,
        "payment_tolerance_firo": user.payment_tolerance_firo,
    }


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
    local password will have has_password=False the security page should
    show the 'Create password' variant in that case."""
    # No direct "has local password" flag exists, so infer it: Firebase-only
    # accounts were created with a random unusable hash, so an account with
    # firebase_uid set and password_changed_at still None is treated as
    # "no local password".
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
    from app.core.security import hash_password, verify_password
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

    user.hashed_password     = hash_password(body.new_password)
    user.password_changed_at = datetime.now(timezone.utc)
    db.add(user)
    await db.commit()

    # Mirror to Firebase if the account is linked there, so both backends stay in sync
    if user.firebase_uid:
        try:
            from app.core import firebase_auth as _fb
            _fb.set_password(user.firebase_uid, body.new_password)
            _fb.revoke_refresh_tokens(user.firebase_uid)
        except Exception:
            pass  # best-effort local password still updated

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
    from sqlalchemy import func as _func
    agg = await db.execute(
        select(
            _func.count(Payment.id),
            _func.sum(Payment.amount_firo),
            _func.sum(Payment.amount_received),
        ).where(
            Payment.merchant_id == user.id,
            Payment.status == PaymentStatus.confirmed,
        )
    )
    agg_row = agg.one()
    confirmed_count  = int(agg_row[0] or 0)
    gross_sales_firo = round(float(agg_row[1] or 0), 8)
    received_firo    = round(float(agg_row[2] or 0), 8)
    avg_order_value  = round(gross_sales_firo / confirmed_count, 8) if confirmed_count else 0.0

    total_res = await db.execute(
        select(_func.count(Payment.id)).where(Payment.merchant_id == user.id)
    )
    total_count = int(total_res.scalar() or 0)
    success_rate = round(confirmed_count / total_count * 100, 1) if total_count else 0.0

    res = await db.execute(
        select(Payment)
        .where(Payment.merchant_id == user.id, Payment.status == PaymentStatus.confirmed)
        .order_by(Payment.confirmed_at.desc())
        .limit(limit)
    )
    payments = res.scalars().all()

    return {
        "metrics": {
            "total_confirmed_payments": confirmed_count,
            "total_completed_orders":   confirmed_count,
            "gross_sales_firo":         gross_sales_firo,
            "received_firo":            received_firo,
            "avg_order_value":          avg_order_value,
            "payment_success_rate_pct": success_rate,
        },
        "sales": [
            {
                "payment_id":   p.id,
                "order_id":     p.order_id,
                "amount_firo":  p.amount_firo,
                "amount_received": p.amount_received,
                "txid":         p.txid,
                "confirmed_at": p.confirmed_at.isoformat() if p.confirmed_at else None,
            }
            for p in payments
        ],
    }


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


class MarketPriceUpdate(BaseModel):
    show_market_price: bool


@router.patch("/market-price-setting")
async def update_market_price_setting(
    body: MarketPriceUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Toggle the live FIRO market price widgets (dashboard stat, payment
    link ticker). Off by default — when off, those widgets never fetch."""
    user.show_market_price = body.show_market_price
    await db.commit()
    return {"show_market_price": user.show_market_price}


# Telegram notifications (bot channel; independent of Telegram login)

@router.get("/telegram-status")
async def telegram_status(user: User = Depends(get_current_user)):
    s = get_settings()
    return {
        "configured":      s.telegram_bot_enabled,
        "bot_username":    s.TELEGRAM_BOT_USERNAME if s.telegram_bot_enabled else None,
        "connected":       bool(user.telegram_chat_id),
        "notify_telegram": bool(user.notify_telegram),
        "notify_on_payment": bool(user.notify_on_payment),
    }


@router.post("/telegram-connect", dependencies=[Depends(rate_limit_moderate)])
async def telegram_connect(user: User = Depends(get_current_user)):
    s = get_settings()
    if not s.telegram_bot_enabled:
        raise HTTPException(503, "Telegram notifications are not configured on this server.")
    from app.services import telegram_bot as tg
    token = tg.new_connect_token(user.id)
    return {"link": tg.connect_link(token), "expires_in": tg.CONNECT_TTL}


@router.delete("/telegram-connect")
async def telegram_disconnect(user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    chat_id = user.telegram_chat_id
    user.telegram_chat_id = None
    user.notify_telegram = False
    db.add(user)
    await db.commit()
    if chat_id and get_settings().telegram_bot_enabled:
        import asyncio as _asyncio
        from app.services.telegram_bot import send_message as _tg_send
        _asyncio.create_task(_tg_send(chat_id,
            "🔌 This chat was disconnected from your FiroGate account via the "
            "dashboard. You will no longer receive notifications here.\n\n"
            "You can reconnect anytime from Security → Notifications."))
    return {"message": "Telegram disconnected."}


class TelegramNotifyUpdate(BaseModel):
    enabled: bool


@router.patch("/telegram-notify")
async def telegram_notify_toggle(body: TelegramNotifyUpdate,
                                 user: User = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    if body.enabled and not user.telegram_chat_id:
        raise HTTPException(400, "Connect Telegram first.")
    changed = bool(body.enabled) != bool(user.notify_telegram)
    user.notify_telegram = bool(body.enabled)
    db.add(user)
    await db.commit()
    if changed and user.telegram_chat_id and get_settings().telegram_bot_enabled:
        import asyncio as _asyncio
        from app.services.telegram_bot import send_message as _tg_send
        _asyncio.create_task(_tg_send(user.telegram_chat_id,
            "🔔 Payment notifications are <b>ON</b>. You'll receive alerts here "
            "whenever a payment is confirmed."
            if body.enabled else
            "🔕 Payment notifications are <b>paused</b> from the dashboard. "
            "This chat stays connected turn them back on anytime."))
    return {"message": "Updated.", "notify_telegram": user.notify_telegram}


# Merchant branding: App Name
# Rules:
#   * User sets it ONCE on first use (when app_name is empty).
#   * After first set, app_name_locked flips to True - direct changes are refused.
#   * To change later, user must submit a `change_app_name` report; operator
#     approves which sets app_name_change_allowed=True, which then permits
#     exactly one further change (permission cleared after the change is saved).

import re as _re_app

APP_NAME_RE = _re_app.compile(r"^[A-Za-z0-9 ._\-&'()]{2,40}$")


class AppNameIn(BaseModel):
    app_name: str


FREE_NAME_CHANGES = 3           # first 3 changes are instant
NAME_CHANGE_COOLDOWN_DAYS = 14  # after that, one change per 14 days


def _name_change_state(user: User):
    """Return (can_change_now, seconds_remaining) for this merchant's name."""
    from datetime import datetime, timezone, timedelta
    count = int(user.app_name_change_count or 0)
    if count < FREE_NAME_CHANGES:
        return True, 0
    last = user.app_name_last_changed_at
    if not last:
        return True, 0
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    nxt = last + timedelta(days=NAME_CHANGE_COOLDOWN_DAYS)
    now = datetime.now(timezone.utc)
    if now >= nxt:
        return True, 0
    return False, int((nxt - now).total_seconds())


@router.get("/app-name")
async def get_app_name(user: User = Depends(get_current_user)):
    has_name = bool((user.app_name or "").strip())
    can_change, secs = _name_change_state(user)
    count = int(user.app_name_change_count or 0)
    return {
        "app_name":          user.app_name or None,
        "has_app_name":      has_name,
        "can_set_now":       (not has_name) or can_change,
        "changes_used":      count,
        "free_changes":      FREE_NAME_CHANGES,
        "free_remaining":    max(0, FREE_NAME_CHANGES - count),
        "cooldown_seconds":  secs,
        "cooldown_days":     NAME_CHANGE_COOLDOWN_DAYS,
    }


def clean_app_name(raw: str) -> str:
    name = sanitize_str(raw, 40).strip() if raw else ""
    import re as _re_name
    name = _re_name.sub(r'<[^>]+>', '', name)
    name = _re_name.sub(r'[<>&"\'\\/]', '', name)
    name = _re_name.sub(r'javascript:|data:|vbscript:', '', name, flags=_re_name.IGNORECASE)
    name = name.strip()
    if not APP_NAME_RE.match(name):
        raise HTTPException(422, "App name must be 2–40 characters: letters, numbers, spaces and ._-&'() only.")
    return name


@router.post("/app-name")
async def set_app_name(
    body: AppNameIn,
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    from datetime import datetime, timezone
    name = clean_app_name(body.app_name)

    has_name = bool((user.app_name or "").strip())

    # Setting the name for the very first time is always allowed and is NOT counted as a "change"
    if not has_name:
        user.app_name = name
        user.app_name_locked = True
        db.add(user)
        await db.commit()
        return {"ok": True, "app_name": user.app_name, "message": "App name saved.",
                "free_remaining": FREE_NAME_CHANGES}

    if name == (user.app_name or "").strip():
        return {"ok": True, "app_name": user.app_name, "message": "No change."}

    can_change, secs = _name_change_state(user)
    if not can_change:
        days = (secs + 86399) // 86400
        raise HTTPException(
            429,
            f"You've used your {FREE_NAME_CHANGES} free name changes. "
            f"You can change it again in about {days} day(s)."
        )

    user.app_name = name
    user.app_name_locked = True
    user.app_name_change_count = int(user.app_name_change_count or 0) + 1
    user.app_name_last_changed_at = datetime.now(timezone.utc)
    db.add(user)
    await db.commit()
    _, secs_after = _name_change_state(user)
    return {
        "ok":            True,
        "app_name":      user.app_name,
        "message":       "App name updated.",
        "changes_used":  user.app_name_change_count,
        "free_remaining": max(0, FREE_NAME_CHANGES - user.app_name_change_count),
        "cooldown_seconds": secs_after,
    }


# Merchant Logo
# Accepts up to 5 MB; auto-compressed server-side (Pillow re-encode, strips
# EXIF/metadata). Stored as base64 data URI. Shown on the checkout page.
_LOGO_MAX_INPUT  = 5 * 1024 * 1024    # 5 MB max raw upload
_LOGO_MAX_OUTPUT = 400 * 1024         # 400 KB max after compression
_LOGO_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
# SVG is intentionally NOT accepted. It's an XML/script-capable format and a
# denylist-based sanitizer (checking for <script>, on*= handlers, etc.) is
# fundamentally bypassable (SMIL event attributes, entity/CDATA obfuscation,
# nested <image href="data:...">, and browser-specific quirks). Every other
# accepted type is re-encoded from scratch via Pillow, which strips any
# embedded payload; there is no equivalent safe path for SVG without a
# dedicated rasterizer, so it's rejected rather than shipped half-sanitized.
_LOGO_RE = _re_app.compile(r'^data:(image/[a-z.+-]+);base64,([A-Za-z0-9+/=]+)$')


def _sniff_image(b: bytes) -> str | None:
    if b[:8] == b"\x89PNG\r\n\x1a\n":               return "image/png"
    if b[:3] == b"\xff\xd8\xff":                     return "image/jpeg"
    if b[:6] in (b"GIF87a", b"GIF89a"):              return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":      return "image/webp"
    head = b[:512].lstrip().lower()
    if head.startswith(b"<?xml") or head.startswith(b"<svg"): return "image/svg+xml"
    return None


def _compress_logo(data: bytes, mime: str) -> tuple[bytes, str]:
    """
    Resize to max 400×400 and re-encode with Pillow. Re-encoding from scratch
    strips all EXIF metadata, ICC profiles, and any embedded payloads which
    is stronger security than a magic-byte check alone. Returns (bytes, mime).
    """
    import io
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ValueError("Cannot decode image")

    img.thumbnail((400, 400), Image.LANCZOS)

    out = io.BytesIO()
    has_alpha = img.mode in ("RGBA", "LA", "PA")

    if has_alpha:
        img = img.convert("RGBA")
        img.save(out, format="WEBP", quality=88, method=4)
        result, out_mime = out.getvalue(), "image/webp"
        if len(result) > _LOGO_MAX_OUTPUT:
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True, compress_level=9)
            result, out_mime = out.getvalue(), "image/png"
    elif mime == "image/gif":
        img.save(out, format="GIF")
        result, out_mime = out.getvalue(), "image/gif"
    else:
        img = img.convert("RGB")
        img.save(out, format="WEBP", quality=88, method=4)
        result, out_mime = out.getvalue(), "image/webp"

    if len(result) > _LOGO_MAX_OUTPUT:
        raise ValueError(f"Image too large after compression. Try a simpler logo.")
    return result, out_mime


class LogoIn(BaseModel):
    logo: str   # full data URI: "data:image/png;base64,...."


@router.get("/app-logo")
async def get_app_logo(user: User = Depends(get_current_user)):
    return {"app_logo": user.app_logo or None, "has_logo": bool(user.app_logo)}


@router.post("/app-logo")
async def set_app_logo(
    body: LogoIn,
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    import base64 as _b64
    raw = (body.logo or "").strip()
    if not raw:
        raise HTTPException(422, "No image provided.")
    m = _LOGO_RE.match(raw)
    if not m:
        raise HTTPException(422, "Invalid image. Use PNG, JPEG, WebP, or GIF.")
    mime, b64data = m.group(1).lower(), m.group(2)
    if mime not in _LOGO_TYPES:
        raise HTTPException(422, "Unsupported image type.")
    try:
        decoded = _b64.b64decode(b64data, validate=True)
    except Exception:
        raise HTTPException(422, "Corrupt image data.")
    if len(decoded) == 0:
        raise HTTPException(422, "Empty image.")
    if len(decoded) > _LOGO_MAX_INPUT:
        raise HTTPException(422, f"Image too large. Max 5 MB.")

    # Verify magic bytes - rejects executables/scripts renamed as images.
    sniffed = _sniff_image(decoded)
    if sniffed is None or sniffed != mime:
        raise HTTPException(422, "File content does not match its declared type.")

    try:
        comp_bytes, out_mime = _compress_logo(decoded, mime)
    except ValueError as e:
        raise HTTPException(422, str(e))
    stored = f"data:{out_mime};base64,{_b64.b64encode(comp_bytes).decode()}"

    user.app_logo = stored
    db.add(user)
    await db.commit()
    return {"ok": True, "has_logo": True}


@router.delete("/app-logo")
async def delete_app_logo(
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    user.app_logo = None
    db.add(user)
    await db.commit()
    return {"ok": True, "has_logo": False}


# Brand Colors
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

    import json, time, hmac, hashlib
    from app.core.security import decrypt_field
    from app.core.validators import validate_url
    from app.services.webhook import _build_client

    # Re-validate at send time (not just at registration time) blocks
    # DNS-rebinding, where a domain resolved to a public IP when the webhook
    # URL was saved but now resolves to an internal/metadata address.
    try:
        safe_url = validate_url(user.webhook_url, "webhook_url")
    except HTTPException:
        raise HTTPException(400, "Webhook URL is no longer valid (points to a disallowed host).")

    payload = {
        "event":      "test",
        "payment_id": "test-00000000-0000-0000-0000-000000000000",
        "status":     "test",
        "amount":     0.0,
        "timestamp":  int(time.time()),
        "message":    "FiroGate webhook test your endpoint is working.",
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
        async with _build_client(safe_url) as client:
            resp = await client.post(safe_url, content=body, headers=headers)
        return {
            "ok":          True,
            "status_code": resp.status_code,
            "message":     f"Test delivered HTTP {resp.status_code}",
        }
    except Exception as e:
        return {"ok": False, "message": f"Delivery failed: {str(e)[:100]}"}
