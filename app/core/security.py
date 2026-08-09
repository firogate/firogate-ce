from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from jose import JWTError, jwt
from loguru import logger

from app.core.config import get_settings


# Default salt (published). For stronger security set FIELD_ENCRYPTION_SALT in
# .env to a unique random value recommended for open-source deployments.
_KDF_SALT_DEFAULT = b"lavapay-field-encryption-v1-salt"
_KDF_ITERATIONS   = 260_000  # keep stable — changing this invalidates existing ciphertext


def _kdf_salt() -> bytes:
    try:
        from app.core.config import get_settings
        custom = (getattr(get_settings(), "FIELD_ENCRYPTION_SALT", "") or "").strip()
        if custom:
            return custom.encode("utf-8")
    except Exception:
        pass
    return _KDF_SALT_DEFAULT

ACCESS_TOKEN_EXPIRE_MINUTES = 10080   # 7 days (7 * 24 * 60)
COOKIE_MAX_AGE = ACCESS_TOKEN_EXPIRE_MINUTES * 60


def _cookie_kwargs(request=None) -> dict:
    """
    Shared cookie attributes for the access_token cookie.

    * `domain`:
        1. explicit COOKIE_DOMAIN from .env (highest priority, e.g. '.example.com')
        2. auto-derived from the CURRENT REQUEST's Host header ensures the
           cookie is always scoped to the registrable domain the user is on
           right now, so a login on example.com works on dashboard.example.com
           and vice versa.
        3. fall back to BASE_URL when the request is unavailable (background task).
        4. empty (host-only) for single-host / localhost / IP.
    * `secure`: True when the request reached us via HTTPS (direct or via a
      reverse-proxy that sets X-Forwarded-Proto: https).
    * `samesite`:
        - 'none' when the cookie is Secure (HTTPS) required for cross-site
          contexts such as Firebase's OAuth popup/iframe returning a response
          that should still carry the session cookie across example.com
          siblings (dashboard ↔ api ↔ checkout).
        - 'lax' as a fallback for plain HTTP (Tor hidden service, local dev).
          Browsers reject SameSite=None without Secure, so we must downgrade.
    """
    s = get_settings()
    kw: dict = {"httponly": True, "max_age": COOKIE_MAX_AGE, "path": "/"}

    dom = (s.COOKIE_DOMAIN or "").strip()
    if not dom and request is not None:
        try:
            req_host = (
                request.headers.get("X-Forwarded-Host", "")
                or request.headers.get("Host", "")
                or (request.url.hostname or "")
            ).split(",")[0].split(":")[0].strip().lower()
            if req_host:
                dom = _derive_cookie_domain(f"https://{req_host}")
        except Exception:
            dom = ""
    if not dom:
        dom = _derive_cookie_domain(s.BASE_URL)
    if dom:
        kw["domain"] = dom

    secure = False
    if request is not None:
        try:
            if (request.url.scheme or "").lower() == "https":
                secure = True
            fwd = (request.headers.get("X-Forwarded-Proto", "") or "").split(",")[0].strip().lower()
            if fwd == "https":
                secure = True
        except Exception:
            pass
    if secure:
        kw["secure"] = True
        kw["samesite"] = "lax"
    else:
        # HTTP (Tor/onion or local dev): SameSite=Lax, no Secure flag
        kw["samesite"] = "lax"
        # For onion requests, do NOT set a domain — keep the cookie host-only
        # so it's scoped to the .onion hostname, not leaked to clearnet domains.
        if "domain" in kw and request is not None:
            try:
                host = (
                    request.headers.get("X-Forwarded-Host", "")
                    or request.headers.get("Host", "")
                    or (request.url.hostname or "")
                ).split(",")[0].split(":")[0].strip().lower()
                if host.endswith(".onion"):
                    kw.pop("domain", None)
            except Exception:
                pass
    return kw


