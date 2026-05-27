"""
Firebase-backed authentication endpoints (hybrid).

Flow:
  * Register / Google sign-in / Login happen via Firebase (client SDK + Admin).
  * After a successful Firebase step, we exchange the Firebase ID token for
    the existing JWT httponly cookie (same format as app.api.auth.login) so
    that ALL downstream features keep working unchanged: TOTP 2FA, admin
    guard, API keys, webhooks, session binding, Tor privacy mode, etc.

Endpoints:
  POST /api/auth/fb/register
  POST /api/auth/fb/login
  POST /api/auth/fb/google
  POST /api/auth/fb/forgot-password
  POST /api/auth/fb/reset-password
  POST /api/auth/fb/verify-email
  POST /api/auth/fb/resend-verification
  GET  /api/auth/fb/config        (safe public config for frontend SDK)
"""
from __future__ import annotations

import hashlib
import re as _re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import rate_limit_auth, rate_limit_moderate, get_rate_limiter, get_client_ip
from app.core.security import (
    create_access_token, hash_password, generate_api_key, generate_webhook_secret,
    encrypt_field,
)
from app.core.validators import validate_password, sanitize_str
from app.core import firebase_auth as fb
from app.core import turnstile
from app.services import mailer
from app.models.models import (
    User, UserRole, LoginAttempt, AuthActionToken, AuthActionPurpose,
)

router = APIRouter(prefix="/api/auth/fb", tags=["auth-firebase"])

EMAIL_RE = _re.compile(r"^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{1,63}$")
GENERIC_MSG = "If an account with that email exists, we sent a link."


# ─ helpers ─
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_token_pair(purpose: AuthActionPurpose) -> tuple[str, str, datetime]:
    s = get_settings()
    ttl = (s.EMAIL_VERIFICATION_EXPIRE_SECONDS if purpose == AuthActionPurpose.verify_email
           else s.PASSWORD_RESET_EXPIRE_SECONDS)
    raw = secrets.token_urlsafe(32)
    return raw, _hash_token(raw), _now() + timedelta(seconds=max(300, int(ttl)))


def _client_ip(request: Request) -> str:
    """Extract the real client IP — same logic as _get_current_user in auth.py
    so the session-binding fingerprint matches between issue and verify."""
    ip = request.headers.get("CF-Connecting-IP", "").strip()
    if not ip:
        ip = request.headers.get("X-Real-IP", "").strip()
    if not ip:
        ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not ip:
        ip = request.client.host if request.client else ""
    return ip


def _rate_limit_ip(request: Request) -> str:
    """Rate-limit / cooldown identifier — keeps privacy-mode hashing behavior."""
    return get_client_ip(request) or "unknown"


async def _get_recent_user(db: AsyncSession, email: str) -> Optional[User]:
    if not email:
        return None
    res = await db.execute(select(User).where(User.email == email.lower()))
    return res.scalar_one_or_none()


async def _consume_token(db: AsyncSession, raw_token: str, purpose: AuthActionPurpose) -> Optional[AuthActionToken]:
    token_hash = _hash_token(raw_token)
    res = await db.execute(select(AuthActionToken).where(
        AuthActionToken.token_hash == token_hash,
        AuthActionToken.purpose == purpose,
    ))
    tok = res.scalar_one_or_none()
    if not tok:
        return None
    if tok.used:
        return None
    if tok.expires_at and tok.expires_at.replace(tzinfo=timezone.utc) < _now():
        return None
    tok.used = True
    tok.used_at = _now()
    db.add(tok)
    return tok


async def _issue_app_cookie(response: Response, user: User, request: Request) -> str:
    """Issue the app's own JWT cookie (same as legacy login) after Firebase auth.

    For Firebase-backed accounts we skip the IP/UA session-binding fingerprint:
    behind Cloudflare + nginx the extracted IP can subtly differ between the
    request that issues the cookie and the request that verifies it, which
    would break every session for no security benefit — Firebase already
    revokes refresh tokens on password change.
    """
    from app.services.privacy_service import is_onion_request
    from app.core.security import _cookie_kwargs
    is_onion = is_onion_request(request)
    is_firebase = bool(getattr(user, "firebase_uid", None))
    privacy = is_onion or bool(user.privacy_mode) or is_firebase
    ip = "" if privacy else _client_ip(request)
    ua = "" if privacy else request.headers.get("user-agent", "")
    token = create_access_token(user.id, ip=ip, ua=ua, privacy=privacy)
    response.set_cookie("access_token", token, **_cookie_kwargs(request))
    return token


