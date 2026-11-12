import re
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup, escape

_ICON_DIR = Path(__file__).resolve().parents[2] / "static" / "icons"
_NAME_RE = re.compile(r"^[a-z0-9-]{1,48}$")


@lru_cache(maxsize=256)
def _icon_body(name: str) -> str:
    if not _NAME_RE.match(name):
        return ""
    path = _ICON_DIR / f"{name}.svg"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def icon(name: str, cls: str = "ico", size: int | None = None, stroke: float = 2) -> Markup:
    body = _icon_body(name)
    if not body:
        return Markup("")
    dims = f' width="{int(size)}" height="{int(size)}"' if size else ""
    return Markup(
        f'<svg class="{escape(cls)}"{dims} viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="{float(stroke):g}" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true" focusable="false">{body}</svg>'
    )
