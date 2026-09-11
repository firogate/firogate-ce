"""
Input validation and sanitization helpers.

These functions are used throughout the API layer to validate
user-supplied input before processing or storing.
"""

import re
import ipaddress
import socket
import asyncio
from urllib.parse import urlparse, urlunparse
from fastapi import HTTPException

_DNS_TIMEOUT = 3.0

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


async def _resolve_ips(host: str) -> list[str]:
    """Resolve `host` off the event loop with a bounded timeout, so a
    blackholed/non-responding DNS name cannot stall the single uvicorn
    worker's event loop for the full (~10-13s) glibc resolver timeout."""
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, None),
            timeout=_DNS_TIMEOUT,
        )
    except Exception:
        return []
    return [info[4][0] for info in infos]


async def _host_is_blocked(host: str) -> bool:
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
    ips = await _resolve_ips(h)
    if not ips:
        return True  # cannot resolve → block
    for addr in ips:
        if _ip_is_blocked(addr):
            return True
    return False


async def validate_url(url: str | None, field: str = "URL") -> str | None:
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
    if await _host_is_blocked(host):
        raise HTTPException(400, f"{field} points to a disallowed (internal/private) host")
    return url


async def resolve_pinned_target(url: str) -> tuple[str, str | None]:
    """Resolve the host of an already-validated, direct (non-Tor) delivery URL
    once, validate every returned address, and rewrite the URL's authority to
    the validated IP. This closes the gap where a second DNS lookup at
    connect time (DNS rebinding) could return a different, internal address
    than the one that was validated.

    Returns (delivery_url, sni_host). sni_host is the original hostname the
    caller must use for TLS SNI / the Host header; it is None when no
    rewrite happened (.onion hosts, literal IPs — nothing to pin)."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host or host.endswith(".onion"):
        return url, None
    try:
        ipaddress.ip_address(host)
        return url, None
    except ValueError:
        pass
    ips = await _resolve_ips(host)
    if not ips:
        raise HTTPException(400, "Host could not be resolved")
    for addr in ips:
        if _ip_is_blocked(addr):
            raise HTTPException(400, "Host points to a disallowed (internal/private) host")
    ip = ips[0]
    netloc = f"[{ip}]" if ":" in ip else ip
    if parsed.port:
        netloc += f":{parsed.port}"
    pinned_url = urlunparse(parsed._replace(netloc=netloc))
    return pinned_url, host


def validate_password(password: str) -> str:
    if len(password) < 10:
        raise HTTPException(400, "Password must be at least 10 characters")
    if len(password) > 128:
        raise HTTPException(400, "Password too long (max 128 chars)")
    return password