def _require_not_onion(request: Request) -> None:
    """Firebase + Turnstile + email flows are disabled for onion sessions."""
    from app.services.privacy_service import is_onion_request
    if is_onion_request(request):
        raise HTTPException(400, "Firebase auth is disabled on Tor. Use the legacy username/password form.")


def _base_url(request: Request) -> str:
    """
    Build the public base URL used in outgoing email links.

    Priority:
      1. explicit BASE_URL from .env (preferred — set this when behind nginx or
         Cloudflare Tunnel so links always match the public hostname)
      2. X-Forwarded-Proto + X-Forwarded-Host (trusted when the request carries
         them, e.g. behind a reverse proxy)
      3. request.base_url (last resort — may be localhost behind a proxy)
    """
    s = get_settings()
    if s.BASE_URL:
        return s.BASE_URL.rstrip("/")

    proto = (request.headers.get("X-Forwarded-Proto", "") or "").split(",")[0].strip()
    host  = (request.headers.get("X-Forwarded-Host",  "") or "").split(",")[0].strip()
    if not host:
        host = (request.headers.get("Host", "") or "").strip()
    if not proto:
        proto = (request.url.scheme or "https")
    if host:
        return f"{proto}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def _action_url(request: Request, mode: str, token: str) -> str:
    """
    Build an email-action URL served by OUR own FastAPI dispatcher at
    /auth/action. Typical shape:

        https://dashboard.firogate.com/auth/action?mode=verifyEmail&oobCode=<token>

    AUTH_URL from .env selects the hostname. We strip both whitespace
    and trailing slashes defensively — a stray space in .env silently
    produced invalid links (`https://... /auth/action`) before.
    """
    s = get_settings()
    origin = (s.AUTH_URL or "").strip().rstrip("/") or _base_url(request)
    from urllib.parse import urlencode
    qs = urlencode({"mode": mode, "oobCode": token})
    return f"{origin}/auth/action?{qs}"


async def _cooldown_check(request: Request, email_key: str) -> None:
    """Per-IP cooldown for reset/verification emails.

    Lenient by design — email delivery is the actual goal. We use a single
    short per-IP window and DO NOT add a per-email lock: the latter caused
    "user exists, clicks forgot-password twice, second click 429s for
    60 s with a confusing message" and locked real users out when SMTP
    transiently dropped the first attempt. Per-IP alone still stops bulk
    abuse, and the rate_limit_moderate dependency adds a second safety net.
    """
    s = get_settings()
    cooldown = max(1, int(s.RESET_COOLDOWN_SECONDS))
    limiter = get_rate_limiter()
    ip_key = _rate_limit_ip(request)
    allowed, _, _ = await limiter.check(f"auth_action:ip:{ip_key}", 1, cooldown)
    if not allowed:
        raise HTTPException(429, "Please wait a few seconds before requesting another email.")


def _ts_error(reason: str) -> str:
    """Map a Cloudflare Turnstile error code to a user-facing diagnostic string."""
    hints = {
        "missing-input-response": "Turnstile widget did not produce a token. Reload the page and try again.",
        "invalid-input-response": "Turnstile token was rejected by Cloudflare. Check that the site key and secret key match and that this domain is listed in the Turnstile widget settings.",
        "timeout-or-duplicate":   "Turnstile token expired or was already used. Please try again.",
        "invalid-input-secret":   "Server Turnstile secret is invalid. Update TURNSTILE_SECRET_KEY in .env.",
        "missing-input-secret":   "Server Turnstile secret is missing. Set TURNSTILE_SECRET_KEY in .env.",
        "bad-request":            "Cloudflare rejected the verification request (bad request).",
        "network-error":          "Could not reach Cloudflare to verify the challenge. Check server network.",
    }
    base = hints.get(reason, f"Turnstile verification failed ({reason}).")
    return base


async def _verify_ts_or_raise(token: str | None, remote_ip: str) -> None:
    """Strict Turnstile check — raises on failure. Used for register and
    password-reset-confirm, where abuse has real downside and a blocked
    hostname should surface as an actionable error."""
    ok, reason = await turnstile.verify_with_reason(token, remote_ip)
    if not ok:
        raise HTTPException(400, _ts_error(reason))


