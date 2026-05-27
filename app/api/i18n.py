"""
FiroGate — i18n API endpoints.

Two routes:
  • GET  /api/i18n/{lang}.json — serves the translation bundle (also
                                  available as a plain static file at
                                  /static/i18n/{lang}.json, which the
                                  client engine uses as the primary
                                  source. This route exists so the bundle
                                  can be served with proper cache headers
                                  and so server-side flows can hot-reload
                                  it without a restart).
  • POST /api/i18n/set         — sets the fg_lang cookie. Returns JSON
                                  echoing the chosen language so the
                                  client can flip the UI without a reload.

All bundles live on disk under static/i18n/ — no external service is ever
contacted.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import JSONResponse

from app.core.i18n import (
    SUPPORTED_LANGS,
    LANG_META,
    load_bundle,
    is_rtl,
)

router = APIRouter(prefix="/api/i18n", tags=["i18n"])


@router.get("/langs")
def list_languages():
    """Public list of supported languages + metadata for switcher UI."""
    return {
        "supported": SUPPORTED_LANGS,
        "languages": [
            {"code": c, **LANG_META[c], "rtl": is_rtl(c)} for c in SUPPORTED_LANGS
        ],
    }


@router.get("/{lang}.json")
def get_bundle(lang: str):
    if lang not in SUPPORTED_LANGS:
        raise HTTPException(status_code=404, detail="language not supported")
    data = load_bundle(lang)
    # Translations don't contain secrets — long cache is fine; client also
    # has its own in-memory cache keyed by lang code.
    return JSONResponse(
        content=data,
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Language": lang,
        },
    )


@router.post("/set")
async def set_language(request: Request):
    """Persist the user's language choice in the fg_lang cookie. Reads
    the body as JSON `{"lang": "<code>"}` *or* the query param `lang`."""
    code = request.query_params.get("lang", "")
    if not code:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            code = (body.get("lang") or "").strip()
    code = (code or "").strip().lower()
    if code not in SUPPORTED_LANGS:
        raise HTTPException(status_code=400, detail="unsupported language")

    # Cookie scoped to the registrable domain so the choice carries
    # across firogate.com + dashboard.firogate.com + checkout.firogate.com
    resp = JSONResponse({
        "ok": True,
        "lang": code,
        "dir": "rtl" if is_rtl(code) else "ltr",
        **LANG_META.get(code, {}),
    })
    # Derive parent domain (".firogate.com") when applicable; safe no-op
    # on localhost / IPs (cookies stay host-only).
    host = (request.headers.get("host", "") or "").split(":")[0].lower()
    cookie_domain = ""
    parts = host.split(".")
    if (
        len(parts) >= 2
        and host not in ("localhost", "127.0.0.1", "::1")
        and not host.replace(".", "").isdigit()
    ):
        cookie_domain = "." + ".".join(parts[-2:])
    resp.set_cookie(
        key="fg_lang",
        value=code,
        max_age=60 * 60 * 24 * 365,        # 1 year
        path="/",
        domain=cookie_domain or None,
        secure=request.url.scheme == "https",
        httponly=False,                     # readable by client (display only)
        samesite="lax",
    )
    return resp
