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
from loguru import logger


# ═══════════════════════════════════════════════════════════════════════════════
# Tor/Onion Detection
# ═══════════════════════════════════════════════════════════════════════════════

def is_onion_request(request: Request) -> bool:
    """
    Detect if the request is coming through Tor/onion.
    
    Checks:
    1. Host header ends with .onion
    2. X-Forwarded-Host contains .onion
    3. Custom header X-Onion-Request (set by reverse proxy)
    4. Tor exit node indicators
    """
    # Check host header
    host = request.headers.get("host", "")
    if host.endswith(".onion"):
        return True
    
    # Check X-Forwarded-Host (for reverse proxy setups)
    x_forwarded_host = request.headers.get("x-forwarded-host", "")
    if ".onion" in x_forwarded_host:
        return True
    
    # Check custom onion header — ONLY trust it from localhost (set by nginx)
    # If accepted from any IP, any client can spoof Tor mode to bypass session binding
    client_ip = request.client.host if request.client else ""
    if (client_ip in ("127.0.0.1", "::1", "")
            and request.headers.get("x-onion-request", "").lower() == "true"):
        return True
    
    # Check URL directly
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
    
    # Check X-Forwarded-For first (reverse proxy)
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Take the first IP (original client)
        return forwarded.split(",")[0].strip()
    
    # Check X-Real-IP
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    
    # Fall back to direct client
    if request.client:
        return request.client.host
    
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Session Privacy State
# ═══════════════════════════════════════════════════════════════════════════════

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
    # Determine if logs should be minimized
    should_minimize = is_onion or user_privacy_mode
    
    # Determine access warning
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


# ═══════════════════════════════════════════════════════════════════════════════
# Privacy-Aware Logging
# ═══════════════════════════════════════════════════════════════════════════════

def privacy_log(
    request: Request,
    level: str,
    message: str,
    include_ip: bool = True,
    include_user_agent: bool = False,
    user_id: Optional[str] = None,
    extra: Optional[dict] = None
):
    """
    Log with privacy awareness.
    
    In privacy mode:
    - No IP addresses
    - No user agents
    - Minimal metadata
    
    In normal mode:
    - Full logging
    """
    state = get_session_privacy_state(request)
    should_minimize = state.get("should_minimize_logs", False)
    
    log_data = {"message": message}
    
    if user_id:
        # Always include user_id (needed for system operation)
        # But truncate in privacy mode
        log_data["user"] = user_id[:8] + "..." if should_minimize else user_id
    
    if not should_minimize:
        if include_ip:
            ip = get_client_ip(request, privacy_mode=False)
            if ip:
                log_data["ip"] = ip
        
        if include_user_agent:
            ua = request.headers.get("user-agent", "")[:100]
            if ua:
                log_data["ua"] = ua
        
        if extra:
            log_data.update(extra)
    
    # Build log message
    log_msg = message
    if log_data.get("user"):
        log_msg += f" [user={log_data['user']}]"
    if log_data.get("ip"):
        log_msg += f" [ip={log_data['ip']}]"
    
    # Log at appropriate level
    log_func = getattr(logger, level.lower(), logger.info)
    log_func(log_msg)


def log_auth_event(
    request: Request,
    event: str,
    user_id: Optional[str] = None,
    success: bool = True,
    extra: Optional[dict] = None
):
    """
    Log authentication events with privacy awareness.
    """
    state = get_session_privacy_state(request)
    should_minimize = state.get("should_minimize_logs", False)
    
    level = "info" if success else "warning"
    
    if should_minimize:
        # Minimal log for privacy mode
        logger.log(level.upper(), f"[auth] {event} {'✓' if success else '✗'}")
    else:
        # Full log for normal mode
        ip = get_client_ip(request, privacy_mode=False)
        user_str = f" user={user_id}" if user_id else ""
        ip_str = f" ip={ip}" if ip else ""
        extra_str = f" {extra}" if extra else ""
        logger.log(level.upper(), f"[auth] {event}{user_str}{ip_str}{extra_str}")


# ═══════════════════════════════════════════════════════════════════════════════
# Rate Limiting Helpers (Privacy-aware)
# ═══════════════════════════════════════════════════════════════════════════════

def get_rate_limit_key(request: Request, prefix: str = "rl") -> str:
    """
    Get rate limit key, using privacy-safe identifier for Tor users.
    
    For privacy mode: Use a hashed session identifier
    For normal mode: Use IP address
    """
    state = get_session_privacy_state(request)
    
    if state.get("is_onion") or state.get("privacy_mode"):
        # For Tor users, use a combination of user-agent hash + session
        # This provides some rate limiting while preserving privacy
        import hashlib
        ua = request.headers.get("user-agent", "unknown")
        # Use a hash to avoid storing identifiable info
        identifier = hashlib.sha256(ua.encode()).hexdigest()[:16]
        return f"{prefix}:onion:{identifier}"
    else:
        ip = get_client_ip(request, privacy_mode=False)
        return f"{prefix}:ip:{ip or 'unknown'}"


# ═══════════════════════════════════════════════════════════════════════════════
# Access Control Helpers
# ═══════════════════════════════════════════════════════════════════════════════

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
        "allowed": True,  # Never hard block
        "warning": None,
        "recommendations": [],
        "is_onion_session": is_onion,
        "user_privacy_mode": user_privacy_mode
    }
    
    # Warning: Privacy account accessed from clearnet
    if created_via_onion and not is_onion:
        result["warning"] = (
            "This account was created in privacy mode. "
            "For better privacy, access via Tor."
        )
        result["recommendations"].append("Consider using Tor Browser for this account")
    
    # Info: Normal account accessed from Tor (totally fine)
    if not user_privacy_mode and is_onion:
        result["recommendations"].append(
            "You're accessing via Tor. Enable Privacy Mode in settings for enhanced privacy."
        )
    
    return result