async def _verify_ts_lenient(token: str | None, remote_ip: str) -> None:
    """Verify Turnstile but DO NOT block on hostname mismatch / missing token.

    Used for low-risk flows (forgot-password, resend-verification) where
    blocking because the Turnstile widget isn't configured for the current
    hostname (e.g. firogate.com vs dashboard.firogate.com) would lock real
    users out. Rate-limiting + per-IP cooldown still cap abuse volume.
    """
    ok, reason = await turnstile.verify_with_reason(token, remote_ip)
    if ok:
        return
    logger.info(f"[turnstile] lenient check failed ({reason}) — allowing request")
    return


# ─ Public config (safe to expose) ─
@router.get("/config")
async def fb_config():
    s = get_settings()
    return {
        "firebase": {
            "apiKey":     s.FIREBASE_API_KEY or "",
            "authDomain": s.FIREBASE_AUTH_DOMAIN or "",
            "projectId":  s.FIREBASE_PROJECT_ID or "",
            "appId":      s.FIREBASE_APP_ID or "",
        },
        "turnstileSiteKey": s.TURNSTILE_SITE_KEY or "",
        "googleClientId":   s.GOOGLE_CLIENT_ID or "",
        "enabled":          fb.is_configured(),
    }


# ─ 1. Register ──
class RegisterIn(BaseModel):
    email:           EmailStr
    password:        str
    username:        str | None = None
    full_name:       str | None = None
    turnstile_token: str | None = None
    agreed_to_terms: bool = False


@router.post("/register", status_code=201, dependencies=[Depends(rate_limit_moderate)])
async def fb_register(body: RegisterIn, request: Request, db: AsyncSession = Depends(get_db)):
    _require_not_onion(request)

    if not body.agreed_to_terms:
        raise HTTPException(400, "You must agree to the Terms of Service and Privacy Policy.")

    await _verify_ts_or_raise(body.turnstile_token, _client_ip(request))

    try:
        validate_password(body.password)
    except ValueError as e:
        raise HTTPException(422, str(e))

    email = str(body.email).strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Invalid email address format.")

    # Generic, enumeration-safe response returned on ALL outcomes below
    generic_resp = {
        "message": "Account created. Please check your email to verify your address.",
    }

    # Username auto-generated from email local part if not provided
    username = (body.username or email.split("@", 1)[0]).lower()
    username = _re.sub(r"[^a-z0-9_\-]", "-", username)[:32] or f"user-{secrets.token_hex(3)}"

    # If email already registered in our SQL → silently return generic message
    existing = await _get_recent_user(db, email)
    if existing:
        return generic_resp

    # If username clashes, auto-suffix so registration can proceed
    for _ in range(6):
        res = await db.execute(select(User).where(User.username == username))
        if not res.scalar_one_or_none():
            break
        username = f"{username[:26]}-{secrets.token_hex(2)}"

    # Create Firebase user (authoritative credential store)
    try:
        fb_user = fb.create_user(email=email, password=body.password, display_name=body.full_name)
    except Exception as exc:
        # Could be 'EMAIL_EXISTS' in Firebase but not in our SQL — still generic
        msg = str(exc).lower()
        if "exists" in msg or "already" in msg:
            return generic_resp
        raise HTTPException(500, "Unable to create account right now. Please try again.") from exc

    # Create local SQL user — password is NOT reused (we use an unusable local hash)
    user = User(
        username=username,
        email=email,
        full_name=sanitize_str(body.full_name, 128) if body.full_name else None,
        hashed_password=hash_password(secrets.token_urlsafe(32)),   # unusable — Firebase owns the password
        role=UserRole.merchant,
        api_key=generate_api_key(),
        api_key_active=True,
        webhook_secret_enc=encrypt_field(generate_webhook_secret()),
        requests_total=50,
        requests_used=0,
        balance_firo=0.0,
        firebase_uid=fb_user.uid,
        email_verified=False,
        privacy_mode=False,
        created_via_onion=False,
    )
    db.add(user)
    await db.flush()
    user_id = user.id

    # Single-use verification token
    raw, tok_hash, expires_at = _generate_token_pair(AuthActionPurpose.verify_email)
    db.add(AuthActionToken(
        user_id=user_id, token_hash=tok_hash,
        purpose=AuthActionPurpose.verify_email, expires_at=expires_at,
    ))
    await db.commit()

    # Update analytics
    try:
        from app.services.analytics_service import on_user_registered
        await on_user_registered(db, user)
    except Exception:
        pass

    verify_url = _action_url(request, "verifyEmail", raw)
    try:
        sent = await mailer.send_verification_email(email, verify_url)
        if not sent:
            logger.warning(f"[fb-register] verification email NOT sent to {email} — check SMTP logs")
    except Exception as exc:
        logger.error(f"[fb-register] verification email exception for {email}: {exc}")

    return generic_resp


