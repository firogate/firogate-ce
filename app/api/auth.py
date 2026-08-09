import json
import secrets
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Response, Request
from pydantic import BaseModel
import re as _re
from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import (
    hash_password, verify_password, create_access_token, verify_access_token,
    generate_api_key, generate_webhook_secret, encrypt_field, decrypt_field,
    _cookie_kwargs,
)
from app.core.validators import validate_password, sanitize_str
from app.core.rate_limit import rate_limit_auth, rate_limit_moderate
from app.models.models import User, UserRole, LoginAttempt

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _trusted_client_ip(request: Request) -> str:
    """Client IP for session-binding/logging. Only trusts proxy headers
    (CF-Connecting-IP / X-Real-IP) when TRUST_PROXY_HEADERS is enabled —
    otherwise they're client-spoofable and must not influence auth logic."""
    from app.core.config import get_settings as _gs_ip
    if _gs_ip().TRUST_PROXY_HEADERS:
        ip = request.headers.get("CF-Connecting-IP", "").strip()
        if ip:
            return ip
        ip = request.headers.get("X-Real-IP", "").strip()
        if ip:
            return ip
    return request.client.host if request.client else ""


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
        ip = _trusted_client_ip(request)
        ua = request.headers.get("user-agent", "")
        if not verify_session_binding(token, ip, ua):
            raise HTTPException(401, "Session expired please log in again")

    res = await db.execute(select(User).where(User.id == uid))
    u = res.scalar_one_or_none()
    if not u or not u.is_active:
        raise HTTPException(401, "User not found or inactive")

    # Operator-email auto-promotion (mirrors the logic in app.api.users.get_current_user).
    # /api/auth/me is the single source of truth the panel uses to check
    # role, so if we don't promote here the panel will see role=merchant
    # even for a user listed in OPERATOR_EMAILS, and kick them back to /dashboard.
    from app.core.config import get_settings as _gs
    _s = _gs()
    if u.role != UserRole.operator and (_s.is_operator_email(u.email) or _s.is_operator_username(u.username)):
        u.role = UserRole.operator
        db.add(u)
        await db.commit()
        await db.refresh(u)
    return u


class RegisterIn(BaseModel):
    username:         str | None = None
    email:            str | None = None   # optional privacy-first
    password:         str
    app_name:         str
    full_name:        str | None = None