def _derive_cookie_domain(base_url: str) -> str:
    """Derive a cross-subdomain cookie domain from BASE_URL.

    Examples:
      'https://example.com'            → '.example.com'
      'https://www.example.com'        → '.example.com'
      'https://dashboard.example.com'  → '.example.com'
      'http://localhost:8000'           → ''      (host-only cookie)
      'http://127.0.0.1:8000'           → ''
      'https://example.co.uk'           → ''      (avoid public-suffix traps)
    """
    try:
        from urllib.parse import urlparse
        host = (urlparse(base_url).hostname or "").strip().lower()
    except Exception:
        return ""
    if not host or host in ("localhost",):
        return ""
    # IP literal: no cookie domain
    try:
        import ipaddress
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    parts = host.split(".")
    if len(parts) < 2:
        return ""
    reg_tld = parts[-1]
    # Avoid 2-letter country TLDs to not fall into ccTLD public-suffix traps
    # (e.g., don't set '.co.uk' as a cookie domain). For these hosts the user
    # should provide COOKIE_DOMAIN explicitly.
    if len(reg_tld) < 3 or len(reg_tld) > 6:
        return ""
    # Leading dot so ALL subdomains receive the cookie
    return "." + ".".join(parts[-2:])


_SESSION_BIND_ALGO = "sha256"


