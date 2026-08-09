"""
FiroGate Checkout Theme System.

PRESETS: 7 professionally designed themes.
Each preset defines a complete set of CSS variables.
Merchants can use a preset as-is or override specific values.

Security:
  - All user-supplied values validated before storage
  - hex colors: ^#[0-9A-Fa-f]{6}$ only
  - text fields: HTML stripped, length capped
  - font keys: allowlist only (no external URLs)
  - radius: integer 0-24 only
  - button_style: allowlist only

Isolation:
  - Theme data read only from authenticated user's own record
  - No shared theme storage between merchants
  - No file uploads in this version
"""

import re
from typing import Any

# Local system fonts only, no external loading
SAFE_FONTS: dict[str, str] = {
    "system":    "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    "mono":      "'Courier New', Courier, monospace",
    "inter":     "Inter, 'Helvetica Neue', Arial, sans-serif",
    "roboto":    "Roboto, 'Helvetica Neue', Arial, sans-serif",
    "georgia":   "Georgia, 'Times New Roman', serif",
    "ubuntu":    "Ubuntu, Cantarell, sans-serif",
    # Bundled locally as woff2 (see templates/base.html @font-face block) —
    # Arabic-range glyphs only, so picking these never affects Latin text.
    "cairo":     "'Cairo', 'Noto Sans Arabic', -apple-system, sans-serif",
    "tajawal":   "'Tajawal', 'Noto Sans Arabic', -apple-system, sans-serif",
    "notoarabic":"'Noto Sans Arabic', 'Cairo', -apple-system, sans-serif",
    "notocyrillic": "'Noto Sans Cyrillic', 'Roboto', -apple-system, sans-serif",
    "notosc":       "'Noto Sans SC', -apple-system, sans-serif",
    # Bundled locally as woff2 (see templates/base.html / gf-faces.css) —
    # decorative/display Latin fonts.
    "spacegrotesk": "'Space Grotesk', 'Helvetica Neue', Arial, sans-serif",
    "jost":         "'Jost', 'Helvetica Neue', Arial, sans-serif",
    "outfit":       "'Outfit', 'Helvetica Neue', Arial, sans-serif",
    "orbitron":     "'Orbitron', 'Helvetica Neue', Arial, sans-serif",
    "archivoblack": "'Archivo Black', 'Helvetica Neue', Arial, sans-serif",
    "baloo2":       "'Baloo 2', cursive, sans-serif",
    "cormorant":    "'Cormorant Garamond', Georgia, serif",
    "jetbrains":    "'JetBrains Mono', 'Courier New', monospace",
    "pressstart":   "'Press Start 2P', 'Courier New', monospace",
    "poiret":       "'Poiret One', cursive, sans-serif",
    "bangers":      "'Bangers', cursive, sans-serif",
    "monoton":      "'Monoton', cursive, sans-serif",
    "pacifico":     "'Pacifico', cursive, sans-serif",
    "caveat":       "'Caveat', cursive, sans-serif",
}

SAFE_BUTTON_STYLES = {"rounded", "sharp", "pill"}