# ─ 2. Login (Firebase ID token → JWT cookie) ──
class LoginIn(BaseModel):
    id_token:        str
    turnstile_token: str | None = None
    totp_code:       str | None = None


@router.post("/login", dependencies=[Depends(rate_limit_auth)])
async def fb_login(body: LoginIn, response: Response, request: Request, db: AsyncSession = Depends(get_db)):
    _require_not_onion(request)

    # Turnstile intentionally NOT required on login — real users got stuck
    # when the widget was slow or blocked. rate_limit_auth still caps brute-force.

    # check_revoked=False here on purpose: enabling it forces an extra HTTPS
    # round-trip to Google on every login. Behind a flaky network or a strict
    # firewall that single call surfaces as "Could not verify session" — which
    # is the misleading "session cookie" error users hit on the VPS. Refresh
    # tokens are short-lived and we revoke them on password change, so this
    # is safe for sign-in. Use check_revoked=True only on long-lived sessions.
    try:
        claims = fb.verify_id_token(body.id_token, check_revoked=False)
    except ValueError as e:
        raise HTTPException(401, str(e))

    email_verified = bool(claims.get("email_verified"))
    uid = claims.get("uid") or claims.get("user_id")
    email = (claims.get("email") or "").lower()
    if not uid or not email:
        raise HTTPException(401, "Invalid session.")

    provider = (claims.get("firebase", {}) or {}).get("sign_in_provider", "")

    # Look up our user by firebase_uid first, then by email (link existing accounts)
    res = await db.execute(select(User).where(User.firebase_uid == uid))
    user = res.scalar_one_or_none()
    if not user and email:
        res = await db.execute(select(User).where(User.email == email.lower()))
        user = res.scalar_one_or_none()
        if user and not user.firebase_uid:
            user.firebase_uid = uid
            db.add(user)
            await db.flush()

    if not user or not user.is_active:
        raise HTTPException(401, "Account not found or inactive.")

    # Google sign-in is inherently verified; mirror into SQL
    if provider == "google.com":
        email_verified = True
        if not user.email_verified:
            user.email_verified = True

    if not email_verified:
        # Auto-resend a fresh verification link so the user isn't stranded,
        # then return the same generic message as a truly non-existent
        # account so we don't leak account state.
        try:
            if user.firebase_uid and user.email:
                raw, tok_hash, expires_at = _generate_token_pair(AuthActionPurpose.verify_email)
                db.add(AuthActionToken(
                    user_id=user.id, token_hash=tok_hash,
                    purpose=AuthActionPurpose.verify_email, expires_at=expires_at,
                ))
                await db.commit()
                verify_url = _action_url(request, "verifyEmail", raw)
                await mailer.send_verification_email(user.email, verify_url)
        except Exception as exc:
            logger.warning(f"[fb-login] auto-resend verification failed: {exc}")
        raise HTTPException(
            401,
            "Account not found or inactive. We just sent a verification link to your email — please check your inbox.",
        )
    if not user.email_verified:
        user.email_verified = True
        db.add(user)
        await db.flush()

    # TOTP second factor — unchanged from legacy flow
    if user.totp_enabled and user.totp_secret_enc:
        code = (body.totp_code or "").strip()
        if not code:
            raise HTTPException(403, "2FA code required")
        from app.core.totp import verify_totp_code, decrypt_totp_secret
        from app.core.security import get_fernet
        try:
            secret = decrypt_totp_secret(user.totp_secret_enc, get_fernet())
            if not secret:
                raise ValueError("decrypt failed")
        except Exception:
            raise HTTPException(500, "2FA configuration error")
        if not verify_totp_code(secret, code):
            # backup codes (TOTP backup, not recovery codes)
            from app.api.auth import _use_backup_code
            if not _use_backup_code(user, code):
                raise HTTPException(401, "Invalid 2FA code")

    user.last_login_at = _now()
    db.add(LoginAttempt(username=user.username, ip_address=_client_ip(request), success=True))
    await db.commit()

    await _issue_app_cookie(response, user, request)

    return {
        "user_id":       user.id,
        "username":      user.username,
        "role":          user.role,
        "is_admin":      user.role == UserRole.admin,
        "has_2fa":       bool(user.totp_enabled),
        "requires_2fa":  False,
        "email":         user.email,
    }


