"""
Firebase Admin SDK wrapper.

Responsibilities:
  - Verify Firebase ID tokens issued to the browser SDK.
  - Create / update Firebase users (email, password, email_verified).
  - Revoke refresh tokens after a password change.

Credentials are read from environment variables:
  FIREBASE_PROJECT_ID
  FIREBASE_CLIENT_EMAIL
  FIREBASE_PRIVATE_KEY    (PEM, escaped \n or literal newlines both supported)
"""
from __future__ import annotations

import threading
from typing import Optional

from loguru import logger

import firebase_admin
from firebase_admin import credentials as fb_credentials
from firebase_admin import auth as fb_auth

from app.core.config import get_settings

_lock = threading.Lock()
_app: Optional[firebase_admin.App] = None


def _build_credentials() -> fb_credentials.Certificate:
    s = get_settings()
    project_id = (s.FIREBASE_PROJECT_ID or "").strip()
    client_email = (s.FIREBASE_CLIENT_EMAIL or "").strip()
    private_key = (s.FIREBASE_PRIVATE_KEY or "").strip()

    if not (project_id and client_email and private_key):
        raise RuntimeError(
            "Firebase is not configured. Set FIREBASE_PROJECT_ID, "
            "FIREBASE_CLIENT_EMAIL, FIREBASE_PRIVATE_KEY in .env"
        )

    # Normalise the PEM body. .env files store the key with literal "\n"
    # sequences; some editors wrap it in quotes. Accept both.
    if "\\n" in private_key:
        private_key = private_key.replace("\\n", "\n")
    if private_key.startswith('"') and private_key.endswith('"'):
        private_key = private_key[1:-1]
    if private_key.startswith("'") and private_key.endswith("'"):
        private_key = private_key[1:-1]
    if "BEGIN PRIVATE KEY" not in private_key:
        raise RuntimeError(
            "FIREBASE_PRIVATE_KEY is malformed (must contain "
            "'-----BEGIN PRIVATE KEY-----'). Re-download the service "
            "account JSON and copy the private_key field exactly."
        )

    return fb_credentials.Certificate({
        "type": "service_account",
        "project_id": project_id,
        "client_email": client_email,
        "private_key": private_key,
        "token_uri": "https://oauth2.googleapis.com/token",
    })


def get_app() -> firebase_admin.App:
    global _app
    if _app is not None:
        return _app
    with _lock:
        if _app is not None:
            return _app
        if firebase_admin._apps:
            _app = firebase_admin.get_app()
            return _app
        _app = firebase_admin.initialize_app(_build_credentials())
        logger.info("[firebase] Admin SDK initialised")
        return _app


def is_configured() -> bool:
    s = get_settings()
    return bool(s.FIREBASE_PROJECT_ID and s.FIREBASE_CLIENT_EMAIL and s.FIREBASE_PRIVATE_KEY)


# ─ ID token verification ──
def verify_id_token(id_token: str, check_revoked: bool = False) -> dict:
    """
    Verify a Firebase ID token. Returns the decoded claims dict on success.
    Raises ValueError with a user-safe message on failure.

    `check_revoked` defaults to False: enabling it forces an extra HTTPS round
    trip to Google on every login and surfaces transient network errors as
    "Could not verify session". Refresh tokens are short-lived and we revoke
    them on password change anyway, so the default is safe for sign-in.
    """
    if not id_token or not isinstance(id_token, str):
        raise ValueError("Missing token")
    try:
        get_app()
        return fb_auth.verify_id_token(id_token, check_revoked=check_revoked)
    except fb_auth.RevokedIdTokenError:
        raise ValueError("Session has been revoked. Please sign in again.")
    except fb_auth.ExpiredIdTokenError:
        raise ValueError("Session expired. Please sign in again.")
    except fb_auth.InvalidIdTokenError:
        raise ValueError("Invalid session token.")
    except Exception as exc:
        err_type = type(exc).__name__
        err_msg = str(exc)[:160]
        logger.warning(f"[firebase] verify_id_token failed ({err_type}): {err_msg}")
        if get_settings().DEBUG:
            raise ValueError(f"Could not verify session ({err_type}: {err_msg})")
        raise ValueError("Could not verify session.")


# ─ User management ─
def create_user(email: str, password: str, display_name: Optional[str] = None):
    get_app()
    return fb_auth.create_user(
        email=email,
        password=password,
        email_verified=False,
        display_name=display_name or None,
        disabled=False,
    )


def get_user_by_email(email: str):
    get_app()
    try:
        return fb_auth.get_user_by_email(email)
    except fb_auth.UserNotFoundError:
        return None


def get_user(uid: str):
    get_app()
    try:
        return fb_auth.get_user(uid)
    except fb_auth.UserNotFoundError:
        return None


def set_email_verified(uid: str) -> None:
    get_app()
    fb_auth.update_user(uid, email_verified=True)


def set_password(uid: str, new_password: str) -> None:
    get_app()
    fb_auth.update_user(uid, password=new_password)


def revoke_refresh_tokens(uid: str) -> None:
    get_app()
    try:
        fb_auth.revoke_refresh_tokens(uid)
    except Exception as exc:
        logger.warning(f"[firebase] revoke_refresh_tokens({uid}) failed: {exc}")


# ─ Password verification via Firebase REST API ─
# Admin SDK cannot directly verify a plaintext password. We hit the public
# Identity Toolkit endpoint (the same one the Web SDK uses) so backend-
# initiated logins still go through Firebase as the source of truth.
def verify_password_and_get_id_token(email: str, password: str) -> Optional[dict]:
    """
    Return a dict with keys {idToken, localId, email, ...} on success, or
    None on any failure (wrong password, no such user, disabled, etc.).
    Never raises and never reveals which check failed.
    """
    import httpx  # local import keeps startup fast
    api_key = (get_settings().FIREBASE_API_KEY or "").strip()
    if not api_key or not email or not password:
        return None
    url = ("https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
           f"?key={api_key}")
    payload = {"email": email, "password": password, "returnSecureToken": True}
    try:
        r = httpx.post(url, json=payload, timeout=8.0)
    except Exception as exc:
        logger.warning(f"[firebase] REST signIn network error: {exc}")
        return None
    if r.status_code != 200:
        try:
            err = (r.json().get("error") or {}).get("message", "")
        except Exception:
            err = ""
        logger.info(f"[firebase] REST signIn rejected: {err or r.status_code}")
        return None
    return r.json()
