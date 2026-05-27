"""
Privacy Middleware - Detects Tor/onion access and sets session privacy state

This middleware runs on every request to:
1. Detect if request is via Tor/onion
2. Set privacy state on request
3. Apply privacy-aware rate limiting
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from loguru import logger

from app.services.privacy_service import (
    is_onion_request,
    set_session_privacy_state,
    get_session_privacy_state
)


class PrivacyMiddleware(BaseHTTPMiddleware):
    """
    Middleware that detects Tor/onion access and sets privacy state.
    
    This middleware:
    - Detects onion requests
    - Sets privacy state on request.state
    - Does NOT block any requests
    - Does NOT modify response (except adding privacy warning header)
    """
    
    async def dispatch(self, request: Request, call_next) -> Response:
        # Detect if this is a Tor/onion request
        is_onion = is_onion_request(request)
        
        # Set initial privacy state (will be updated with user info after auth)
        set_session_privacy_state(
            request,
            is_onion=is_onion,
            user_privacy_mode=False,  # Will be updated after auth
            user_created_via_onion=False
        )
        
        # Log minimally for onion requests
        if is_onion:
            logger.debug(f"[privacy] Onion request: {request.method} {request.url.path}")
        
        # Process the request
        response = await call_next(request)
        
        # Add privacy indicator header (useful for frontend)
        state = get_session_privacy_state(request)
        if state.get("is_onion"):
            response.headers["X-Privacy-Mode"] = "onion"
        
        # Add access warning header if applicable
        if state.get("access_warning"):
            response.headers["X-Privacy-Warning"] = state["access_warning"]
        
        return response


def update_privacy_state_for_user(request: Request, user) -> dict:
    """
    Update privacy state after user authentication.
    Call this in auth endpoints after verifying user.
    
    Returns the updated state with any warnings.
    """
    is_onion = is_onion_request(request)
    user_privacy_mode = getattr(user, 'privacy_mode', False)
    created_via_onion = getattr(user, 'created_via_onion', False)
    
    state = set_session_privacy_state(
        request,
        is_onion=is_onion,
        user_privacy_mode=user_privacy_mode,
        user_created_via_onion=created_via_onion
    )
    
    return state
