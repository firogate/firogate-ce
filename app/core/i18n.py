"""
FiroGate Internal i18n core (server-side helpers).

Fully local. No external services, no CDN translation APIs.

What it provides
──
1.  `get_lang(request)`        picks the active language from the
                                  `fg_lang` cookie, then `Accept-Language`,
                                  then DEFAULT_LANGUAGE.
2.  `is_rtl(lang)`              True for RTL languages (currently ar).
3.  `register_jinja(templates)` injects `lang`, `dir`, `is_rtl`,
                                  `SUPPORTED_LANGS` into every render
                                  and exposes a `t(key)` Jinja global so
                                  templates may use server-rendered text
                                  for the FIRST paint (the JS engine then
                                  keeps everything in sync after that).
4.  `load_bundle(lang)`         loads `static/i18n/{lang}.json` from disk
                                  with an in-process cache.

The translation file format is a plain {English-source: Translated} map.
This is the same format the client-side engine consumes, so server and
client share ONE source of truth.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Dict

from fastapi import Request
from fastapi.templating import Jinja2Templates

SUPPORTED_LANGS = ["en", "ar", "ru", "de", "zh"]
RTL_LANGS = {"ar"}
DEFAULT_LANGUAGE = os.environ.get("DEFAULT_LANGUAGE", "en")

LANG_META = {
    "en": {"name": "English",  "native": "English",       "flag": "🇬🇧", "dir": "ltr"},
    "ar": {"name": "Arabic",   "native": "العربية",        "flag": "🇸🇦", "dir": "rtl"},
    "ru": {"name": "Russian",  "native": "Русский",        "flag": "🇷🇺", "dir": "ltr"},
    "de": {"name": "German",   "native": "Deutsch",        "flag": "🇩🇪", "dir": "ltr"},
    "zh": {"name": "Chinese",  "native": "简体中文",        "flag": "🇨🇳", "dir": "ltr"},
}

_BUNDLE_DIR = os.path.join("static", "i18n")


def _normalize(code: str) -> str:
    code = (code or "").lower().strip()
    if not code:
        return ""
    # "zh-CN", "zh_Hans" → "zh"  ;  "en-US" → "en"
    head = code.split(",")[0].split(";")[0].replace("_", "-").split("-")[0]
    return head if head in SUPPORTED_LANGS else ""


def get_lang(request: Request) -> str:
    # Priority: explicit ?lang= override (single-request preview), then
    # the fg_lang cookie, then the Accept-Language header.
    q = request.query_params.get("lang")
    if q:
        n = _normalize(q)
        if n:
            return n
    c = request.cookies.get("fg_lang")
    if c:
        n = _normalize(c)
        if n:
            return n
    h = request.headers.get("accept-language", "")
    if h:
        for part in h.split(","):
            n = _normalize(part)
            if n:
                return n
    return DEFAULT_LANGUAGE if DEFAULT_LANGUAGE in SUPPORTED_LANGS else "en"


def is_rtl(lang: str) -> bool:
    return lang in RTL_LANGS


@lru_cache(maxsize=16)
def load_bundle(lang: str) -> Dict[str, str]:
    if lang not in SUPPORTED_LANGS:
        lang = "en"
    path = os.path.join(_BUNDLE_DIR, f"{lang}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return {}


def t(key: str, lang: str | None = None) -> str:
    """Server-side translator. Returns the translated string or the key itself
    when no translation is found (English source acts as the natural fallback)."""
    if not key:
        return key
    if not lang or lang not in SUPPORTED_LANGS:
        lang = "en"
    if lang == "en":
        return key
    bundle = load_bundle(lang)
    return bundle.get(key, key)


def register_jinja(templates: Jinja2Templates) -> None:
    """Expose i18n primitives to every template render. The actual `lang`
    and `dir` are injected by main.page() (it knows the request)."""
    templates.env.globals.setdefault("SUPPORTED_LANGS", SUPPORTED_LANGS)
    templates.env.globals.setdefault("LANG_META", LANG_META)
    templates.env.globals.setdefault("DEFAULT_LANGUAGE", DEFAULT_LANGUAGE)

    # A no-arg `t` global that picks lang up from the template context.
    def _t(key, ctx_lang=None):
        return t(key, ctx_lang)
    templates.env.globals.setdefault("t", _t)