# ─ 3. Google sign-in (auto-verified) ──
class GoogleLoginIn(BaseModel):
    id_token:        str
    turnstile_token: str | None = None


@router.post("/google", dependencies=[Depends(rate_limit_auth)])
async def fb_google(body: GoogleLoginIn, response: Response, request: Request, db: AsyncSession = Depends(get_db)):
    _require_not_onion(request)

    # Turnstile intentionally NOT required on Google sign-in — Google already
    # applies robust bot detection and the extra challenge caused user
    # lock-out on redirect-based flows. `rate_limit_auth` still applies.

    try:
        claims = fb.verify_id_token(body.id_token, check_revoked=False)
    except ValueError as e:
        raise HTTPException(401, str(e))

    provider = (claims.get("firebase", {}) or {}).get("sign_in_provider", "")
    if provider != "google.com":
        raise HTTPException(400, "Google sign-in required.")

    uid   = claims.get("uid") or claims.get("user_id")
    email = (claims.get("email") or "").lower()
    name  = claims.get("name") or ""
    if not uid or not email:
        raise HTTPException(401, "Invalid Google session.")

    # Find by firebase_uid or email; auto-create on first sign-in
    res = await db.execute(select(User).where(User.firebase_uid == uid))
    user = res.scalar_one_or_none()
    if not user:
        res = await db.execute(select(User).where(User.email == email))
        user = res.scalar_one_or_none()

    if not user:
        username = _re.sub(r"[^a-z0-9_\-]", "-", email.split("@", 1)[0].lower())[:32] or f"user-{secrets.token_hex(3)}"
        for _ in range(6):
            res = await db.execute(select(User).where(User.username == username))
            if not res.scalar_one_or_none():
                break
            username = f"{username[:26]}-{secrets.token_hex(2)}"
        user = User(
            username=username,
            email=email,
            full_name=sanitize_str(name, 128) if name else None,
            hashed_password=hash_password(secrets.token_urlsafe(32)),
            role=UserRole.merchant,
            api_key=generate_api_key(),
            api_key_active=True,
            webhook_secret_enc=encrypt_field(generate_webhook_secret()),
            requests_total=50,
            requests_used=0,
            balance_firo=0.0,
            firebase_uid=uid,
            email_verified=True,
        )
        db.add(user)
        await db.flush()
        try:
            from app.services.analytics_service import on_user_registered
            await on_user_registered(db, user)
        except Exception:
            pass
    else:
        if not user.firebase_uid:
            user.firebase_uid = uid
        user.email_verified = True
        db.add(user)

    if not user.is_active:
        raise HTTPException(401, "Account is inactive.")

    user.last_login_at = _now()
    await db.commit()

    await _issue_app_cookie(response, user, request)

    return {
        "user_id":  user.id,
        "username": user.username,
        "role":     user.role,
        "is_admin": user.role == UserRole.admin,
        "email":    user.email,
    }


# ─ 4. Forgot password ─
class ForgotIn(BaseModel):
    email:           EmailStr
    turnstile_token: str | None = None


@router.post("/forgot-password", dependencies=[Depends(rate_limit_moderate)])
async def fb_forgot(body: ForgotIn, request: Request, db: AsyncSession = Depends(get_db)):
    _require_not_onion(request)

    # Lenient Turnstile — do not block on hostname mismatch or missing
    # widget token. Per-IP cooldown + rate_limit_moderate already cap abuse.
    await _verify_ts_lenient(body.turnstile_token, _client_ip(request))

    email = str(body.email).strip().lower()
    await _cooldown_check(request, email)

    user = await _get_recent_user(db, email)
    # Always return the same generic response — no enumeration.
    # Legacy (non-Firebase) users are ALSO supported: their reset flow
    # lands in fb_reset() which updates the local hashed_password when
    # firebase_uid is absent. Without this branch, the "no email" bug
    # kept hitting every pre-Firebase account.
    if user:
        raw, tok_hash, expires_at = _generate_token_pair(AuthActionPurpose.reset_password)
        db.add(AuthActionToken(
            user_id=user.id, token_hash=tok_hash,
            purpose=AuthActionPurpose.reset_password, expires_at=expires_at,
        ))
        await db.commit()
        reset_url = _action_url(request, "resetPassword", raw)
        try:
            sent = await mailer.send_password_reset_email(email, reset_url)
            if not sent:
                logger.warning(f"[fb-forgot] reset email NOT sent to {email} — check SMTP logs")
        except Exception as exc:
            logger.error(f"[fb-forgot] reset email exception for {email}: {exc}")

    return {"message": GENERIC_MSG}


