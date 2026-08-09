"""
Firebase-backed Google sign-in.

Flow:
  * Google sign-in happens via Firebase (client SDK + Admin, auto-verified).
  * After a successful Google step, we exchange the Firebase ID token for
    the existing JWT httponly cookie (same format as app.api.auth.login) so
    that ALL downstream features keep working unchanged: TOTP 2FA, panel
    guard, API keys, webhooks, session binding, Tor privacy mode, etc.

Username+password registration/login use app.api.auth instead (no email
required). Firebase is only used here for the optional "Sign in with
Google" convenience button.

Endpoints:
  POST /api/auth/fb/google
  GET  /api/auth/fb/config        (safe public config for frontend SDK)
"""
from __future__ import annotations

import re as _re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import rate_limit_auth
from app.core.security import (
    create_access_token, hash_password, generate_api_key, generate_webhook_secret,
    encrypt_field,
)
from app.core.validators import sanitize_str
from app.core import firebase_auth as fb
from app.models.models import User, UserRole

router = APIRouter(prefix="/api/auth/fb", tags=["auth-firebase"])


def _client_ip(request: Request) -> str:
    if get_settings().TRUST_PROXY_HEADERS:
        ip = request.headers.get("CF-Connecting-IP", "").strip()
        if not ip:
            ip = request.headers.get("X-Real-IP", "").strip()
        if not ip:
            ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if ip:
            return ip
    return request.client.host if request.client else ""


async def _issue_app_cookie(response: Response, user: User, request: Request) -> str:
    """Issue the app's own JWT cookie (same as legacy login) after Firebase auth.

    For Firebase-backed accounts we skip the IP/UA session-binding fingerprint:
    behind a reverse proxy the extracted IP can subtly differ between the
    request that issues the cookie and the request that verifies it, which
    would break every session for no security benefit Firebase already
    revokes refresh tokens on password change.
    """
    from app.services.privacy_service import is_onion_request
    from app.core.security import _cookie_kwargs
    is_onion = is_onion_request(request)
    is_third_party = bool(getattr(user, "firebase_uid", None)) or bool(getattr(user, "telegram_id", None))
    privacy = is_onion or bool(user.privacy_mode) or is_third_party
    ip = "" if privacy else _client_ip(request)
    ua = "" if privacy else request.headers.get("user-agent", "")
    token = create_access_token(user.id, ip=ip, ua=ua, privacy=privacy)
    response.set_cookie("access_token", token, **_cookie_kwargs(request))
    return token


def _require_not_onion(request: Request) -> None:
    """Google sign-in is disabled for onion sessions."""
    from app.services.privacy_service import is_onion_request
    if is_onion_request(request):
        raise HTTPException(400, "Google sign-in is disabled on Tor. Use the username/password form.")


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
        "googleClientId":   s.GOOGLE_CLIENT_ID or "",
        "enabled":          fb.is_configured(),
    }


class GoogleLoginIn(BaseModel):
    id_token: str


@router.post("/google", dependencies=[Depends(rate_limit_auth)])
async def fb_google(body: GoogleLoginIn, response: Response, request: Request, db: AsyncSession = Depends(get_db)):
    _require_not_onion(request)

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
        from app.core.system_settings import hide_auth_pages_enabled
        if await hide_auth_pages_enabled(db):
            raise HTTPException(404)
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
            firebase_uid=uid,
            email_verified=True,
            merchant_setup_unlocked=True,
            app_name=name.strip()[:40] or username,
            app_name_locked=False,
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

    from app.core.security import record_login_meta
    record_login_meta(user, request)
    await db.commit()

    await _issue_app_cookie(response, user, request)

    return {
        "user_id":  user.id,
        "username": user.username,
        "role":     user.role,
        "is_operator": user.role == UserRole.operator,
        "email":    user.email,
    }
