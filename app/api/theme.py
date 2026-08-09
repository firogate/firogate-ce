"""
Theme API endpoints.

All endpoints require authentication.
Merchants can only access their own theme data no cross-merchant access.

GET  /api/theme          → current theme settings + resolved CSS vars
PUT  /api/theme          → save theme settings
DELETE /api/theme        → reset to default preset
GET  /api/theme/presets  → list all available presets (public, no auth)
GET  /api/theme/preview  → resolve theme without saving (for live preview)
"""

import re, base64, pathlib
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from app.core.database import get_db
from app.models.models import User
from app.api.users import get_current_user
from app.models.models import UserRole
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
    theme_qr_position:       Optional[str] = None
    theme_cancel_position:   Optional[str] = None
    theme_bg_image:          Optional[str] = None
    theme_bg_overlay:        Optional[str] = None
    checkout_layout:         Optional[str] = None
    theme_v2_colors_json:    Optional[dict] = None


@router.get("/presets")
async def get_presets():
    """Public list all available presets."""
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
            "theme_qr_position":       user.theme_qr_position or "bottom",
            "theme_cancel_position":   user.theme_cancel_position or "bottom",
            "theme_bg_image":          user.theme_bg_image,
            "theme_bg_overlay":        user.theme_bg_overlay or "70",
            "checkout_layout":         getattr(user, "checkout_layout", None) or "stripe",
            "theme_v2_colors":         theme.get("v2_colors", {}),
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

    # Only apply fields the client explicitly sent (even if None = reset);
    # fields absent from the request are left untouched.
    for field, value in safe.items():
        if getattr(body, field, None) is not None:
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
        "theme_success_msg", "theme_cancel_msg", "theme_v2_colors_json",
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
    Resolve theme without saving used for live preview in dashboard.
    Returns CSS variables only, does NOT touch the database.
    """
    safe = validate_theme_input(body.model_dump())
    overrides = {k.replace("theme_", ""): v for k, v in safe.items() if v is not None}
    theme_id = safe.get("theme_id") or user.theme_id or "dark_gold"
    resolved = resolve_theme(theme_id, overrides)
    return {"resolved": resolved}


# Theme thumbnail images
_VALID_LAYOUT_IDS = {
    "stripe", "receipt", "glass", "brutalist", "luxury", "playful",
    "newspaper", "cyberpunk", "swiss", "pixel", "boarding_pass", "zen",
    "aurora", "blueprint", "botanical", "eink", "fintech", "artdeco", "comic", "vaporwave",
    "delivery", "candy", "beauty", "princess", "boutique", "chalkboard", "gym", "vinyl",
}
_THUMB_DIR = pathlib.Path(__file__).parent.parent.parent / "static" / "images" / "themes"
_THUMB_DIR.mkdir(parents=True, exist_ok=True)

_ALLOWED_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


@router.get("/thumbnails")
async def get_theme_thumbnails():
    """Return a map of layout_id → static URL for uploaded thumbnails."""
    result = {}
    for lid in _VALID_LAYOUT_IDS:
        for ext in (".webp", ".jpg", ".png"):
            p = _THUMB_DIR / (lid + ext)
            if p.exists():
                result[lid] = f"/static/images/themes/{lid}{ext}"
                break
    return result


class ThumbnailInput(BaseModel):
    image: str  # data URI: data:image/png;base64,...


def _sniff_thumb_image(b: bytes) -> str | None:
    if b[:8] == b"\x89PNG\r\n\x1a\n":               return "image/png"
    if b[:3] == b"\xff\xd8\xff":                     return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":      return "image/webp"
    return None


@router.post("/thumbnail/{layout_id}")
async def upload_theme_thumbnail(
    layout_id: str,
    body: ThumbnailInput,
    user: User = Depends(get_current_user),
):
    if user.role != UserRole.operator:
        raise HTTPException(status_code=403, detail="Operator only.")
    if layout_id not in _VALID_LAYOUT_IDS:
        raise HTTPException(status_code=400, detail="Unknown layout id.")
    data_uri = (body.image or "").strip()
    m = re.match(r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)$", data_uri)
    if not m:
        raise HTTPException(status_code=400, detail="Invalid image data URI.")
    mime = m.group(1)
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Corrupt image data.")
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="Empty image.")
    if len(raw) > 3 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 3 MB).")

    # Verify magic bytes match the declared type - don't trust the client label.
    sniffed = _sniff_thumb_image(raw)
    if sniffed is None or sniffed != mime:
        raise HTTPException(status_code=400, detail="File content does not match its declared type.")

    # Re-encode with Pillow: strips EXIF/ICC/any embedded payload, and
    # (Pillow's default DecompressionBombError) guards against pixel bombs.
    import io
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise HTTPException(status_code=400, detail="Cannot decode image.")
    img.thumbnail((1200, 1200), Image.LANCZOS)
    out = io.BytesIO()
    ext = _ALLOWED_MIME[mime]
    if mime == "image/png":
        img.convert("RGBA" if img.mode in ("RGBA", "LA", "PA") else "RGB").save(out, format="PNG", optimize=True)
    elif mime == "image/webp":
        img.convert("RGBA" if img.mode in ("RGBA", "LA", "PA") else "RGB").save(out, format="WEBP", quality=90, method=4)
    else:
        img.convert("RGB").save(out, format="JPEG", quality=90)
    clean = out.getvalue()
    if len(clean) > 3 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large after processing.")

    for old_ext in (".webp", ".jpg", ".png"):
        old = _THUMB_DIR / (layout_id + old_ext)
        if old.exists():
            old.unlink()
    dest = _THUMB_DIR / (layout_id + ext)
    dest.write_bytes(clean)
    return {"ok": True, "url": f"/static/images/themes/{layout_id}{ext}"}