# ─ 5. Reset password (confirm) ─
class ResetIn(BaseModel):
    token:        str
    new_password: str


@router.post("/reset-password", dependencies=[Depends(rate_limit_auth)])
async def fb_reset(body: ResetIn, db: AsyncSession = Depends(get_db)):
    try:
        validate_password(body.new_password)
    except ValueError as e:
        raise HTTPException(422, str(e))

    tok = await _consume_token(db, body.token, AuthActionPurpose.reset_password)
    if not tok:
        raise HTTPException(400, "This link is invalid or has expired.")

    res = await db.execute(select(User).where(User.id == tok.user_id))
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(400, "This link is invalid or has expired.")

    # ─ Legacy (pre-Firebase) account ──
    # No firebase_uid → update the LOCAL hashed_password directly. These
    # users were created through the old /api/auth/register flow and
    # never had a Firebase record, so we must NOT call fb.set_password.
    if not user.firebase_uid:
        user.hashed_password = hash_password(body.new_password)
        user.password_changed_at = _now()
        db.add(user)
        await db.commit()
        return {"message": "Password updated. You can now log in."}

    # ─ Firebase-backed account ─
    try:
        fb.set_password(user.firebase_uid, body.new_password)
        fb.revoke_refresh_tokens(user.firebase_uid)
    except Exception as exc:
        raise HTTPException(500, "Unable to update password right now. Please try again.") from exc

    # Verify the password is actually usable for email/password sign-in.
    # For Google-only users (no password provider yet) `set_password` should
    # add the provider automatically — but any inconsistency would cause
    # the next sign-in to silently fail with "Invalid credentials". We surface
    # it here so the user is not stranded.
    try:
        fb_check = fb.verify_password_and_get_id_token(
            email=(user.email or "").strip().lower(),
            password=body.new_password,
        )
        if not fb_check:
            logger.warning(
                f"[fb-reset] set_password succeeded for uid={user.firebase_uid} "
                f"but REST signIn rejected — password/provider mismatch"
            )
            raise HTTPException(
                500,
                "Your password was saved but it is not usable for sign-in yet. "
                "Please request a new reset link and try again.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        # Network / transient — don't block the reset on diagnostic failures
        logger.info(f"[fb-reset] post-reset verification skipped: {exc}")

    user.password_changed_at = _now()
    user.email_verified = True   # implicitly proves email ownership
    db.add(user)
    await db.commit()

    return {"message": "Password updated. You can now log in."}


# ─ 6. Verify email ─
class VerifyIn(BaseModel):
    token: str


@router.post("/verify-email", dependencies=[Depends(rate_limit_moderate)])
async def fb_verify_email(body: VerifyIn, db: AsyncSession = Depends(get_db)):
    tok = await _consume_token(db, body.token, AuthActionPurpose.verify_email)
    if not tok:
        raise HTTPException(400, "This link is invalid or has expired.")

    res = await db.execute(select(User).where(User.id == tok.user_id))
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(400, "This link is invalid or has expired.")

    user.email_verified = True
    db.add(user)

    if user.firebase_uid:
        try:
            fb.set_email_verified(user.firebase_uid)
        except Exception:
            pass

    await db.commit()
    return {"message": "Verified. You can now log in."}


# ─ 7. Resend verification (generic) ─
class ResendIn(BaseModel):
    email:           EmailStr
    turnstile_token: str | None = None


@router.post("/resend-verification", dependencies=[Depends(rate_limit_moderate)])
async def fb_resend(body: ResendIn, request: Request, db: AsyncSession = Depends(get_db)):
    _require_not_onion(request)

    # Lenient Turnstile — see rationale in fb_forgot.
    await _verify_ts_lenient(body.turnstile_token, _client_ip(request))

    email = str(body.email).strip().lower()
    await _cooldown_check(request, email)

    user = await _get_recent_user(db, email)
    if user and user.firebase_uid and not user.email_verified:
        raw, tok_hash, expires_at = _generate_token_pair(AuthActionPurpose.verify_email)
        db.add(AuthActionToken(
            user_id=user.id, token_hash=tok_hash,
            purpose=AuthActionPurpose.verify_email, expires_at=expires_at,
        ))
        await db.commit()
        verify_url = _action_url(request, "verifyEmail", raw)
        await mailer.send_verification_email(email, verify_url)

    return {"message": GENERIC_MSG}
