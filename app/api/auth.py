import json
import secrets
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Response, Request
from pydantic import BaseModel, EmailStr
import re as _re
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import (
    hash_password, verify_password, create_access_token, verify_access_token,
    generate_api_key, generate_webhook_secret, encrypt_field, decrypt_field,
    _cookie_kwargs,
)
from app.core.validators import validate_username, validate_password, sanitize_str
from app.core.rate_limit import rate_limit_auth, rate_limit_moderate
from app.models.models import User, UserRole, LoginAttempt

router = APIRouter(prefix="/api/auth", tags=["auth"])


async def _get_current_user(request: Request, db: AsyncSession) -> User:
    token = (request.cookies.get("access_token") or
             request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
    uid = verify_access_token(token)
    if not uid:
        raise HTTPException(401, "Not authenticated")

    # Session binding check: verify IP + UA fingerprint for clearnet sessions
    from app.core.security import verify_session_binding
    from app.services.privacy_service import is_onion_request
    if not is_onion_request(request):
        ip = request.headers.get("CF-Connecting-IP", "").strip()
        if not ip:
            ip = request.headers.get("X-Real-IP", "").strip()
        if not ip:
            ip = request.client.host if request.client else ""
        ua = request.headers.get("user-agent", "")
        if not verify_session_binding(token, ip, ua):
            raise HTTPException(401, "Session expired — please log in again")

    res = await db.execute(select(User).where(User.id == uid))
    u = res.scalar_one_or_none()
    if not u or not u.is_active:
        raise HTTPException(401, "User not found or inactive")

    # Admin-email auto-promotion (mirrors the logic in app.api.users.get_current_user).
    # /api/auth/me is the single source of truth the admin page uses to check
    # role, so if we don't promote here the admin panel will see role=merchant
    # even for a user listed in OPERATOR_EMAILS, and kick them back to /dashboard.
    from app.core.config import get_settings as _gs
    if u.role != UserRole.admin and _gs().is_admin_email(u.email):
        u.role = UserRole.admin
        db.add(u)
        await db.commit()
        await db.refresh(u)
    return u


def _generate_recovery_codes(count: int = 8) -> list[str]:
    # DEPRECATED — recovery codes removed; kept as a compatibility stub.
    return []


class RegisterIn(BaseModel):
    username:         str
    email:            str | None = None   # optional — privacy-first
    password:         str
    full_name:        str | None = None
    agreed_to_terms:  bool = False        # must be True — enforced server-side


@router.post("/register", status_code=201, dependencies=[Depends(rate_limit_moderate)])
async def register(body: RegisterIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    # Server-side enforcement: terms must be explicitly accepted.
    # This is a second layer — the frontend also enforces it.
    if not body.agreed_to_terms:
        raise HTTPException(400, "You must agree to the Terms of Service and Privacy Policy to register.")

    from app.services.privacy_service import is_onion_request
    
    try:
        body.username = validate_username(body.username)
        validate_password(body.password)
        if body.full_name:
            body.full_name = sanitize_str(body.full_name, 128)
    except ValueError as e:
        raise HTTPException(422, str(e))

    # Validate email format only if provided
    clean_email: str | None = None
    if body.email:
        raw_email = body.email.strip().lower()
        # Basic RFC-safe check — no external library needed
        if not _re.match(r'^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{1,63}$', raw_email):
            raise HTTPException(422, "Invalid email address format.")
        if len(raw_email) > 320:
            raise HTTPException(422, "Email address is too long.")
        clean_email = raw_email

    res = await db.execute(select(User).where(User.username == body.username))
    if res.scalar_one_or_none():
        raise HTTPException(409, "Username already taken")

    # Only check email uniqueness if one was provided
    if clean_email:
        res = await db.execute(select(User).where(User.email == clean_email))
        if res.scalar_one_or_none():
            raise HTTPException(409, "Email already registered")

    # Detect if registering via Tor/onion
    is_onion = is_onion_request(request)
    # Get client IP for session binding (blank for Tor/onion users)
    ip = ""
    if not is_onion:
        ip = request.headers.get("CF-Connecting-IP", "").strip()
        if not ip:
            ip = request.headers.get("X-Real-IP", "").strip()
        if not ip:
            ip = request.client.host if request.client else ""

    webhook_secret = generate_webhook_secret()
    user = User(
        username=body.username,
        email=clean_email,   # None if not provided
        full_name=body.full_name,
        hashed_password=hash_password(body.password),  # always stored — Tor login fallback
        role=UserRole.merchant,
        api_key=generate_api_key(),
        api_key_active=True,
        webhook_secret_enc=encrypt_field(webhook_secret),
        requests_total=50,
        requests_used=0,
        balance_firo=0.0,
        # Privacy mode - set if registering via onion
        privacy_mode=is_onion,
        created_via_onion=is_onion,
    )
    db.add(user)
    await db.flush()
    user_id = user.id
    await db.commit()
    
    # Update analytics stats for new user
    from app.services.analytics_service import on_user_registered
    await on_user_registered(db, user)

    token = create_access_token(
        user_id,
        ip=ip if not is_onion else "",
        ua=request.headers.get("user-agent", "") if not is_onion else "",
        privacy=is_onion,
    )

    # Set httponly cookie — same as login, so browser is authenticated immediately.
    # Without this, Tor Browser gets the token in JSON but never stores it,
    # causing an instant 401 on the next request and kicking the user out.
    from app.core.security import _cookie_kwargs
    response.set_cookie("access_token", token, **_cookie_kwargs(request))

    return {
        "message": "Account created",
        "user_id": user_id,
        "access_token": token,
        "token": token,  # backward compatibility
    }


class LoginIn(BaseModel):
    username:  str
    password:  str
    totp_code: str | None = None


@router.post("/login", dependencies=[Depends(rate_limit_auth)])
async def login(body: LoginIn, response: Response, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.privacy_service import is_onion_request, get_client_ip, check_privacy_mode_access
    from app.core.privacy_middleware import update_privacy_state_for_user
    
    is_onion = is_onion_request(request)
    # Only log IP for non-privacy requests
    ip = get_client_ip(request, privacy_mode=is_onion) or "privacy"
    identifier = body.username.strip().lower()


    res = await db.execute(
        select(User).where(
            or_(
                User.username == identifier,
                User.email    == identifier,
            )
        )
    )
    user = res.scalar_one_or_none()

    # Firebase-backed account? Verify the password via Firebase (the source of
    # truth for it). The local hashed_password for these accounts is an
    # unusable random value by design, so we MUST NOT compare it here.
    # EXCEPTION: On Tor/onion, Firebase REST API is unreachable — fall back to
    # local hashed_password if one exists (set during onion registration).
    if user and user.firebase_uid and not is_onion:
        from app.core import firebase_auth as _fb
        fb_result = None
        try:
            fb_result = _fb.verify_password_and_get_id_token(
                email=(user.email or "").strip().lower(),
                password=body.password,
            )
        except Exception:
            fb_result = None
        pw_ok = bool(fb_result and user.is_active)
    elif user and user.firebase_uid and is_onion:
        # Tor path: Firebase unreachable — use local password hash if available
        if user.hashed_password and not user.hashed_password.startswith("UNUSABLE"):
            pw_ok = bool(verify_password(body.password, user.hashed_password) and user.is_active)
        else:
            # Firebase-only account with no local password — cannot log in via Tor
            # Return a helpful error instead of a confusing 401
            raise HTTPException(
                403,
                "This account uses Google/Firebase sign-in which is not available over Tor. "
                "Please register a new account with username + password while on Tor."
            )
    else:
        pw_ok = bool(user and verify_password(body.password, user.hashed_password) and user.is_active)

    if not pw_ok:
        # Log attempt with privacy awareness
        if not is_onion and not (user and getattr(user, 'privacy_mode', False)):
            db.add(LoginAttempt(username=body.username, ip_address=ip, success=False))
        await db.commit()
        raise HTTPException(401, "Invalid credentials")

    # Firebase users must have verified email before we hand out a session
    if user.firebase_uid and not user.email_verified:
        raise HTTPException(403, "Please verify your email first.")


    if user.totp_enabled and user.totp_secret_enc:
        code = (body.totp_code or "").strip()
        if not code:
            if not is_onion and not user.privacy_mode:
                db.add(LoginAttempt(username=body.username, ip_address=ip, success=False))
            await db.commit()
            raise HTTPException(403, "2FA code required")

        from app.core.totp import verify_totp_code, decrypt_totp_secret
        from app.core.security import get_fernet
        try:
            fernet = get_fernet()
            secret = decrypt_totp_secret(user.totp_secret_enc, fernet)
            if not secret:
                raise ValueError("Failed to decrypt")
        except Exception:
            raise HTTPException(500, "2FA configuration error")

        totp_ok = verify_totp_code(secret, code)
        backup_ok = (not totp_ok) and _use_backup_code(user, code)

        if not totp_ok and not backup_ok:
            if not is_onion and not user.privacy_mode:
                db.add(LoginAttempt(username=body.username, ip_address=ip, success=False))
            await db.commit()
            raise HTTPException(401, "Invalid 2FA code")

    user.last_login_at = datetime.now(timezone.utc)

    # Session binding: embed IP + UA fingerprint for clearnet non-Firebase users.
    # Firebase users skip it — Firebase revocation already invalidates sessions
    # on password change, and behind Cloudflare + nginx the IP extracted at
    # cookie issue vs verify can subtly differ, locking legitimate users out.
    ua = request.headers.get("user-agent", "")
    is_firebase = bool(getattr(user, "firebase_uid", None))
    is_privacy = is_onion or getattr(user, 'privacy_mode', False) or is_firebase
    token = create_access_token(user.id, ip=ip, ua=ua, privacy=is_privacy)
    
    # Log successful attempt with privacy awareness
    if not is_onion and not user.privacy_mode:
        db.add(LoginAttempt(username=body.username, ip_address=ip, success=True))
    await db.commit()

    response.set_cookie("access_token", token, **_cookie_kwargs(request))

    # Update privacy state for this session
    privacy_state = update_privacy_state_for_user(request, user)

    if user.role == UserRole.admin and user.totp_enabled and (body.totp_code or "").strip():
        try:
            from app.enterprise.core.admin_guard import mark_admin_2fa_verified
            mark_admin_2fa_verified(token)
        except ImportError:
            pass  # Community Edition — no admin 2FA tracking

    return {
        "access_token":      token,
        "token_type":        "bearer",
        "user_id":           user.id,
        "username":          user.username,
        "role":              user.role,
        "is_admin":          user.role == UserRole.admin,
        "has_2fa":           bool(user.totp_enabled),
        "requires_2fa":      False,
        "admin_2fa_granted": (
            user.role == UserRole.admin
            and user.totp_enabled
            and bool((body.totp_code or "").strip())
        ),
        # Privacy mode info
        "privacy_mode":      user.privacy_mode,
        "is_onion_session":  is_onion,
        "privacy_warning":   privacy_state.get("access_warning"),
    }


def _use_backup_code(user: User, code: str) -> bool:
    if not user.totp_backup_enc:
        return False
    try:
        codes: list[str] = json.loads(decrypt_field(user.totp_backup_enc))
    except Exception:
        return False
    normalized = code.strip().upper().replace(" ", "").replace("-", "")
    for i, c in enumerate(codes):
        if c.upper().replace("-", "").replace(" ", "") == normalized:
            codes.pop(i)
            user.totp_backup_enc = encrypt_field(json.dumps(codes))
            return True
    return False


class RecoveryStep1In(BaseModel):
    username_or_email: str


@router.post("/recovery/verify-code")
async def recovery_verify_code(body: RecoveryStep1In, db: AsyncSession = Depends(get_db)):
    # Recovery-code flow removed. Returns a generic message pointing users at the new email-based reset.
    raise HTTPException(410, "Recovery codes have been removed. Use the forgot-password flow instead.")


@router.post("/recovery/reset-password")
async def recovery_reset_password():
    raise HTTPException(410, "Recovery codes have been removed. Use the forgot-password flow instead.")


@router.post("/recovery/regenerate-codes")
async def regenerate_recovery_codes():
    raise HTTPException(410, "Recovery codes have been removed.")


@router.post("/logout")
async def logout(request: Request, response: Response):
    from app.core.security import _derive_cookie_domain
    from app.core.config import get_settings as _gs
    s = _gs()
    dom = (s.COOKIE_DOMAIN or "").strip() or _derive_cookie_domain(s.BASE_URL)
    if dom:
        response.delete_cookie("access_token", path="/", domain=dom)
    else:
        response.delete_cookie("access_token", path="/")
    return {"message": "Logged out"}


@router.get("/me")
async def me(request: Request, db: AsyncSession = Depends(get_db)):
    u = await _get_current_user(request, db)
    return {
        "id":              u.id,
        "username":        u.username,
        "email":           u.email,
        "email_verified":  bool(getattr(u, 'email_verified', False)),
        "full_name":       getattr(u, 'full_name', None),
        "role":            u.role,
        "is_admin":        u.role == UserRole.admin,
        "plan":            u.plan,
        "requests_total":  u.requests_total,
        "requests_used":   u.requests_used,
        "requests_left":   max(0, (u.requests_total or 0) - (u.requests_used or 0)),
        "balance_firo":    round(u.balance_firo or 0, 8),
        "balance_pending": round(u.balance_pending or 0, 8),
        "total_earned":    round(u.total_earned_firo or 0, 8),
        "plan_expires_at": u.plan_expires_at.isoformat() if u.plan_expires_at else None,
        "api_key":         u.api_key,
        "webhook_url":     u.webhook_url,
        "has_2fa":         bool(u.totp_enabled),
    }


@router.get("/2fa/status")
async def tfa_status(request: Request, db: AsyncSession = Depends(get_db)):
    u = await _get_current_user(request, db)
    backup_count = 0
    if u.totp_backup_enc:
        try:
            backup_count = len(json.loads(decrypt_field(u.totp_backup_enc)))
        except Exception:
            pass
    return {
        "enabled":                bool(u.totp_enabled),
        "backup_codes_remaining": backup_count,
    }


@router.post("/2fa/setup")
async def tfa_setup(request: Request, db: AsyncSession = Depends(get_db)):
    u = await _get_current_user(request, db)
    from app.core.totp import init_totp_for_user
    from app.core.security import get_fernet

    fernet    = get_fernet()
    totp_data = init_totp_for_user(u.username, fernet)

    u.totp_secret_enc = totp_data['secret_encrypted']
    u.totp_enabled    = False
    db.add(u)
    await db.commit()

    return {
        "secret":         totp_data['secret'],
        "qr":             totp_data['qr_code'],
        "recovery_codes": totp_data.get('recovery_codes', []),
    }


@router.post("/2fa/verify")
async def tfa_verify(request: Request, db: AsyncSession = Depends(get_db)):
    u = await _get_current_user(request, db)
    body = await request.json()
    code = str(body.get("code", "")).strip()

    if not u.totp_secret_enc:
        raise HTTPException(400, "Run 2FA setup first")

    from app.core.totp import verify_totp_code, decrypt_totp_secret, generate_recovery_codes, hash_recovery_code
    from app.core.security import get_fernet
    try:
        fernet = get_fernet()
        secret = decrypt_totp_secret(u.totp_secret_enc, fernet)
        if not secret:
            raise ValueError("Failed to decrypt TOTP secret")
    except Exception:
        raise HTTPException(500, "2FA secret error")

    if not verify_totp_code(secret, code):
        raise HTTPException(400, "Invalid code — check your authenticator app clock")

    backup_codes        = generate_recovery_codes(8)
    backup_codes_hashed = [hash_recovery_code(c) for c in backup_codes]
    u.totp_enabled    = True
    u.totp_backup_enc = encrypt_field(json.dumps(backup_codes_hashed))
    db.add(u)
    await db.commit()

    return {
        "message":      "2FA enabled successfully",
        "backup_codes": backup_codes,
    }


@router.post("/2fa/disable")
async def tfa_disable(request: Request, db: AsyncSession = Depends(get_db)):
    u = await _get_current_user(request, db)
    body = await request.json()
    password  = body.get("password", "")
    totp_code = str(body.get("totp_code", "")).strip()

    if not verify_password(password, u.hashed_password):
        raise HTTPException(400, "Incorrect password")

    if u.totp_enabled and u.totp_secret_enc:
        from app.core.totp import verify_totp_code, decrypt_totp_secret
        from app.core.security import get_fernet
        try:
            fernet = get_fernet()
            secret = decrypt_totp_secret(u.totp_secret_enc, fernet)
        except Exception:
            raise HTTPException(500, "2FA secret error")
        if not verify_totp_code(secret, totp_code) and not _use_backup_code(u, totp_code):
            raise HTTPException(400, "Invalid 2FA code")

    u.totp_enabled    = False
    u.totp_secret_enc = None
    u.totp_backup_enc = None
    db.add(u)
    await db.commit()
    return {"message": "2FA disabled"}


class RefreshIn(BaseModel):
    refresh_token: str | None = None  # accepted but unused — we refresh from access token


@router.post("/refresh")
async def refresh_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Re-issue a fresh access token for a valid (or recently expired) session.
    Accepts the token via Authorization header, cookie, or request body.
    No separate refresh-token DB table needed — the access token itself is
    re-verified with a grace window to allow seamless background renewal.
    """
    # Extract token from header, cookie, or body
    token = (
        request.cookies.get("access_token")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )

    # Also accept token from body for clients that send it explicitly
    if not token:
        try:
            body = await request.json()
            token = body.get("refresh_token") or body.get("access_token") or ""
        except Exception:
            token = ""

    if not token:
        raise HTTPException(401, "No token provided")

    # Try normal verification first
    uid = verify_access_token(token)

    # If expired, attempt grace-window decode (leeway 30 min)
    if not uid:
        from jose import jwt, JWTError
        from app.core.config import get_settings as _gs
        try:
            payload = jwt.decode(
                token,
                _gs().SECRET_KEY,
                algorithms=["HS256"],
                options={"leeway": 1800},  # 30 min grace after expiry
            )
            uid = payload.get("sub")
        except JWTError:
            uid = None

    if not uid:
        raise HTTPException(401, "Token invalid or too old to refresh")

    # Load user and verify still active
    res = await db.execute(select(User).where(User.id == uid))
    u = res.scalar_one_or_none()
    if not u or not u.is_active:
        raise HTTPException(401, "User not found or disabled")

    # Issue fresh token with session binding
    from app.services.privacy_service import is_onion_request
    _ref_ip = request.headers.get("CF-Connecting-IP", "").strip()
    if not _ref_ip:
        _ref_ip = request.headers.get("X-Real-IP", "").strip()
    if not _ref_ip:
        _ref_ip = request.client.host if request.client else ""
    _ref_ua = request.headers.get("user-agent", "")
    _ref_priv = is_onion_request(request) or getattr(u, 'privacy_mode', False)
    new_token = create_access_token(u.id, ip=_ref_ip, ua=_ref_ua, privacy=_ref_priv)

    from fastapi.responses import JSONResponse
    response = JSONResponse(content={
        "access_token":  new_token,
        "refresh_token": new_token,   # same token returned as refresh_token for client compatibility
        "token_type":    "bearer",
        "user_id":       u.id,
    })
    response.set_cookie(
        "access_token", new_token, **_cookie_kwargs(request),
    )
    return response
