"""
Cloudflare Turnstile server-side verification.

Docs: https://developers.cloudflare.com/turnstile/get-started/server-side-validation/

Fail-closed when TURNSTILE_SECRET_KEY is set and the check fails.
Fail-open (return True) only when the secret is not configured in .env,
so development setups don't block legitimate requests.
"""
from __future__ import annotations

import ipaddress
from typing import Optional, Tuple

import httpx
from loguru import logger

from app.core.config import get_settings

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def is_configured() -> bool:
    return bool((get_settings().TURNSTILE_SECRET_KEY or "").strip())


def _is_real_ip(ip: str | None) -> bool:
    if not ip or not isinstance(ip, str):
        return False
    ip = ip.strip()
    if not ip or ip.startswith("priv_") or ip == "unknown":
        return False
    try:
        obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if obj.is_loopback or obj.is_private or obj.is_link_local or obj.is_multicast:
        return False
    return True


async def verify_with_reason(token: Optional[str], remote_ip: Optional[str] = None) -> Tuple[bool, str]:
    """
    Return (ok, reason). When ok is False, `reason` is a short diagnostic string
    like 'invalid-input-response', 'timeout-or-duplicate', 'missing-input-response',
    or a network-level hint. Safe to show to end users — no secrets leak.
    """
    secret = (get_settings().TURNSTILE_SECRET_KEY or "").strip()
    if not secret:
        return True, ""
    if not token:
        return False, "missing-input-response"

    data = {"secret": secret, "response": token}
    if _is_real_ip(remote_ip):
        data["remoteip"] = remote_ip

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.post(VERIFY_URL, data=data)
            if r.status_code != 200:
                logger.warning(f"[turnstile] HTTP {r.status_code}: {r.text[:200]}")
                return False, f"cf-http-{r.status_code}"
            payload = r.json()
    except Exception as exc:
        logger.warning(f"[turnstile] verify network error: {exc}")
        return False, "network-error"

    if bool(payload.get("success")):
        return True, ""

    codes = payload.get("error-codes") or []
    logger.warning(
        f"[turnstile] rejected: errors={codes} hostname={payload.get('hostname')}"
    )
    return False, (codes[0] if codes else "unknown")


async def verify(token: Optional[str], remote_ip: Optional[str] = None) -> bool:
    ok, _ = await verify_with_reason(token, remote_ip)
    return ok