def hash_password(password: str) -> str:
    """Hash a password using bcrypt (12 rounds). Truncates to 72 bytes —
    bcrypt's hard limit to stay compatible with all bcrypt implementations."""
    pw = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt(12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash. Returns False on any
    error (bad hash, encoding issue, etc.) rather than raising."""
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(
    sub: str,
    minutes: int = ACCESS_TOKEN_EXPIRE_MINUTES,
    ip: str = "",
    ua: str = "",
    privacy: bool = False,
) -> str:
    """
    Create JWT.
    Session binding is intentionally DISABLED behind a reverse proxy /
    rotating mobile networks, the client IP observed at cookie issue vs verify
    can differ, which breaks legitimate sessions (users get kicked out to
    /login on every tab click). Token-theft is already mitigated by:
      * httponly + samesite=lax cookies (no JS can read the token)
      * TLS everywhere
      * Firebase refresh-token revocation on password change
    The `ip`/`ua`/`privacy` params are kept for backwards compatibility with
    callers but no longer produce an `sfp` claim.
    """
    now = datetime.now(timezone.utc)
    claims = {
        "sub": sub,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
        "jti": secrets.token_hex(16),
    }
    return jwt.encode(claims, get_settings().SECRET_KEY, algorithm="HS256")


def verify_access_token(token: str) -> Optional[str]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, get_settings().SECRET_KEY, algorithms=["HS256"])
        return payload.get("sub")
    except JWTError:
        return None


def verify_session_binding(token: str, ip: str, ua: str) -> bool:
    """
    Legacy session fingerprint check. Session binding has been disabled
    (see create_access_token) because IP mismatches behind a proxy cause
    false logouts. We still accept old tokens that embed an `sfp` claim —
    we just don't enforce it strictly. Returns True whenever the JWT decodes
    successfully. Token-theft defence relies on httponly cookie + TLS.
    """
    if not token:
        return False
    try:
        jwt.decode(token, get_settings().SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        return False
    return True


def _derive_fernet(key_material: str, iterations: int = _KDF_ITERATIONS) -> Fernet:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_kdf_salt(),
        iterations=iterations,
        backend=default_backend(),
    )
    raw = kdf.derive(key_material.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(raw))


_ephemeral_key_material: Optional[str] = None


def _get_ephemeral_key() -> str:
    global _ephemeral_key_material
    if _ephemeral_key_material is None:
        _ephemeral_key_material = secrets.token_urlsafe(32)
    return _ephemeral_key_material


_multi_fernet: Optional[MultiFernet]   = None
_fernet_key_snapshot: str             = ""


def _build_multi_fernet(s) -> MultiFernet:
    current_raw = (getattr(s, "FIELD_ENCRYPTION_KEY", "") or "").strip()
    old_raw     = (getattr(s, "FIELD_ENCRYPTION_KEY_OLD", "") or "").strip()

    if not current_raw:
        logger.critical(
            "[security] ⚠  FIELD_ENCRYPTION_KEY is empty  using an EPHEMERAL key.\n"
            "         Encrypted data (TOTP secrets, webhook secrets) will be LOST on restart.\n"
            "         Generate a key and add it to .env before going live."
        )
        current_raw = _get_ephemeral_key()

    keys = [_derive_fernet(current_raw)]

    if old_raw and old_raw != current_raw:
        keys.append(_derive_fernet(old_raw))
        logger.info(
            "[security] Key rotation active: MultiFernet with current + old key. "
            "Run /api/panel/rotate-field-keys to re-encrypt at-rest data, "
            "then remove FIELD_ENCRYPTION_KEY_OLD from .env."
        )

    return MultiFernet(keys)


def get_fernet() -> MultiFernet:
    global _multi_fernet, _fernet_key_snapshot

    s = get_settings()
    snapshot = (
        (getattr(s, "FIELD_ENCRYPTION_KEY", "") or "")
        + "|"
        + (getattr(s, "FIELD_ENCRYPTION_KEY_OLD", "") or "")
    )

    if _multi_fernet is None or snapshot != _fernet_key_snapshot:
        _multi_fernet = _build_multi_fernet(s)
        _fernet_key_snapshot = snapshot

    return _multi_fernet


def encrypt_field(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"encrypt_field expects str, got {type(value).__name__}")
    return get_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_field(value: str) -> str:
    if not value:
        raise ValueError("decrypt_field: received empty token")
    try:
        return get_fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "Field decryption failed token is invalid, tampered, or was "
            "encrypted with a different FIELD_ENCRYPTION_KEY."
        ) from exc


def rotate_encrypted_field(token: str) -> str:
    if not token:
        raise ValueError("rotate_encrypted_field: empty token")
    try:
        return get_fernet().rotate(token.encode("ascii")).decode("ascii")
    except InvalidToken as exc:
        raise ValueError("rotate_encrypted_field: decryption failed") from exc


def validate_encryption_key_on_startup() -> None:
    s   = get_settings()
    raw = (getattr(s, "FIELD_ENCRYPTION_KEY", "") or "").strip()

    if not raw:
        logger.critical(
            "[security] ═══════════════════════════════════════════════════\n"
            "[security]  FIELD_ENCRYPTION_KEY is NOT SET in .env\n"
            "[security]  All encrypted DB fields use an EPHEMERAL key.\n"
            "[security]  Data will be unreadable after every restart.\n"
            "[security]\n"
            "[security]  Fix: generate a key and add to .env\n"
            "[security]  python3 -c \""
            "import secrets,base64; "
            "print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
            "\"\n"
            "[security] ═══════════════════════════════════════════════════"
        )
        return

    try:
        fernet = _derive_fernet(raw)
        probe  = secrets.token_hex(32)
        token  = fernet.encrypt(probe.encode()).decode()
        result = fernet.decrypt(token.encode()).decode()
        if result != probe:
            raise AssertionError("round-trip plaintext mismatch")
    except Exception as exc:
        raise RuntimeError(
            f"[security] FIELD_ENCRYPTION_KEY validation FAILED: {exc}\n"
            "The application cannot start safely fix the key in .env."
        ) from exc

    logger.success(
        f"[security] ✅ FIELD_ENCRYPTION_KEY validated "
        f"(PBKDF2-SHA256, {_KDF_ITERATIONS:,} iter)"
    )

    if not (getattr(s, "FIELD_ENCRYPTION_SALT", "") or "").strip():
        logger.warning(
            "[security] FIELD_ENCRYPTION_SALT is NOT SET in .env — using the "
            "shared default salt baked into this open-source repo. This does "
            "not break encryption, but every deployment that skips this "
            "setting derives its encryption key with the same salt, which "
            "narrows the KDF's per-deployment diversity.\n"
            "[security]  Recommended: set FIELD_ENCRYPTION_SALT and rotate "
            "via FIELD_ENCRYPTION_KEY_OLD (see below) do NOT change it "
            "silently on an existing deployment, or previously-encrypted "
            "fields (webhook secrets, TOTP secrets) become unreadable.\n"
            "[security]  python3 -c \"import secrets; print(secrets.token_hex(16))\""
        )

    old_raw = (getattr(s, "FIELD_ENCRYPTION_KEY_OLD", "") or "").strip()
    if old_raw:
        logger.info(
            "[security] FIELD_ENCRYPTION_KEY_OLD detected key rotation mode is active. "
            "After migrating all rows, remove FIELD_ENCRYPTION_KEY_OLD from .env."
        )


def generate_api_key() -> str:
    return "fgate_" + secrets.token_hex(24)


def generate_webhook_secret() -> str:
    return secrets.token_hex(32)


def generate_account_number() -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(16))


def format_account_number(raw: str) -> str:
    digits = "".join(c for c in raw if c.isdigit())
    return " ".join(digits[i:i + 4] for i in range(0, len(digits), 4))


def normalize_account_number(raw: str) -> str:
    return "".join(c for c in raw if c.isdigit())


def account_number_lookup_key(normalized: str) -> str:
    key = get_settings().SECRET_KEY.encode("utf-8")
    return hmac.new(key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_webhook(payload: dict, secret: str) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def build_webhook_payload(base: dict) -> dict:
    return {**base, "nonce": secrets.token_hex(16), "timestamp": int(time.time())}


def generate_checkout_token(payment_id: str, created_ts: str) -> str:
    """Generate a short HMAC token for the checkout URL.

    Protects public payment endpoints from enumeration: even if someone
    guesses a UUID, they can't access it without the token. Token is the
    first 12 bytes of HMAC-SHA256(payment_id + ":" + created_ts, SECRET_KEY),
    encoded as URL-safe base64.
    """
    from app.core.config import get_settings
    secret = get_settings().SECRET_KEY.encode()
    # created_ts comes from datetime.isoformat(). SQLite drops tzinfo on
    # read, so the same instant serializes with a "+00:00" suffix right
    # after creation (in-memory, tz-aware) but without one once re-fetched
    # from the DB (naive). Strip it so both produce the same token.
    if created_ts.endswith("+00:00"):
        created_ts = created_ts[:-6]
    elif created_ts.endswith("Z"):
        created_ts = created_ts[:-1]
    message = f"{payment_id}:{created_ts}".encode()
    digest = hmac.new(secret, message, hashlib.sha256).digest()
    import base64
    return base64.urlsafe_b64encode(digest[:12]).decode().rstrip("=")


def verify_checkout_token(payment_id: str, created_ts: str, token: str) -> bool:
    """Constant-time verify of checkout token."""
    if not token or len(token) < 10:
        return False
    expected = generate_checkout_token(payment_id, created_ts)
    return hmac.compare_digest(expected, token)


def record_login_meta(user, request) -> None:
    """Stamp last-login time / IP / device on the user row (panel visibility).

    Privacy rules: for privacy-mode accounts and Tor sessions the IP and
    user-agent are NOT recorded only the timestamp. The row is overwritten
    on every successful login (one snapshot per user, not a history log).
    """
    from datetime import datetime, timezone
    user.last_login_at = datetime.now(timezone.utc)
    try:
        from app.services.privacy_service import is_onion_request
        private = bool(getattr(user, "privacy_mode", False)) or is_onion_request(request)
    except Exception:
        private = bool(getattr(user, "privacy_mode", False))
    if private:
        user.last_login_ip = None
        user.last_login_device = None
        return
    try:
        from app.core.config import get_settings
        ip = ""
        if get_settings().TRUST_PROXY_HEADERS:
            ip = (request.headers.get("CF-Connecting-IP", "").strip()
                  or request.headers.get("X-Real-IP", "").strip()
                  or (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()))
        if not ip:
            ip = request.client.host if request.client else ""
        user.last_login_ip = ip[:64] or None
    except Exception:
        user.last_login_ip = None
    ua = (request.headers.get("user-agent") or "").strip()
    user.last_login_device = ua[:256] or None
