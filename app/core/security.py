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


_KDF_SALT       = b"lavapay-field-encryption-v1-salt"
_KDF_ITERATIONS = 260_000

ACCESS_TOKEN_EXPIRE_MINUTES = 10080   # 7 days (7 * 24 * 60)
COOKIE_MAX_AGE = ACCESS_TOKEN_EXPIRE_MINUTES * 60


def _cookie_kwargs(request=None) -> dict:
    """
    Shared cookie attributes for the access_token cookie.

    * `domain`:
        1. explicit COOKIE_DOMAIN from .env (highest priority, e.g. '.firogate.com')
        2. auto-derived from the CURRENT REQUEST's Host header — ensures the
           cookie is always scoped to the registrable domain the user is on
           right now, so a login on firogate.com works on dashboard.firogate.com
           and vice versa.
        3. fall back to BASE_URL when the request is unavailable (background task).
        4. empty (host-only) for single-host / localhost / IP.
    * `secure`: True when the request reached us via HTTPS (direct or via a
      reverse-proxy that sets X-Forwarded-Proto: https).
    * `samesite`:
        - 'none' when the cookie is Secure (HTTPS) — required for cross-site
          contexts such as Firebase's OAuth popup/iframe returning a response
          that should still carry the session cookie across firogate.com
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
        kw["samesite"] = "none"
    else:
        # HTTP (Tor/onion or local dev) — SameSite=Lax, no Secure flag
        # Browsers reject SameSite=None without Secure, so Lax is correct here.
        kw["samesite"] = "lax"
        # For onion requests: do NOT set a domain — keep cookie host-only
        # so it's scoped to the .onion hostname, not leaked to clearnet domains.
        if "domain" in kw and request is not None:
            try:
                host = (
                    request.headers.get("X-Forwarded-Host", "")
                    or request.headers.get("Host", "")
                    or (request.url.hostname or "")
                ).split(",")[0].split(":")[0].strip().lower()
                if host.endswith(".onion"):
                    kw.pop("domain", None)  # host-only cookie for .onion
            except Exception:
                pass
    return kw


def _derive_cookie_domain(base_url: str) -> str:
    """Derive a cross-subdomain cookie domain from BASE_URL.

    Examples:
      'https://firogate.com'            → '.firogate.com'
      'https://www.firogate.com'        → '.firogate.com'
      'https://dashboard.firogate.com'  → '.firogate.com'
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
    # IP literal? no cookie domain
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

# Session binding: hash of IP + User-Agent for clearnet users
_SESSION_BIND_ALGO = "sha256"


def hash_password(password: str) -> str:
    """Hash a password using bcrypt (12 rounds). Truncates to 72 bytes —
    bcrypt's hard limit — to stay compatible with all bcrypt implementations."""
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


def _compute_session_fingerprint(ip: str, ua: str) -> str:
    """Compute a short hash binding a session to IP + User-Agent."""
    raw = f"{ip or ''}|{ua or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def create_access_token(
    sub: str,
    minutes: int = ACCESS_TOKEN_EXPIRE_MINUTES,
    ip: str = "",
    ua: str = "",
    privacy: bool = False,
) -> str:
    """
    Create JWT.
    Session binding is intentionally DISABLED — behind Cloudflare / nginx /
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
    (see create_access_token) because IP mismatches behind Cloudflare cause
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
        salt=_KDF_SALT,
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
            "Run /api/admin/rotate-field-keys to re-encrypt at-rest data, "
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
            "Field decryption failed — token is invalid, tampered, or was "
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
            "The application cannot start safely — fix the key in .env."
        ) from exc

    key_prefix = raw[:6] + "…" if len(raw) > 6 else raw
    logger.success(
        f"[security] ✅ FIELD_ENCRYPTION_KEY validated "
        f"(PBKDF2-SHA256, {_KDF_ITERATIONS:,} iter) prefix={key_prefix}"
    )

    old_raw = (getattr(s, "FIELD_ENCRYPTION_KEY_OLD", "") or "").strip()
    if old_raw:
        logger.info(
            "[security] FIELD_ENCRYPTION_KEY_OLD detected — key rotation mode is active. "
            "After migrating all rows, remove FIELD_ENCRYPTION_KEY_OLD from .env."
        )


def generate_api_key() -> str:
    return "fgate_" + secrets.token_hex(24)


def generate_webhook_secret() -> str:
    return secrets.token_hex(32)


def sign_webhook(payload: dict, secret: str) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    payload: dict,
    secret: str,
    signature: str,
    max_age_seconds: int = 300,
) -> bool:
    ts  = payload.get("timestamp", 0)
    now = time.time()
    if abs(now - ts) > max_age_seconds:
        return False
    if ts > now + 30:
        return False
    expected = sign_webhook(payload, secret)
    if not hmac.compare_digest(expected, signature):
        return False
    # Nonce replay check is handled externally via nonce_tracker
    # (caller should check is_nonce_used before calling this)
    return True


def build_webhook_payload(base: dict) -> dict:
    return {**base, "nonce": secrets.token_hex(16), "timestamp": int(time.time())}

# ─ Checkout Access Token (HMAC) ─
# Protects public payment endpoints from enumeration.
# Even if someone guesses a UUID, they can't access it without the token.
#
# Token = first 16 bytes of HMAC-SHA256(payment_id + ":" + created_ts, SECRET_KEY)
# Encoded as URL-safe base64 (22 chars, no padding).

def generate_checkout_token(payment_id: str, created_ts: str) -> str:
    """Generate a short HMAC token for checkout URL."""
    from app.core.config import get_settings
    secret = get_settings().SECRET_KEY.encode()
    message = f"{payment_id}:{created_ts}".encode()
    digest = hmac.new(secret, message, hashlib.sha256).digest()
    # First 12 bytes → 16 URL-safe base64 chars (no padding)
    import base64
    return base64.urlsafe_b64encode(digest[:12]).decode().rstrip("=")


def verify_checkout_token(payment_id: str, created_ts: str, token: str) -> bool:
    """Constant-time verify of checkout token."""
    if not token or len(token) < 10:
        return False
    expected = generate_checkout_token(payment_id, created_ts)
    return hmac.compare_digest(expected, token)