PRESETS: dict[str, dict] = {
    "dark_gold": {
        "label":       "Dark Gold",
        "description": "Premium dark with gold accents",
        "accent":      "#F5C542",
        "bg":          "#0A0806",
        "surface":     "#161210",
        "text":        "#EBE4D7",
        "text_muted":  "#8C7E6A",
        "border":      "#2A221A",
        "success":     "#22C55E",
        "error":       "#EF4444",
        "radius":      "12",
        "font":        "system",
        "button_style":"rounded",
    },
    "midnight": {
        "label":       "Midnight",
        "description": "Deep blue-black, minimal",
        "accent":      "#6366F1",
        "bg":          "#0F0F14",
        "surface":     "#18181F",
        "text":        "#E2E2F0",
        "text_muted":  "#6B6B80",
        "border":      "#2A2A35",
        "success":     "#22C55E",
        "error":       "#EF4444",
        "radius":      "10",
        "font":        "inter",
        "button_style":"rounded",
    },
    "minimal_white": {
        "label":       "Minimal White",
        "description": "Clean light theme, maximum clarity",
        "accent":      "#111111",
        "bg":          "#FAFAFA",
        "surface":     "#FFFFFF",
        "text":        "#111111",
        "text_muted":  "#6B7280",
        "border":      "#E5E7EB",
        "success":     "#16A34A",
        "error":       "#DC2626",
        "radius":      "8",
        "font":        "inter",
        "button_style":"rounded",
    },
    "corporate_blue": {
        "label":       "Corporate Blue",
        "description": "Professional corporate look",
        "accent":      "#2563EB",
        "bg":          "#F8FAFF",
        "surface":     "#FFFFFF",
        "text":        "#1E293B",
        "text_muted":  "#64748B",
        "border":      "#CBD5E1",
        "success":     "#059669",
        "error":       "#DC2626",
        "radius":      "6",
        "font":        "roboto",
        "button_style":"rounded",
    },
    "terminal_dark": {
        "label":       "Terminal Dark",
        "description": "Developer-friendly monospace terminal",
        "accent":      "#00FF41",
        "bg":          "#0D0D0D",
        "surface":     "#141414",
        "text":        "#00FF41",
        "text_muted":  "#00AA2B",
        "border":      "#1A2A1A",
        "success":     "#00FF41",
        "error":       "#FF4444",
        "radius":      "4",
        "font":        "mono",
        "button_style":"sharp",
    },
    "soft_gray": {
        "label":       "Soft Gray",
        "description": "Neutral, understated, universal",
        "accent":      "#374151",
        "bg":          "#F3F4F6",
        "surface":     "#FFFFFF",
        "text":        "#111827",
        "text_muted":  "#6B7280",
        "border":      "#D1D5DB",
        "success":     "#059669",
        "error":       "#DC2626",
        "radius":      "10",
        "font":        "system",
        "button_style":"rounded",
    },
    "clean_modern": {
        "label":       "Clean Modern",
        "description": "Warm dark with subtle gradients",
        "accent":      "#F97316",
        "bg":          "#111827",
        "surface":     "#1F2937",
        "text":        "#F9FAFB",
        "text_muted":  "#9CA3AF",
        "border":      "#374151",
        "success":     "#22C55E",
        "error":       "#EF4444",
        "radius":      "14",
        "font":        "inter",
        "button_style":"pill",
    },
}


_HEX_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')


def _safe_hex(v: Any) -> str | None:
    if not v or not isinstance(v, str):
        return None
    v = v.strip()
    return v.upper() if _HEX_RE.match(v) else None


def _safe_text(v: Any, maxlen: int = 120) -> str | None:
    if not v or not isinstance(v, str):
        return None
    v = re.sub(r'<[^>]+>', '', str(v))
    v = re.sub(r'[<>&"\'\\]', '', v)
    v = re.sub(r'(javascript|data|vbscript):', '', v, flags=re.IGNORECASE)
    v = v.strip()[:maxlen]
    return v or None


def _safe_radius(v: Any) -> str | None:
    if v is None:
        return None
    try:
        n = int(str(v).strip())
        return str(max(0, min(24, n)))
    except (ValueError, TypeError):
        return None


def _safe_font(v: Any) -> str | None:
    if not v or not isinstance(v, str):
        return None
    k = v.strip().lower()
    return k if k in SAFE_FONTS else None


def _safe_button_style(v: Any) -> str | None:
    if not v or not isinstance(v, str):
        return None
    k = v.strip().lower()
    return k if k in SAFE_BUTTON_STYLES else None


def _safe_theme_id(v: Any) -> str | None:
    if not v or not isinstance(v, str):
        return None
    k = v.strip().lower()
    return k if k in PRESETS or k == "custom" else None


