"""
Input validation and sanitization helpers.

These functions are used throughout the API layer to validate
user-supplied input before processing or storing.
"""

import re
import ipaddress
import socket
from urllib.parse import urlparse
from fastapi import HTTPException

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


def _ip_is_blocked(ip_str: str) -> bool:
    """True if the IP is loopback, private, link-local, reserved, or otherwise
    not a normal public address (blocks SSRF to internal services / metadata)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → block to be safe
    return (
        ip.is_loopback        # 127.0.0.0/8, ::1
        or ip.is_private      # 10/8, 172.16/12, 192.168/16, fc00::/7
        or ip.is_link_local   # 169.254.0.0/16 (incl. cloud metadata 169.254.169.254), fe80::/10
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified  # 0.0.0.0, ::
    )


def _host_is_blocked(host: str) -> bool:
    """Block obviously-internal hostnames and any host that resolves to a
    non-public IP. `.onion` hosts are allowed (reached only via the Tor proxy,
    not via local DNS) since they are not internal IPs."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return True
    if h == "localhost" or h.endswith(".localhost") or h.endswith(".local") or h.endswith(".internal"):
        return True
    if h.endswith(".onion"):
        return False  # Tor hidden service not an internal IP, routed over Tor
    # If the host is a literal IP, check it directly.
    try:
        ipaddress.ip_address(h.strip("[]"))
        return _ip_is_blocked(h.strip("[]"))
    except ValueError:
        pass
    # Otherwise resolve the DNS name and block if ANY resolved address is internal
    # (defends against DNS-rebinding to 127.0.0.1 / 169.254.169.254 / etc.).
    try:
        infos = socket.getaddrinfo(h, None)
    except Exception:
        return True  # cannot resolve → block
    for info in infos:
        addr = info[4][0]
        if _ip_is_blocked(addr):
            return True
    return False


def validate_url(url: str | None, field: str = "URL") -> str | None:
    if not url:
        return None
    url = url.strip()[:2048]
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, f"{field} must start with http:// or https://")
    if detect_attack(url):
        raise HTTPException(400, f"{field} contains invalid content")
    # SSRF protection: reject internal/loopback/link-local/metadata targets.
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(400, f"{field} is not a valid URL")
    host = parsed.hostname or ""
    if _host_is_blocked(host):
        raise HTTPException(400, f"{field} points to a disallowed (internal/private) host")
    return url


def validate_password(password: str) -> str:
    if len(password) < 10:
        raise HTTPException(400, "Password must be at least 10 characters")
    if len(password) > 128:
        raise HTTPException(400, "Password too long (max 128 chars)")
    return password
