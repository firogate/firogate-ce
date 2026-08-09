from functools import lru_cache
from io import BytesIO
from pathlib import Path

import qrcode
import qrcode.constants
from PIL import Image

_LOGO_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "im"
_LOGO_SVG_PATH = _LOGO_DIR / "qr-logo.svg"
_LOGO_PNG_PATH = _LOGO_DIR / "qr-logo.png"

_BOX_SIZE = 10
_BORDER = 2
_LOGO_MODULES = 9


@lru_cache(maxsize=1)
def _load_logo() -> Image.Image | None:
    if _LOGO_SVG_PATH.is_file():
        try:
            import cairosvg
            png_bytes = cairosvg.svg2png(url=str(_LOGO_SVG_PATH), output_width=512, output_height=512)
            return Image.open(BytesIO(png_bytes)).convert("RGBA")
        except Exception:
            pass
    if _LOGO_PNG_PATH.is_file():
        return Image.open(_LOGO_PNG_PATH).convert("RGBA")
    return None


def make_payment_qr_png(data: str) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=_BOX_SIZE,
        border=_BORDER,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")

    logo = _load_logo()
    if logo is not None:
        img = _paste_logo(img, logo, qr.modules_count)

    buffer = BytesIO()
    img.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def _paste_logo(qr_img: Image.Image, logo: Image.Image, modules_count: int) -> Image.Image:
    logo_px = _LOGO_MODULES * _BOX_SIZE
    resized_logo = logo.resize((logo_px, logo_px), Image.LANCZOS)

    quiet_px = _BORDER * _BOX_SIZE
    off_modules = (modules_count - _LOGO_MODULES) // 2
    px = quiet_px + off_modules * _BOX_SIZE
    py = quiet_px + off_modules * _BOX_SIZE

    qr_img.paste(resized_logo, (px, py), resized_logo)
    return qr_img
