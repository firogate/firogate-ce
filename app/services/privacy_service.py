"""
Privacy Mode Service - Handles Tor/onion detection and privacy-aware logging

This service provides:
- Detection of Tor/onion access
- Privacy-aware logging (minimal for privacy mode)
- Session privacy state management
- Access warnings for cross-mode usage
"""

from typing import Optional
from fastapi import Request


def is_onion_request(request: Request) -> bool:
    """
    Detect if the request is coming through Tor/onion.
    
    Checks:
    1. Host header ends with .onion
    2. X-Forwarded-Host contains .onion
    3. Custom header X-Onion-Request (set by reverse proxy)
    4. Tor exit node indicators
    """
    host = request.headers.get("host", "")
    if host.endswith(".onion"):
        return True

    x_forwarded_host = request.headers.get("x-forwarded-host", "")
    if ".onion" in x_forwarded_host:
        return True

    # Only trust the custom onion header from localhost (set by the proxy) —
    # if accepted from any IP, any client could spoof Tor mode to bypass
    # session binding.
    client_ip = request.client.host if request.client else ""
    if (client_ip in ("127.0.0.1", "::1", "")
            and request.headers.get("x-onion-request", "").lower() == "true"):
        return True

    url_str = str(request.url)
    if ".onion" in url_str:
        return True

    return False


def get_client_ip(request: Request, privacy_mode: bool = False) -> Optional[str]:
    """
    Get client IP address, respecting privacy mode.
    
    In privacy mode: returns None (no IP logging)
    In normal mode: returns actual IP
    """
    if privacy_mode:
        return None

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # first entry is the original client, rest are proxy hops
        return forwarded.split(",")[0].strip()

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    if request.client:
        return request.client.host

    return None


SESSION_PRIVACY_KEY = "_privacy_session"

def get_session_privacy_state(request: Request) -> dict:
    """
    Get privacy state from request state.
    Returns dict with:
    - is_onion: bool - Current request is via onion
    - privacy_mode: bool - User has privacy mode enabled
    - should_minimize_logs: bool - Combined decision
    """
    return getattr(request.state, SESSION_PRIVACY_KEY, {
        "is_onion": False,
        "privacy_mode": False,
        "should_minimize_logs": False,
        "access_warning": None
    })


def set_session_privacy_state(request: Request, is_onion: bool, user_privacy_mode: bool = False,
                               user_created_via_onion: bool = False) -> dict:
    """
    Set privacy state on request.
    Also determines if access warning should be shown.
    """
    should_minimize = is_onion or user_privacy_mode

    access_warning = None
    if user_created_via_onion and not is_onion:
        access_warning = (
            "This account was created in privacy mode (via Tor). "
            "For better privacy, consider accessing via Tor."
        )
    
    state = {
        "is_onion": is_onion,
        "privacy_mode": user_privacy_mode,
        "user_created_via_onion": user_created_via_onion,
        "should_minimize_logs": should_minimize,
        "access_warning": access_warning
    }
    
    setattr(request.state, SESSION_PRIVACY_KEY, state)
    return state


def check_privacy_mode_access(user, request: Request) -> dict:
    """
    Check if user is accessing from expected mode and return warnings if needed.
    
    Returns dict with:
    - allowed: bool (always True - we don't hard block)
    - warning: str or None
    - recommendations: list of strings
    """
    is_onion = is_onion_request(request)
    user_privacy_mode = getattr(user, 'privacy_mode', False)
    created_via_onion = getattr(user, 'created_via_onion', False)
    
    result = {
        "allowed": True,  # never hard block
        "warning": None,
        "recommendations": [],
        "is_onion_session": is_onion,
        "user_privacy_mode": user_privacy_mode
    }

    if created_via_onion and not is_onion:
        result["warning"] = (
            "This account was created in privacy mode. "
            "For better privacy, access via Tor."
        )
        result["recommendations"].append("Consider using Tor Browser for this account")

    if not user_privacy_mode and is_onion:
        result["recommendations"].append(
            "You're accessing via Tor. Enable Privacy Mode in settings for enhanced privacy."
        )
    
    return result