@router.post("/register", status_code=201, dependencies=[Depends(rate_limit_moderate)])
async def register(body: RegisterIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    from app.core.system_settings import hide_auth_pages_enabled
    if await hide_auth_pages_enabled(db):
        raise HTTPException(404)

    from app.services.privacy_service import is_onion_request
    from app.api.users import clean_app_name

    try:
        validate_password(body.password)
        if body.full_name:
            body.full_name = sanitize_str(body.full_name, 128)
    except ValueError as e:
        raise HTTPException(422, str(e))

    app_name = clean_app_name(body.app_name)

    clean_email: str | None = None
    if body.email:
        raw_email = body.email.strip().lower()
        # Basic RFC-safe check, no external library needed
        if not _re.match(r'^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{1,63}$', raw_email):
            raise HTTPException(422, "Invalid email address format.")
        if len(raw_email) > 320:
            raise HTTPException(422, "Email address is too long.")
        clean_email = raw_email

    if body.username:
        candidate = body.username.strip().lower()
        if not _re.match(r'^[a-z0-9_\-]{3,32}$', candidate):
            raise HTTPException(422, "Username must be 3-32 characters: letters, numbers, - and _ only.")
        res = await db.execute(select(User).where(User.username == candidate))
        if res.scalar_one_or_none():
            raise HTTPException(409, "Username already taken")
        username = candidate
    else:
        username = (clean_email.split("@", 1)[0] if clean_email else f"user-{secrets.token_hex(3)}").lower()
        username = _re.sub(r"[^a-z0-9_\-]", "-", username)[:32] or f"user-{secrets.token_hex(3)}"
        for _ in range(6):
            res = await db.execute(select(User).where(User.username == username))
            if not res.scalar_one_or_none():
                break
            username = f"{username[:26]}-{secrets.token_hex(2)}"

    if clean_email:
        res = await db.execute(select(User).where(User.email == clean_email))
        if res.scalar_one_or_none():
            raise HTTPException(409, "Email already registered")

    is_onion = is_onion_request(request)
    ip = "" if is_onion else _trusted_client_ip(request)

    webhook_secret = generate_webhook_secret()
    user = User(
        username=username,
        email=clean_email,   # None if not provided
        full_name=body.full_name,
        hashed_password=hash_password(body.password),  # always stored, used as Tor login fallback
        role=UserRole.merchant,
        api_key=generate_api_key(),
        api_key_active=True,
        webhook_secret_enc=encrypt_field(webhook_secret),
        privacy_mode=is_onion,
        created_via_onion=is_onion,
        merchant_setup_unlocked=True,
        app_name=app_name,
        app_name_locked=True,
    )
    db.add(user)
    await db.flush()
    user_id = user.id
    await db.commit()

    from app.services.analytics_service import on_user_registered
    await on_user_registered(db, user)

    token = create_access_token(
        user_id,
        ip=ip if not is_onion else "",
        ua=request.headers.get("user-agent", "") if not is_onion else "",
        privacy=is_onion,
    )

    # Set httponly cookie same as login, so the browser is authenticated
    # immediately. Without this, Tor Browser gets the token in JSON but never
    # stores it, causing an instant 401 on the next request.
    from app.core.security import _cookie_kwargs
    response.set_cookie("access_token", token, **_cookie_kwargs(request))

    result = {"message": "Account created", "user_id": user_id}
    if not clean_email:
        result["username"] = username
    return result


class LoginIn(BaseModel):
    username:  str
    password:  str
    totp_code: str | None = None


@router.post("/login", dependencies=[Depends(rate_limit_auth)])
async def login(body: LoginIn, response: Response, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.privacy_service import is_onion_request, get_client_ip
    from app.core.privacy_middleware import update_privacy_state_for_user
    
    is_onion = is_onion_request(request)
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

    # Per-account lockout backstop against brute force even if the IP-based
    # rate limit is evaded (rotating proxies, spoofable X-Forwarded-For, etc).
    # 10 failed attempts against the SAME username within 15 minutes locks
    # that account out for 15 minutes, independent of source IP.
    if user and not is_onion:
        from datetime import timedelta as _td
        _window_start = datetime.now(timezone.utc) - _td(minutes=15)
        _fail_count_res = await db.execute(
            select(func.count()).select_from(LoginAttempt).where(
                LoginAttempt.username == body.username,
                LoginAttempt.success == False,
                LoginAttempt.created_at >= _window_start,
            )
        )
        _fail_count = _fail_count_res.scalar() or 0
        if _fail_count >= 10:
            raise HTTPException(
                429,
                "Too many failed login attempts for this account. Try again in 15 minutes."
            )

    # Firebase-backed account? Verify the password via Firebase (the source of
    # truth for it). The local hashed_password for these accounts is an
    # unusable random value by design, so we MUST NOT compare it here.
    # EXCEPTION: On Tor/onion, Firebase REST API is unreachable fall back to
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
        # Tor path: Firebase is unreachable, use local password hash if available
        if user.hashed_password and not user.hashed_password.startswith("UNUSABLE"):
            pw_ok = bool(verify_password(body.password, user.hashed_password) and user.is_active)
        else:
            # Firebase-only account with no local password cannot log in via Tor.
            # Return a helpful error instead of a confusing 401.
            raise HTTPException(
                403,
                "This account uses Google/Firebase sign-in which is not available over Tor. "
                "Please register a new account with username + password while on Tor."
            )
    else:
        pw_ok = bool(user and verify_password(body.password, user.hashed_password) and user.is_active)

    if not pw_ok:
        if not is_onion and not (user and getattr(user, 'privacy_mode', False)):
            db.add(LoginAttempt(username=body.username, ip_address=ip, success=False))
        await db.commit()
        raise HTTPException(401, "Invalid credentials")

    if user.totp_enabled and user.totp_secret_enc:
        code = (body.totp_code or "").strip()
        if not code:
            if not is_onion and not user.privacy_mode:
                db.add(LoginAttempt(username=body.username, ip_address=ip, success=False))
            await db.commit()
            raise HTTPException(403, "2FA code required")

        from app.core.totp import verify_totp_code, decrypt_totp_secret, current_totp_step
        from app.core.security import get_fernet
        try:
            fernet = get_fernet()
            secret = decrypt_totp_secret(user.totp_secret_enc, fernet)
            if not secret:
                raise ValueError("Failed to decrypt")
        except Exception:
            raise HTTPException(500, "2FA configuration error")

        totp_ok = verify_totp_code(secret, code, last_step=user.totp_last_step)
        backup_ok = (not totp_ok) and _use_backup_code(user, code)

        if not totp_ok and not backup_ok:
            if not is_onion and not user.privacy_mode:
                db.add(LoginAttempt(username=body.username, ip_address=ip, success=False))
            await db.commit()
            raise HTTPException(401, "Invalid 2FA code")

        if totp_ok:
            step = current_totp_step(secret, code)
            if step is not None:
                user.totp_last_step = step

    from app.core.security import record_login_meta
    record_login_meta(user, request)

    # Session binding: embed IP + UA fingerprint for clearnet non-Firebase users.
    # Firebase users skip it Firebase revocation already invalidates sessions
    # on password change, and behind a reverse proxy the IP extracted at
    # cookie issue vs verify can subtly differ, locking legitimate users out.
    ua = request.headers.get("user-agent", "")
    is_firebase = bool(getattr(user, "firebase_uid", None))
    is_privacy = is_onion or getattr(user, 'privacy_mode', False) or is_firebase
    token = create_access_token(user.id, ip=ip, ua=ua, privacy=is_privacy)

    if not is_onion and not user.privacy_mode:
        db.add(LoginAttempt(username=body.username, ip_address=ip, success=True))
    await db.commit()

    response.set_cookie("access_token", token, **_cookie_kwargs(request))

    privacy_state = update_privacy_state_for_user(request, user)

    return {
        "access_token":      token,
        "token_type":        "bearer",
        "user_id":           user.id,
        "username":          user.username,
        "role":              user.role,
        "is_operator":          user.role == UserRole.operator,
        "has_2fa":           bool(user.totp_enabled),
        "requires_2fa":      False,
        "operator_2fa_granted": (
            user.role == UserRole.operator
            and user.totp_enabled
            and bool((body.totp_code or "").strip())
        ),
        "privacy_mode":      user.privacy_mode,
        "is_onion_session":  is_onion,
        "privacy_warning":   privacy_state.get("access_warning"),
    }


@router.post("/register-number", status_code=201, dependencies=[Depends(rate_limit_moderate)])
async def register_number(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    from app.core.system_settings import hide_auth_pages_enabled
    if await hide_auth_pages_enabled(db):
        raise HTTPException(404)

    from app.core.security import (
        generate_account_number, format_account_number,
        account_number_lookup_key,
    )
    from app.services.privacy_service import is_onion_request

    is_onion = is_onion_request(request)
    ip = "" if is_onion else _trusted_client_ip(request)

    raw_number = generate_account_number()
    lookup = account_number_lookup_key(raw_number)

    username = f"user-{secrets.token_hex(4)}"
    for _ in range(6):
        res = await db.execute(select(User).where(User.username == username))
        if not res.scalar_one_or_none():
            break
        username = f"user-{secrets.token_hex(4)}"

    webhook_secret = generate_webhook_secret()
    user = User(
        username=username,
        email=None,
        hashed_password="UNUSABLE-" + secrets.token_hex(32),
        account_number_hash=hash_password(raw_number),
        account_number_lookup=lookup,
        auth_method="account_number",
        role=UserRole.merchant,
        api_key=generate_api_key(),
        api_key_active=True,
        webhook_secret_enc=encrypt_field(webhook_secret),
        privacy_mode=is_onion,
        created_via_onion=is_onion,
        merchant_setup_unlocked=True,
        app_name="Payment Gateway",
        app_name_locked=False,
    )
    db.add(user)
    await db.flush()
    user_id = user.id
    await db.commit()

    from app.services.analytics_service import on_user_registered
    await on_user_registered(db, user)

    token = create_access_token(
        user_id,
        ip=ip if not is_onion else "",
        ua=request.headers.get("user-agent", "") if not is_onion else "",
        privacy=True,
    )
    response.set_cookie("access_token", token, **_cookie_kwargs(request))

    return {
        "message": "Account created",
        "user_id": user_id,
        "account_number": format_account_number(raw_number),
    }


class LoginNumberIn(BaseModel):
    account_number: str


@router.post("/login-number", dependencies=[Depends(rate_limit_auth)])
async def login_number(body: LoginNumberIn, response: Response, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.privacy_service import is_onion_request
    from app.core.privacy_middleware import update_privacy_state_for_user
    from app.core.security import normalize_account_number, account_number_lookup_key, record_login_meta

    is_onion = is_onion_request(request)
    ip = "" if is_onion else _trusted_client_ip(request)

    normalized = normalize_account_number(body.account_number)
    if len(normalized) != 16:
        raise HTTPException(401, "Invalid account number")

    if not is_onion:
        from datetime import timedelta as _td
        _window_start = datetime.now(timezone.utc) - _td(minutes=15)
        _fail_count_res = await db.execute(
            select(func.count()).select_from(LoginAttempt).where(
                LoginAttempt.username == "acct-number",
                LoginAttempt.ip_address == ip,
                LoginAttempt.success == False,
                LoginAttempt.created_at >= _window_start,
            )
        )
        if (_fail_count_res.scalar() or 0) >= 10:
            raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")

    lookup = account_number_lookup_key(normalized)
    res = await db.execute(select(User).where(User.account_number_lookup == lookup))
    user = res.scalar_one_or_none()

    pw_ok = bool(
        user
        and user.account_number_hash
        and verify_password(normalized, user.account_number_hash)
        and user.is_active
    )

    if not pw_ok:
        if not is_onion:
            db.add(LoginAttempt(username="acct-number", ip_address=ip, success=False))
            await db.commit()
        raise HTTPException(401, "Invalid account number")

    record_login_meta(user, request)

    ua = request.headers.get("user-agent", "")
    token = create_access_token(user.id, ip=ip, ua=ua, privacy=True)

    if not is_onion:
        db.add(LoginAttempt(username="acct-number", ip_address=ip, success=True))
    await db.commit()

    response.set_cookie("access_token", token, **_cookie_kwargs(request))
    privacy_state = update_privacy_state_for_user(request, user)

    return {
        "access_token":      token,
        "token_type":        "bearer",
        "user_id":           user.id,
        "username":          user.username,
        "role":              user.role,
        "is_operator":       user.role == UserRole.operator,
        "has_2fa":           bool(user.totp_enabled),
        "privacy_mode":      True,
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
        "is_operator":        u.role == UserRole.operator,
        "plan":            u.plan,
        "requests_total":  u.requests_total,
        "requests_used":   u.requests_used,
        "requests_left":   max(0, (u.requests_total or 0) - (u.requests_used or 0)),
        "lifetime_gross_sales_firo":   round(u.lifetime_gross_sales_firo or 0, 8),
        "lifetime_received_firo":      round(u.lifetime_received_firo or 0, 8),
        "lifetime_confirmed_payments": int(u.lifetime_confirmed_payments or 0),
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

    from app.core.totp import verify_totp_code, decrypt_totp_secret, generate_recovery_codes, hash_recovery_code, current_totp_step
    from app.core.security import get_fernet
    try:
        fernet = get_fernet()
        secret = decrypt_totp_secret(u.totp_secret_enc, fernet)
        if not secret:
            raise ValueError("Failed to decrypt TOTP secret")
    except Exception:
        raise HTTPException(500, "2FA secret error")

    if not verify_totp_code(secret, code, last_step=u.totp_last_step):
        raise HTTPException(400, "Invalid code check your authenticator app clock")

    step = current_totp_step(secret, code)
    if step is not None:
        u.totp_last_step = step

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
        from app.core.totp import verify_totp_code, decrypt_totp_secret, current_totp_step
        from app.core.security import get_fernet
        try:
            fernet = get_fernet()
            secret = decrypt_totp_secret(u.totp_secret_enc, fernet)
        except Exception:
            raise HTTPException(500, "2FA secret error")
        totp_ok = verify_totp_code(secret, totp_code, last_step=u.totp_last_step)
        if not totp_ok and not _use_backup_code(u, totp_code):
            raise HTTPException(400, "Invalid 2FA code")
        if totp_ok:
            step = current_totp_step(secret, totp_code)
            if step is not None:
                u.totp_last_step = step

    u.totp_enabled    = False
    u.totp_secret_enc = None
    u.totp_backup_enc = None
    db.add(u)
    await db.commit()
    return {"message": "2FA disabled"}


class RefreshIn(BaseModel):
    refresh_token: str | None = None  # accepted but unused - we refresh from the access token


@router.post("/refresh")
async def refresh_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Re-issue a fresh access token for a valid (or recently expired) session.
    Accepts the token via Authorization header, cookie, or request body.
    No separate refresh-token DB table needed - the access token itself is
    re-verified with a grace window to allow seamless background renewal.
    """
    token = (
        request.cookies.get("access_token")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )

    if not token:
        try:
            body = await request.json()
            token = body.get("refresh_token") or body.get("access_token") or ""
        except Exception:
            token = ""

    if not token:
        raise HTTPException(401, "No token provided")

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

    res = await db.execute(select(User).where(User.id == uid))
    u = res.scalar_one_or_none()
    if not u or not u.is_active:
        raise HTTPException(401, "User not found or disabled")

    from app.services.privacy_service import is_onion_request
    _ref_ip = _trusted_client_ip(request)
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