def resolve_theme(
    theme_id: str | None,
    overrides: dict,
) -> dict:
    """
    Returns a dict of safe CSS variable values for the checkout page.
    Presets define all base values; overrides replace specific keys.
    """
    base = PRESETS.get(theme_id or "dark_gold", PRESETS["dark_gold"]).copy()

    if _safe_hex(overrides.get("accent")):
        base["accent"] = _safe_hex(overrides["accent"])
    if _safe_hex(overrides.get("bg")):
        base["bg"] = _safe_hex(overrides["bg"])
    if _safe_hex(overrides.get("surface")):
        base["surface"] = _safe_hex(overrides["surface"])
    if _safe_hex(overrides.get("text")):
        base["text"] = _safe_hex(overrides["text"])
    if _safe_radius(overrides.get("radius")):
        base["radius"] = _safe_radius(overrides["radius"])
    if _safe_font(overrides.get("font")):
        base["font"] = _safe_font(overrides["font"])
    if _safe_button_style(overrides.get("button_style")):
        base["button_style"] = _safe_button_style(overrides["button_style"])

    base["font_stack"] = SAFE_FONTS.get(base["font"], SAFE_FONTS["system"])

    r = int(base.get("radius", "12"))
    base["radius_sm"]  = str(max(0, r - 4))
    base["radius_lg"]  = str(min(24, r + 4))
    base["radius_btn"] = {
        "rounded": str(r),
        "sharp":   "2",
        "pill":    "999",
    }.get(base.get("button_style", "rounded"), str(r))

    return base


def validate_theme_input(data: dict) -> dict:
    """
    Validate and sanitize all theme input fields.
    Returns dict of safe values (None for invalid/missing).
    """
    return {
        "theme_id":           _safe_theme_id(data.get("theme_id")),
        "theme_accent":       _safe_hex(data.get("theme_accent")),
        "theme_bg":           _safe_hex(data.get("theme_bg")),
        "theme_surface":      _safe_hex(data.get("theme_surface")),
        "theme_text":         _safe_hex(data.get("theme_text")),
        "theme_radius":       _safe_radius(data.get("theme_radius")),
        "theme_font":         _safe_font(data.get("theme_font")),
        "theme_button_style": _safe_button_style(data.get("theme_button_style")),
        "theme_checkout_title":    _safe_text(data.get("theme_checkout_title"), 80),
        "theme_checkout_subtitle": _safe_text(data.get("theme_checkout_subtitle"), 120),
        "theme_success_msg":       _safe_text(data.get("theme_success_msg"), 200),
        "theme_cancel_msg":        _safe_text(data.get("theme_cancel_msg"), 200),
        "theme_qr_position":       _safe_position(data.get("theme_qr_position")),
        "theme_cancel_position":   _safe_position(data.get("theme_cancel_position")),
        "theme_bg_image":          _safe_bg_image(data.get("theme_bg_image")),
        "theme_bg_overlay":        _safe_overlay(data.get("theme_bg_overlay")),
        "checkout_layout":         _safe_checkout_layout(data.get("checkout_layout")),
        "theme_v2_colors_json":    safe_v2_colors_json(data.get("theme_v2_colors_json")),
    }


_VALID_LAYOUTS = {
    "stripe", "receipt", "glass", "brutalist", "luxury", "playful",
    "newspaper", "cyberpunk", "swiss", "pixel", "boarding_pass", "zen",
    "aurora", "blueprint", "botanical", "eink", "fintech", "artdeco", "comic", "vaporwave",
    "delivery", "candy", "beauty", "princess", "boutique", "chalkboard", "gym", "vinyl",
}
_V2_LAYOUTS = _VALID_LAYOUTS - {"stripe"}

def _safe_checkout_layout(v):
    return v if v in _VALID_LAYOUTS else None


def safe_v2_colors_json(v: Any) -> str | None:
    """Validate {layout_id: {accent: '#hex', bg: '#hex'}, ...}. Unknown
    layout ids and non-hex values are dropped rather than rejecting the
    whole payload, so one bad entry can't block saving the rest."""
    if v is None:
        return None
    if isinstance(v, str):
        import json
        try:
            v = json.loads(v)
        except Exception:
            return None
    if not isinstance(v, dict):
        return None
    clean: dict[str, dict[str, str]] = {}
    for layout_id, colors in v.items():
        if layout_id not in _V2_LAYOUTS or not isinstance(colors, dict):
            continue
        entry = {}
        accent = _safe_hex(colors.get("accent"))
        bg = _safe_hex(colors.get("bg"))
        font = _safe_font(colors.get("font"))
        if accent:
            entry["accent"] = accent
        if bg:
            entry["bg"] = bg
        if font:
            entry["font"] = font
        if entry:
            clean[layout_id] = entry
    if not clean:
        return None
    import json
    return json.dumps(clean)


