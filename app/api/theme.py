"""
Theme API endpoints.

All endpoints require authentication.
Merchants can only access their own theme data — no cross-merchant access.

GET  /api/theme          → current theme settings + resolved CSS vars
PUT  /api/theme          → save theme settings
DELETE /api/theme        → reset to default preset
GET  /api/theme/presets  → list all available presets (public, no auth)
GET  /api/theme/preview  → resolve theme without saving (for live preview)
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from app.core.database import get_db
from app.models.models import User
from app.api.users import get_current_user
from app.services.themes import (
    PRESETS, SAFE_FONTS, SAFE_BUTTON_STYLES,
    validate_theme_input, resolve_theme, theme_from_user,
)

router = APIRouter(prefix="/api/theme", tags=["theme"])


class ThemeInput(BaseModel):
    theme_id:                Optional[str] = None
    theme_accent:            Optional[str] = None
    theme_bg:                Optional[str] = None
    theme_surface:           Optional[str] = None
    theme_text:              Optional[str] = None
    theme_radius:            Optional[str] = None
    theme_font:              Optional[str] = None
    theme_button_style:      Optional[str] = None
    theme_checkout_title:    Optional[str] = None
    theme_checkout_subtitle: Optional[str] = None
    theme_success_msg:       Optional[str] = None
    theme_cancel_msg:        Optional[str] = None


@router.get("/presets")
async def get_presets():
    """Public — list all available presets."""
    return {
        "presets": [
            {
                "id":          pid,
                "label":       p["label"],
                "description": p["description"],
                "preview": {
                    "accent":  p["accent"],
                    "bg":      p["bg"],
                    "surface": p["surface"],
                    "text":    p["text"],
                },
            }
            for pid, p in PRESETS.items()
        ],
        "fonts":         list(SAFE_FONTS.keys()),
        "button_styles": list(SAFE_BUTTON_STYLES),
    }


@router.get("")
async def get_theme(
    user: User = Depends(get_current_user),
):
    """Get current merchant theme settings + resolved CSS variables."""
    theme = theme_from_user(user)
    return {
        "settings": {
            "theme_id":                user.theme_id or "dark_gold",
            "theme_accent":            user.theme_accent,
            "theme_bg":                user.theme_bg,
            "theme_surface":           user.theme_surface,
            "theme_text":              user.theme_text,
            "theme_radius":            user.theme_radius,
            "theme_font":              user.theme_font,
            "theme_button_style":      user.theme_button_style,
            "theme_checkout_title":    user.theme_checkout_title,
            "theme_checkout_subtitle": user.theme_checkout_subtitle,
            "theme_success_msg":       user.theme_success_msg,
            "theme_cancel_msg":        user.theme_cancel_msg,
        },
        "resolved": theme,
    }


@router.put("")
async def save_theme(
    body: ThemeInput,
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Save merchant theme settings. Validates all inputs."""
    safe = validate_theme_input(body.model_dump())

    # Apply only provided (non-None in input) fields
    for field, value in safe.items():
        if getattr(body, field, None) is not None:
            # If user explicitly sent a field, write it (even if None = reset)
            setattr(user, field, value)

    db.add(user)
    await db.commit()
    await db.refresh(user)

    return {"ok": True, "resolved": theme_from_user(user)}


@router.delete("")
async def reset_theme(
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Reset all theme customizations to default preset."""
    theme_fields = [
        "theme_id", "theme_accent", "theme_bg", "theme_surface",
        "theme_text", "theme_radius", "theme_font", "theme_button_style",
        "theme_checkout_title", "theme_checkout_subtitle",
        "theme_success_msg", "theme_cancel_msg",
    ]
    for f in theme_fields:
        setattr(user, f, None)
    user.theme_id = "dark_gold"
    db.add(user)
    await db.commit()
    return {"ok": True, "theme_id": "dark_gold"}


@router.post("/preview")
async def preview_theme(
    body: ThemeInput,
    user: User = Depends(get_current_user),
):
    """
    Resolve theme without saving — used for live preview in dashboard.
    Returns CSS variables only, does NOT touch the database.
    """
    safe = validate_theme_input(body.model_dump())
    overrides = {k.replace("theme_", ""): v for k, v in safe.items() if v is not None}
    theme_id = safe.get("theme_id") or user.theme_id or "dark_gold"
    resolved = resolve_theme(theme_id, overrides)
    return {"resolved": resolved}
