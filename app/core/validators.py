"""
Input validation and sanitization helpers — Community Edition.

These functions are used throughout the API layer to validate
user-supplied input before processing or storing.
"""

import re
from fastapi import HTTPException

# ─ Patterns ─
_RE_NULL      = re.compile(r"\x00")
_RE_TRAVERSAL = re.compile(r"\.\./|\.\.\\", re.IGNORECASE)
_RE_XSS       = re.compile(r"<script|javascript:|data:text/html|vbscript:", re.IGNORECASE)
_RE_SQLI      = re.compile(r"(union\s+select|drop\s+table|insert\s+into|delete\s+from)", re.IGNORECASE)
_RE_TEMPLATE  = re.compile(r"\{\{.*?\}\}|\{%.*?%\}")


def detect_attack(text: str) -> str | None:
    """Return attack type string if suspicious input detected, else None."""
    if not text:
        return None
    if _RE_NULL.search(text):
        return "null_byte"
    if _RE_TRAVERSAL.search(text):
        return "path_traversal"
    if _RE_XSS.search(text):
        return "xss"
    if _RE_SQLI.search(text):
        return "sqli"
    if _RE_TEMPLATE.search(text):
        return "ssti"
    return None


def sanitize_str(v: str | None, max_len: int = 512) -> str | None:
    """Strip control characters and truncate."""
    if v is None:
        return None
    v = str(v)[:max_len]
    v = _RE_NULL.sub("", v)
    v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v)
    return v.strip()


def validate_clean(value: str | None, field: str = "input") -> str | None:
    """Sanitize + reject known attack patterns."""
    if not value:
        return value
    attack = detect_attack(str(value))
    if attack:
        raise HTTPException(400, f"Invalid {field}: contains {attack} pattern")
    return sanitize_str(value)


def validate_amount(v: float, min_val: float = 0.0001, max_val: float = 1_000_000.0) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "Amount must be a number")
    if v < min_val:
        raise HTTPException(400, f"Amount must be at least {min_val} FIRO")
    if v > max_val:
        raise HTTPException(400, f"Amount too large (max {max_val} FIRO)")
    return round(v, 8)


def validate_address(addr: str) -> str:
    addr = (addr or "").strip()
    if not addr:
        raise HTTPException(400, "Address is required")
    if len(addr) < 25 or len(addr) > 100:
        raise HTTPException(400, "Invalid address length")
    if not re.match(r"^[a-zA-Z0-9]+$", addr):
        raise HTTPException(400, "Address contains invalid characters")
    if detect_attack(addr):
        raise HTTPException(400, "Address contains invalid content")
    return addr


def validate_url(url: str | None, field: str = "URL") -> str | None:
    if not url:
        return None
    url = url.strip()[:2048]
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, f"{field} must start with http:// or https://")
    if detect_attack(url):
        raise HTTPException(400, f"{field} contains invalid content")
    return url


def validate_username(username: str) -> str:
    username = (username or "").strip().lower()
    if len(username) < 3 or len(username) > 32:
        raise HTTPException(400, "Username must be 3–32 characters")
    if not re.match(r"^[a-z0-9_\-]+$", username):
        raise HTTPException(400, "Username may only contain letters, numbers, _ and -")
    return username


def validate_password(password: str) -> str:
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if len(password) > 128:
        raise HTTPException(400, "Password too long (max 128 chars)")
    return password