def _safe_position(v):
    """QR / cancel position: only 'top' or 'bottom'."""
    return v if v in ("top", "bottom") else None


def _safe_overlay(v):
    """Background overlay strength 0–95 (digits only)."""
    if v is None:
        return None
    s = re.sub(r"[^0-9]", "", str(v))
    if not s:
        return None
    n = max(0, min(95, int(s)))
    return str(n)


_BG_RE      = re.compile(r'^data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=]+)$')
_BG_MAX_IN  = 8 * 1024 * 1024   # 8 MB max raw upload
_BG_MAX_OUT = 2 * 1024 * 1024   # 2 MB max after compression


def _compress_bg(data: bytes) -> tuple[bytes, str]:
    """
    Resize to max 1920×1200 and re-encode as JPEG. Re-encoding strips all EXIF
    metadata and embedded payloads. Returns (compressed_bytes, 'image/jpeg').
    """
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img.load()
    img.thumbnail((1920, 1200), Image.LANCZOS)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")
    out = io.BytesIO()
    quality = 85
    img.save(out, format="JPEG", quality=quality, optimize=True)
    result = out.getvalue()
    while len(result) > _BG_MAX_OUT and quality > 60:
        quality -= 8
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        result = out.getvalue()
    return result, "image/jpeg"


def _safe_bg_image(v):
    """
    Validate, compress and return a checkout background image data URI.
    Accepts up to 8 MB raster input; compresses to JPEG ≤ 2 MB via Pillow
    (strips EXIF/metadata). SVG not allowed (script risk). Empty string = clear.
    """
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return ""
    m = _BG_RE.match(s)
    if not m:
        return None
    import base64 as _b64
    try:
        raw = _b64.b64decode(m.group(2), validate=True)
    except Exception:
        return None
    if len(raw) == 0 or len(raw) > _BG_MAX_IN:
        return None
    # Magic-byte check rejects executables renamed as images.
    ok = (raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:3] == b"\xff\xd8\xff"
          or raw[:6] in (b"GIF87a", b"GIF89a")
          or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"))
    if not ok:
        return None
    try:
        comp, out_mime = _compress_bg(raw)
    except Exception:
        return None
    if len(comp) > _BG_MAX_OUT:
        return None
    return f"data:{out_mime};base64,{_b64.b64encode(comp).decode()}"


def theme_from_user(user) -> dict:
    """Build resolved theme dict from a User model instance."""
    overrides = {
        "accent":       user.theme_accent,
        "bg":           user.theme_bg,
        "surface":      user.theme_surface,
        "text":         user.theme_text,
        "radius":       user.theme_radius,
        "font":         user.theme_font,
        "button_style": user.theme_button_style,
    }
    theme = resolve_theme(user.theme_id, overrides)
    theme["checkout_title"]    = _safe_text(user.theme_checkout_title, 80) or ""
    theme["checkout_subtitle"] = _safe_text(user.theme_checkout_subtitle, 120) or ""
    theme["success_msg"]       = _safe_text(user.theme_success_msg, 200) or ""
    theme["cancel_msg"]        = _safe_text(user.theme_cancel_msg, 200) or ""
    theme["qr_position"]       = _safe_position(user.theme_qr_position) or "bottom"
    theme["cancel_position"]   = _safe_position(user.theme_cancel_position) or "bottom"
    theme["bg_image"]          = _safe_bg_image(user.theme_bg_image) or ""
    theme["bg_overlay"]        = _safe_overlay(user.theme_bg_overlay) or "70"
    theme["theme_id"]          = user.theme_id or "dark_gold"
    raw_layout = getattr(user, "checkout_layout", None) or "stripe"
    theme["checkout_layout"] = raw_layout if raw_layout in _VALID_LAYOUTS else "stripe"
    theme["v2_colors"] = _parse_v2_colors(getattr(user, "theme_v2_colors_json", None))
    return theme


def _parse_v2_colors(raw: str | None) -> dict:
    if not raw:
        return {}
    import json
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
