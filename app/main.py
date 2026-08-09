import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from app.core.config import get_settings
from app.core.version import API_VERSION, APP_VERSION
from app.core.database import create_tables
from app.core.rate_limit import rate_limit_relaxed
from app.core import i18n as fg_i18n
from app.api import auth, auth_fb, telegram_webhook, payments, users, analytics
from app.api import spark_connect
from app.api import wallet_auth
from app.api import i18n as i18n_api
from app.api import payment_links
from app.api import events as events_api
from app.api import theme as theme_api
from app.api import api_keys as api_keys_api
from app.api import reports as reports_api
from app.api import panel_tools_api
from app.api import internal as internal_api

# Security-headers middleware. Pure ASGI (not BaseHTTPMiddleware) so it never
# buffers/re-wraps the response body — BaseHTTPMiddleware's call_next() opens
# a task group around the whole response, which raises spurious
# LocalProtocolError/"No response returned" noise on a long-lived
# StreamingResponse (SSE) when the client disconnects mid-stream. Setting
# headers on the raw http.response.start event sidesteps that entirely.
class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        is_https = scope.get("scheme") == "https"

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                existing = {k.decode("latin-1").lower() for k, _ in headers}

                def _setdefault(name: str, value: str):
                    if name.lower() not in existing:
                        headers.append((name.encode("latin-1"), value.encode("latin-1")))

                _setdefault("X-Content-Type-Options", "nosniff")
                _setdefault("X-Frame-Options", "DENY")
                _setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                _setdefault("Permissions-Policy",
                            "camera=(self), microphone=(), geolocation=(), payment=()")
                _setdefault("Content-Security-Policy",
                            "default-src 'self'; "
                            "script-src 'self' 'unsafe-inline' https://www.gstatic.com https://apis.google.com https://challenges.cloudflare.com https://cdnjs.cloudflare.com; "
                            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                            "font-src 'self' https://fonts.gstatic.com data:; "
                            "img-src 'self' data: https:; "
                            "connect-src 'self' https: wss:; "
                            "frame-src https://challenges.cloudflare.com; "
                            "frame-ancestors 'none'; "
                            "base-uri 'self'; "
                            "form-action 'self'")
                if is_https:
                    _setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            await send(message)

        await self.app(scope, receive, send_wrapper)


# Cache policy for static assets. Starlette's StaticFiles sends ETag +
# Last-Modified but NO Cache-Control, so browsers fall back to heuristic
# caching and serve stale copies without ever revalidating mobile
# browsers kept running week-old main.css/i18n.js (with long-fixed bugs)
# until the user manually cleared their cache, because the ?v= cache-buster
# in base.html only helps when someone remembers to bump it after every
# edit. Policy:
#   • CSS/JS/JSON no-cache: browser may store but must revalidate each
#     load; with ETags already present that's a cheap conditional request
#     answered 304 when unchanged, and a fresh copy the moment a file
#     actually changes. Bug fixes now propagate on a normal reload.
#   • images/fonts immutable-ish long cache; they're big, rarely edited,
#     and get new filenames when they do change.
# Pure ASGI, same SSE-safety reason as SecurityMiddleware above, and skips
# non-/static/ requests (SSE included) before ever touching the send channel.
class StaticCachePolicyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/static/"):
            return await self.app(scope, receive, send)

        path = scope["path"]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                if path.endswith((".css", ".js", ".json")):
                    headers.append((b"Cache-Control", b"no-cache"))
                elif path.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
                                     ".ico", ".woff", ".woff2", ".ttf")):
                    existing = {k.decode("latin-1").lower() for k, _ in headers}
                    if "cache-control" not in existing:
                        headers.append((b"Cache-Control", b"public, max-age=604800"))
            await send(message)

        await self.app(scope, receive, send_wrapper)


settings  = get_settings()
templates = Jinja2Templates(directory="templates")
scheduler = AsyncIOScheduler(timezone="UTC")
fg_i18n.register_jinja(templates)


# ── Static asset cache-buster ────────────────────────────────────────────────
# Appends the file's mtime to any /static/... path so browsers fetch a fresh
# copy the moment you replace an image (or CSS/JS) on disk. Usage in Jinja:
#     <img src="{{ '/static/im/logo.png'|bust }}">
def _bust(path: str) -> str:
    try:
        import os
        if not isinstance(path, str) or not path.startswith("/static/"):
            return path
        # Strip any existing query, we'll append our own.
        clean = path.split("?", 1)[0]
        fs_path = os.path.join(".", clean.lstrip("/"))
        if os.path.isfile(fs_path):
            mtime = int(os.path.getmtime(fs_path))
            return f"{clean}?v={mtime}"
    except Exception:
        pass
    return path


templates.env.filters["bust"] = _bust


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {settings.APP_NAME}")
    logger.info(f"BASE_URL : {settings.BASE_URL}")

    # Cross-subdomain cookie diagnostics helps diagnose "logged in on example.com
    # but not on dashboard.example.com" type issues at a glance.
    try:
        from app.core.security import _derive_cookie_domain
        eff_domain = (settings.COOKIE_DOMAIN or "").strip() or _derive_cookie_domain(settings.BASE_URL)
        logger.info(f"COOKIE_DOMAIN (effective): {eff_domain or '(none host-only cookies)'}")
        if settings.DASHBOARD_URL:
            logger.info(f"DASHBOARD_URL: {settings.DASHBOARD_URL}")
        if not eff_domain or not eff_domain.startswith("."):
            logger.warning(
                "Cookie is not scoped to a shared parent domain - sessions "
                "will NOT be shared across subdomains. Set COOKIE_DOMAIN=.example.com in .env"
            )
    except Exception as _e:
        logger.debug(f"cookie-domain diagnostics skipped: {_e}")
    if settings.ONION_URL:
        logger.info(f"ONION_URL: {settings.ONION_URL}")
    if settings.TOR_ENABLED:
        logger.info(f"Tor SOCKS5 enabled at socks5://127.0.0.1:{settings.TOR_SOCKS_PORT}")
        if settings.TOR_ALL_TRAFFIC:
            logger.info("   All outbound traffic routed through Tor")
        else:
            logger.info("   Only .onion webhooks + RPC routed through Tor")
    else:
        logger.warning("Tor disabled - .onion webhook calls will fail if not on same host")

    if not settings.PANEL_REQUIRE_2FA:
        logger.warning("PANEL_REQUIRE_2FA=false - 2FA not enforced")
    else:
        logger.info("Operator 2FA: required")

    await create_tables()
    await _seed()


    from app.core.security import validate_encryption_key_on_startup
    validate_encryption_key_on_startup()

    from app.core.rate_limit import log_rate_limiter_info
    log_rate_limiter_info()

    from app.services.payment_monitor import check_pending_payments
    scheduler.add_job(check_pending_payments, "interval", seconds=12, id="payment_monitor")

    from app.services.payment_engine import check_spark_payments
    scheduler.add_job(check_spark_payments, "interval", seconds=20, id="spark_scanner")

    from app.services.webhook import retry_failed_webhooks
    scheduler.add_job(retry_failed_webhooks, "interval", seconds=120, id="webhook_retry")

    from app.services.db_cleanup import run_db_cleanup
    scheduler.add_job(run_db_cleanup, "cron", hour=3, minute=0, id="db_cleanup")

    from app.services.accounting_verification import run_scheduled_verification
    scheduler.add_job(run_scheduled_verification, "cron", hour="*/6", minute=0, id="accounting_check")

    scheduler.start()
    logger.info("Schedulers started")

    try:
        from app.services.telegram_bot import setup_webhook
        import asyncio as _tg_asyncio
        _tg_asyncio.create_task(setup_webhook())
    except Exception:
        pass

    yield
    scheduler.shutdown()
    from app.services.firo_rpc import get_rpc
    try:
        await get_rpc().close()
    except Exception:
        pass


async def _seed():
    from app.core.database import AsyncSessionLocal
    from app.core.security import hash_password, generate_api_key
    from app.models.models import User, UserRole
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        # ── Operator via OPERATOR_EMAILS (preferred) ────────────────────
        # Any existing user whose email is listed in OPERATOR_EMAILS is
        # promoted to operator on every startup. Brand new operators
        # don't exist yet they'll be promoted the moment they register
        # / log in with their listed Gmail (see register_with_email /
        # login_with_email / google sign-in paths).
        operator_emails = settings.operator_email_set
        operator_usernames = settings.operator_username_set
        promoted = 0
        if operator_emails or operator_usernames:
            from sqlalchemy import or_
            conditions = []
            if operator_emails:
                conditions.append(User.email.in_(list(operator_emails)))
            if operator_usernames:
                conditions.append(User.username.in_(list(operator_usernames)))
            res = await db.execute(select(User).where(or_(*conditions)))
            for u in res.scalars().all():
                if u.role != UserRole.operator:
                    u.role = UserRole.operator
                    db.add(u)
                    promoted += 1
            if promoted:
                logger.info(f"[seed] Promoted {promoted} operator account(s)")

        # ── Legacy username/password operator seed ──────────────────────
        # Runs ONLY when ADMIN_USERNAME *and* ADMIN_PASSWORD are both set.
        # Skipped entirely when OPERATOR_EMAILS is the only operator source.
        if settings.ADMIN_USERNAME and settings.ADMIN_PASSWORD:
            res = await db.execute(
                select(User).where(User.username == settings.ADMIN_USERNAME.lower())
            )
            if not res.scalar_one_or_none():
                db.add(User(
                    username=settings.ADMIN_USERNAME.lower(),
                    email=(settings.ADMIN_EMAIL or f"{settings.ADMIN_USERNAME}@example.com"),
                    hashed_password=hash_password(settings.ADMIN_PASSWORD),
                    role=UserRole.operator,
                    api_key=generate_api_key(),
                    requests_total=999999,
                ))
                logger.info(f"Operator account created")

        await db.commit()
    logger.info("Seed complete")


app = FastAPI(
    title="FiroGate",
    version="1.0.0",
    docs_url="/api/docs" if settings.DEBUG else None,
    redoc_url=None,
    lifespan=lifespan,
)


# Translate HTTPException.detail into the requester's language before it
# goes out. This is the ONE place that needs to know about translation for
# every error raised anywhere in the API none of the ~170 raise sites
# elsewhere in the codebase need to change, and any new HTTPException added
# in the future is covered automatically for free. Reuses the exact same
# static/i18n/{lang}.json bundles the client-side engine uses (single
# source of truth, per app/core/i18n.py) a string only translates if a
# matching key already exists there; anything untranslated silently falls
# back to the original English detail, so this can never turn a real error
# into a blank/broken response.
from starlette.exceptions import HTTPException as _StarletteHTTPException
from fastapi.exception_handlers import http_exception_handler as _default_http_exception_handler

@app.exception_handler(_StarletteHTTPException)
async def _translated_http_exception_handler(request: Request, exc: _StarletteHTTPException):
    if isinstance(exc.detail, str) and exc.detail:
        try:
            lang = fg_i18n.get_lang(request)
            exc.detail = fg_i18n.t(exc.detail, lang)
        except Exception:
            pass
    return await _default_http_exception_handler(request, exc)


# Dashboard/cookie origins (credentialed) kept tight.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=r"https?://.*\.onion(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# CSRF guard for cookie-authenticated, state-changing requests.
#
# The access_token cookie is SameSite=Lax (or no SameSite restriction at all
# on plain-HTTP/Tor deployments see _cookie_kwargs) and, when COOKIE_DOMAIN
# is set, shared across every subdomain (dashboard/checkout/plan/api). Lax
# still allows the cookie on cross-site *and* cross-subdomain simple
# POST/PUT/PATCH/DELETE requests (e.g. a same-site XHR/fetch from a
# compromised checkout-subdomain script, or a plain <form> submit from any
# origin). Bearer-token and X-API-Key auth are immune to this (an attacker
# page cannot attach those headers to a cross-origin request without a
# CORS-approved preflight), so this check only fires for requests that rely
# solely on the cookie.
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _csrf_allowed_hosts() -> set[str]:
    from urllib.parse import urlparse
    hosts: set[str] = set()
    for url in settings.allowed_origins:
        try:
            h = urlparse(url).hostname
            if h:
                hosts.add(h.lower())
        except Exception:
            pass
    hosts.update({"localhost", "127.0.0.1"})
    return hosts


_CSRF_ALLOWED_HOSTS = _csrf_allowed_hosts()


@app.middleware("http")
async def _csrf_guard(request, call_next):
    if request.method not in _CSRF_SAFE_METHODS:
        has_cookie_auth = bool(request.cookies.get("access_token"))
        has_header_auth = bool(
            request.headers.get("authorization") or request.headers.get("x-api-key")
        )
        if has_cookie_auth and not has_header_auth:
            from urllib.parse import urlparse
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            host = ""
            if origin:
                try:
                    host = (urlparse(origin).hostname or "").lower()
                except Exception:
                    host = ""
            is_onion = host.endswith(".onion")
            allowed = (
                not origin  # some legitimate same-origin requests omit both headers
                or is_onion
                or host in _CSRF_ALLOWED_HOSTS
                or any(host == h or host.endswith("." + h) for h in _CSRF_ALLOWED_HOSTS if h not in ("localhost", "127.0.0.1"))
            )
            if not allowed:
                from starlette.responses import JSONResponse
                return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
    return await call_next(request)


# Wallets connect with X-API-Key (no cookies) from app origins like
# capacitor://localhost, http://localhost, ionic://localhost, or a hosted web
# wallet. These requests are NOT credentialed (no cookies), so it is safe to
# accept them broadly auth is enforced by the API key, not the origin.
@app.middleware("http")
async def _wallet_cors(request, call_next):
    path = request.url.path
    is_api_key_route = (
        path.startswith("/api/wallet")
        or path.startswith("/api/connect")
        or path.startswith("/auth/wallet")
        or (path.startswith("/api/payments") and request.headers.get("x-api-key"))
    )
    if request.method == "OPTIONS" and is_api_key_route:
        from starlette.responses import Response
        resp = Response(status_code=204)
    else:
        resp = await call_next(request)
    if is_api_key_route:
        origin = request.headers.get("origin")
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
            resp.headers["Vary"] = "Origin"
    return resp


if settings.TRUST_PROXY_HEADERS:
    try:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
        _trusted = [h.strip() for h in (os.environ.get("TRUSTED_PROXY_IPS", "127.0.0.1") or "127.0.0.1").split(",") if h.strip()]
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=_trusted)
        logger.info("ProxyHeadersMiddleware enabled (TRUST_PROXY_HEADERS=true)")
    except ImportError:
        pass

app.add_middleware(SecurityMiddleware)

# Host-header validation: reject requests whose Host isn't a configured domain
# (prevents Host-header poisoning). Falls back to '*' only on the localhost
# default so local dev / Docker healthchecks aren't broken. Onion + proxy hosts
# are included via allowed_hosts. Wildit subdomains are matched by Starlette.
try:
    _hosts = settings.allowed_hosts
    if _hosts != ["*"]:
        # allow subdomains of each configured host too
        _wild = []
        for _h in _hosts:
            _wild.append(_h)
            if _h not in ("localhost", "127.0.0.1") and not _h.startswith("*."):
                _wild.append("*." + _h)
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(dict.fromkeys(_wild)))
        logger.info(f"TrustedHostMiddleware enabled: {_wild}")
    else:
        logger.info("TrustedHostMiddleware skipped (localhost default dev mode)")
except Exception as _e:
    logger.warning(f"TrustedHostMiddleware not enabled: {_e}")

# Privacy Middleware - Must be added before SecurityMiddleware processes the request
from app.core.privacy_middleware import PrivacyMiddleware
app.add_middleware(PrivacyMiddleware)
app.add_middleware(StaticCachePolicyMiddleware)


# i18n bundles are edited frequently as translations are added/fixed. Serve
# them with no-cache (always revalidate via ETag/Last-Modified cheap, a
# 304 when unchanged) so a browser or CDN that cached an older ar.json
# before new keys were added doesn't keep showing stale/missing translations
# indefinitely. Registered before the generic /static mount so it wins.
@app.get("/static/i18n/{lang}.json")
async def _i18n_bundle(lang: str):
    import re as _re
    from pathlib import Path as _Path
    from fastapi import HTTPException as _HTTPException
    from fastapi.responses import FileResponse
    safe_lang = _re.sub(r"[^a-z]", "", lang.lower())[:8]
    path = _Path("static/i18n") / f"{safe_lang}.json"
    if not path.is_file():
        raise _HTTPException(404, "unknown language")
    resp = FileResponse(path, media_type="application/json")
    resp.headers["Cache-Control"] = "no-cache"
    return resp

app.mount("/static", StaticFiles(directory="static"), name="static")


app.include_router(auth.router)
app.include_router(auth_fb.router)
app.include_router(telegram_webhook.router)
app.include_router(payments.public_router)
app.include_router(payments.router)
app.include_router(spark_connect.router)
app.include_router(wallet_auth.router)
app.include_router(panel_tools_api.router)
app.include_router(internal_api.router)

app.include_router(users.router)
app.include_router(reports_api.router)
app.include_router(analytics.router)
app.include_router(i18n_api.router)
app.include_router(payment_links.router)
app.include_router(events_api.router)
app.include_router(theme_api.router)
app.include_router(api_keys_api.router)

@app.get("/api/health")
async def health():
    """Public-facing status + (best-effort) live Firo node stats.

    `status` is always "ok" as long as the API itself is up that part
    never depended on the node. block_height/peers/mempool_size/version/
    sync_progress are populated from the node when reachable; they're
    simply omitted (not faked) when the node is offline or slow, so the
    dashboard's node-health widget correctly falls back to "—" instead
    of showing stale or fabricated numbers.
    """
    out = {"status": "ok"}
    try:
        import asyncio
        from app.services.firo_rpc import get_rpc
        rpc = get_rpc()
        chain_info, net_info, mempool_info = await asyncio.gather(
            rpc.get_blockchain_info(),
            rpc.get_network_info(),
            rpc.call("getmempoolinfo"),
            return_exceptions=True,
        )
        if isinstance(chain_info, dict):
            out["block_height"] = chain_info.get("blocks")
            out["sync_progress"] = chain_info.get("verificationprogress")
            # Ground truth from the node itself (not our own config) lets
            # the operator catch a misconfigured deployment, e.g. FiroGate set
            # to testnet mode but actually pointed at a mainnet node.
            out["chain"] = chain_info.get("chain")
        if isinstance(net_info, dict):
            out["peers"] = net_info.get("connections")
            out["version"] = net_info.get("subversion", "").strip("/")
        if isinstance(mempool_info, dict):
            out["mempool_size"] = mempool_info.get("size")
    except Exception:
        pass
    return out


# ── Price feed endpoint ────────────────────────────────────────────────────────
@app.get("/api/price", dependencies=[Depends(rate_limit_relaxed)])
async def get_price(fresh: bool = False):
    """
    Returns current FIRO/USDT price from MEXC.
    ?fresh=true  → bypass cache (checkout)
    ?fresh=false → use cache   (home, dashboard)
    """
    from app.services.price_service import get_firo_price
    price = await get_firo_price(fresh=fresh)
    if price is None:
        from fastapi.responses import JSONResponse
        return JSONResponse({"price": None, "error": "Price unavailable"}, status_code=503)
    return {"price": price, "symbol": "FIRO/USDT", "source": "MEXC"}


def _get_host(r: Request) -> str:
    """Return the effective hostname, respecting proxy headers."""
    if settings.TRUST_PROXY_HEADERS:
        return r.headers.get("x-forwarded-host", "") or r.headers.get("host", "")
    return r.headers.get("host", "")


def _is_onion_request(r: Request) -> bool:
    """
    Return True when the request arrived via a Tor hidden service.

    Detection is done in two ways both independent of whether ONION_URL
    is configured in .env:
      1. The host header contains '.onion'  (primary always reliable)
      2. Proxy set X-Onion-Request: true    (secondary fallback header)

    This means onion routing works correctly even if the operator forgot to
    set ONION_URL in .env.
    """
    if ".onion" in _get_host(r):
        return True
    if r.headers.get("x-onion-request", "").lower() == "true":
        return True
    return False


def _get_request_base_url(r: Request) -> str:
    """Return the effective base URL for this request, onion-aware."""
    if _is_onion_request(r):
        if settings.ONION_URL:
            return settings.ONION_URL.rstrip("/")
        # ONION_URL not configured reconstruct from the host header so the
        # operator still gets correct behaviour without extra .env config.
        host   = _get_host(r)
        scheme = r.headers.get("x-forwarded-proto", "http")
        return f"{scheme}://{host}"
    return settings.BASE_URL.rstrip("/")


def _should_redirect_to_subdomain(r: Request) -> bool:
    """
    Return True only when the request arrived on the configured main domain
    and we should redirect to a subdomain (dashboard.example.com etc.).

    Returns False (serve directly) when:
      - Tor/onion:  request came via .onion hidden service
      - Local/dev:  request came from localhost / 127.0.0.1 (operator setup, dev)
    """
    # Check the host header directly never rely on ONION_URL being set.
    if _is_onion_request(r):
        return False
    host = _get_host(r).lower().split(":")[0]   # strip port
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False
    return True



def _is_dashboard_host(r: Request) -> bool:
    """True when request comes from dashboard.example.com (or equivalent)."""
    if not settings.DASHBOARD_URL:
        return False
    host = _get_host(r)
    from urllib.parse import urlparse
    dashboard_host = urlparse(settings.DASHBOARD_URL).netloc
    return host == dashboard_host


async def _get_auth_pages_hidden_cached(r: Request) -> bool:
    cached = getattr(r.state, "_auth_pages_hidden", None)
    if cached is not None:
        return cached
    hidden = await _auth_pages_hidden()
    r.state._auth_pages_hidden = hidden
    return hidden


async def page_async(name: str, r: Request, **ctx):
    ctx.setdefault("auth_pages_hidden", await _get_auth_pages_hidden_cached(r))
    return page(name, r, **ctx)


def page(name: str, r: Request, **ctx):
    base_url    = _get_request_base_url(r)
    _via_onion  = _is_onion_request(r)          # host-header check no ONION_URL dependency
    _host_plain = _get_host(r).lower().split(":")[0]
    _is_local   = _host_plain in ("localhost", "127.0.0.1", "::1", "0.0.0.0")

    if _via_onion:
        # Tor/onion session keep ALL navigation within the .onion domain.
        # Never redirect to clearnet subdomains (dashboard.example.com etc.)
        # Use ONION_URL if configured; fall back to the reconstructed base_url.
        _onion = (settings.ONION_URL.rstrip("/") if settings.ONION_URL else base_url)
        dashboard_url = _onion + "/dashboard"
        checkout_url  = _onion
        api_url       = _onion
        login_url     = _onion + "/login"
    elif _is_local:
        # Local / direct access (operator dev/setup, LAN) use path-relative URLs
        # so "← Back to Dashboard" never redirects off-server to a clearnet subdomain.
        dashboard_url = "/dashboard"
        checkout_url  = ""
        api_url       = ""
        login_url     = "/login"
    else:
        dashboard_url = settings.DASHBOARD_URL or (settings.BASE_URL.rstrip("/") + "/dashboard")
        checkout_url  = settings.CHECKOUT_URL  or settings.BASE_URL.rstrip("/")
        api_url       = settings.API_URL       or settings.BASE_URL.rstrip("/")
        login_url     = settings.login_url

    resp = templates.TemplateResponse(name, {
        "request":      r,
        "settings":     settings,
        "base_url":     base_url,
        "is_onion":     _via_onion,
        "is_testnet":   settings.is_testnet,
        # ── i18n: active language + direction injected into every template ──
        "lang":         fg_i18n.get_lang(r),
        "dir":          ("rtl" if fg_i18n.is_rtl(fg_i18n.get_lang(r)) else "ltr"),
        "is_rtl":       fg_i18n.is_rtl(fg_i18n.get_lang(r)),
        "supported_langs": fg_i18n.SUPPORTED_LANGS,
        "lang_meta":    fg_i18n.LANG_META,
        # ── subdomain URLs injected into every template ──
        "dashboard_url": dashboard_url,
        "checkout_url":  checkout_url,
        "api_url":       api_url,
        "login_url":     login_url,
        "panel_require_2fa": settings.PANEL_REQUIRE_2FA,
        # ── public Firebase config (safe to expose) ──
        "firebase_config": {
            "apiKey":     settings.FIREBASE_API_KEY,
            "authDomain": settings.FIREBASE_AUTH_DOMAIN,
            "projectId":  settings.FIREBASE_PROJECT_ID,
            "appId":      settings.FIREBASE_APP_ID,
        },
        "firebase_enabled":  bool(settings.FIREBASE_API_KEY and settings.FIREBASE_PROJECT_ID),
        "google_client_id":  settings.GOOGLE_CLIENT_ID,
        **ctx,
    })
    # Critical: prevent the browser from caching authenticated HTML.
    # A cached stale dashboard/login page was the root cause of the
    # dashboard ↔ /login navigation loop browsers were re-serving an
    # older template that did not contain our loop-guard.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.get("/favicon.ico")
async def favicon():
    import os
    from fastapi.responses import FileResponse, Response
    path = os.path.join("static", "favicon.ico")
    if os.path.exists(path):
        return FileResponse(path)
    return Response(status_code=204)
# ── robots.txt ────────────────────────────────────────────────────────────────
@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    import os
    path = os.path.join("static", "robots.txt")
    if os.path.exists(path):
        with open(path) as f:
            return PlainTextResponse(f.read(), media_type="text/plain")
    return PlainTextResponse("User-agent: *\nAllow: /\n", media_type="text/plain")


# ── Home ──────────────────────────────────────────────────────────────────────
async def _is_operator_request(r: Request) -> bool:
    """Server-side operator check used to gate the hidden panel URL out of
    HTML rendered for merchant accounts. Reads the access_token cookie,
    looks up the user, returns True only when role == operator."""
    try:
        from app.core.security import verify_access_token
        from app.core.database import AsyncSessionLocal
        from app.models.models import User, UserRole
        from sqlalchemy import select
        tok = r.cookies.get("access_token") or ""
        if not tok:
            return False
        uid = verify_access_token(tok)
        if not uid:
            return False
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(User).where(User.id == uid))
            u = res.scalar_one_or_none()
            if not u or not u.is_active:
                return False
            if u.role == UserRole.operator:
                return True
            if settings.is_operator_email(u.email) or settings.is_operator_username(u.username):
                return True
        return False
    except Exception:
        return False


async def _dashboard_show_market_price(r: Request) -> bool:
    """Server-side lookup so the currency switcher can be baked into the
    initial dashboard HTML as hidden/shown correctly from the first paint —
    without this, it would flash visible before the client-side profile
    fetch resolves and hides it."""
    try:
        from app.core.security import verify_access_token
        from app.core.database import AsyncSessionLocal
        from app.models.models import User
        from sqlalchemy import select
        tok = r.cookies.get("access_token") or ""
        if not tok:
            return False
        uid = verify_access_token(tok)
        if not uid:
            return False
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(User.show_market_price).where(User.id == uid))
            row = res.scalar_one_or_none()
            return bool(row)
    except Exception:
        return False


@app.get("/", response_class=HTMLResponse)
async def home(r: Request):
    # If request is from dashboard subdomain root → serve dashboard, not landing page
    if _is_dashboard_host(r):
        is_operator = await _is_operator_request(r)
        return page("dashboard/index.html", r, is_operator=is_operator, show_market_price=await _dashboard_show_market_price(r), active_tab="home")

    return await page_async("index.html", r)


async def _auth_pages_hidden() -> bool:
    if get_settings().FORCE_AUTH_PAGES_VISIBLE:
        return False
    from app.core.database import AsyncSessionLocal
    from app.core.system_settings import hide_auth_pages_enabled
    async with AsyncSessionLocal() as db:
        return await hide_auth_pages_enabled(db)


# ── Auth pages ────────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login(r: Request):
    # Serve the login page. We intentionally DO NOT server-side-redirect
    # logged-in users to the dashboard here doing so creates a redirect loop
    # with the dashboard's Auth.requireAuth() whenever auth state is even
    # briefly inconsistent (e.g. account flagged inactive mid-session),
    # which an upstream rate limit then 503s. The `_guardLogin()` script at the
    # top of login.html performs the same check client-side using the exact
    # same /api/auth/me endpoint the dashboard uses so truth is consistent
    # across the whole app.
    if await _auth_pages_hidden():
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(404)
    from fastapi.responses import Response as _R
    resp = page("auth/login.html", r)
    if isinstance(resp, _R):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp

@app.get("/register", response_class=HTMLResponse)
async def register(r: Request):
    if await _auth_pages_hidden():
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(404)
    from fastapi.responses import Response as _R
    resp = page("auth/register.html", r)
    if isinstance(resp, _R):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.get("/report", response_class=HTMLResponse)
async def report_page(r: Request):
    return page("report.html", r)


# ── Payment Links public page ───────────────────────────────────────────────
@app.get("/pay/{slug}", response_class=HTMLResponse)
async def payment_link_page(r: Request, slug: str):
    """Public-facing page for a payment link no auth required."""
    from app.core.database import AsyncSessionLocal
    from app.models.models import PaymentLink as _PL, User as _U
    from sqlalchemy import select as _sel
    from datetime import datetime, timezone as _tz
    async with AsyncSessionLocal() as db:
        res = await db.execute(_sel(_PL).where(_PL.slug == slug))
        link = res.scalar_one_or_none()
        merchant_shows_price = False
        if link:
            mres = await db.execute(_sel(_U.show_market_price).where(_U.id == link.merchant_id))
            merchant_shows_price = bool(mres.scalar_one_or_none())
    if not link or not link.is_active:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/", status_code=302)
    now = datetime.now(_tz.utc)
    if link.expires_at and link.expires_at.replace(tzinfo=_tz.utc) < now:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/", status_code=302)
    return page(
        "paylink.html", r,
        link_slug          = link.slug,
        link_title         = link.title,
        link_description   = link.description or "",
        link_amount        = link.amount_firo,
        link_fixed         = link.fixed_amount,
        link_collect_email = link.collect_email,
        show_market_price  = merchant_shows_price,
    )


# ── Static content pages ──────────────────────────────────────────────────────
@app.get("/about", response_class=HTMLResponse)
async def about(r: Request):     return await page_async("about.html", r)

@app.get("/docs", response_class=HTMLResponse)
async def api_docs_page(r: Request): return await page_async("api-docs.html", r, api_version=API_VERSION)

@app.get("/changelog", response_class=HTMLResponse)
async def changelog_page(r: Request):
    from app.core.changelog import parse_changelog
    return page(
        "changelog.html", r,
        releases=parse_changelog(),
        api_version=API_VERSION,
        app_version=APP_VERSION,
    )


# ── Dashboard ─────────────────────────────────────────────────────────────────
# Supports two routing modes:
#   Subdomain mode:  dashboard.<your-domain>/{tab}   (clean URLs)
#   Path mode:       <your-domain>/dashboard/{tab}   (fallback / no subdomain)
#
# Both modes serve the same SPA. JS reads active_tab injected by server.

_DASHBOARD_TABS = [
    "home", "api", "payments", "balance",
    "security", "account", "analytics", "wallet",
]


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_root(r: Request):
    from fastapi.responses import RedirectResponse
    if settings.DASHBOARD_URL and _should_redirect_to_subdomain(r):
        return RedirectResponse(url=settings.DASHBOARD_URL.rstrip("/") + "/", status_code=301)
    is_operator = await _is_operator_request(r)
    return page("dashboard/index.html", r, is_operator=is_operator, show_market_price=await _dashboard_show_market_price(r), active_tab="home")


@app.get("/dashboard/{tab}", response_class=HTMLResponse)
async def dashboard_tab(r: Request, tab: str):
    from fastapi.responses import RedirectResponse
    safe_tab = tab if tab in _DASHBOARD_TABS else "home"
    if settings.DASHBOARD_URL and _should_redirect_to_subdomain(r):
        return RedirectResponse(
            url=settings.DASHBOARD_URL.rstrip("/") + f"/{safe_tab}",
            status_code=301,
        )
    is_operator = await _is_operator_request(r)
    return page("dashboard/index.html", r, is_operator=is_operator, show_market_price=await _dashboard_show_market_price(r), active_tab=safe_tab)



# ── Checkout / Invoice ────────────────────────────────────────────────────────
# Primary URL: checkout.<your-domain>/invoice/{id}
# Legacy /checkout/{id} → 301 redirect to /invoice/{id}

@app.get("/theme-preview", response_class=HTMLResponse)
async def theme_preview(r: Request):
    """Preview page shows how checkout looks with given theme params."""
    return page("theme_preview.html", r)

@app.get("/checkout/preview", response_class=HTMLResponse)
async def checkout_layout_preview(r: Request):
    """Preview checkout themes uses the real checkout page with fake demo data."""
    return page("checkout/index.html", r, payment_id="preview")

@app.get("/invoice/{pay_id}", response_class=HTMLResponse)
async def invoice(r: Request, pay_id: str):
    preview_layout = r.query_params.get("layout", "") if pay_id == "preview" else ""
    return page("checkout/index.html", r, payment_id=pay_id, preview_layout=preview_layout)

@app.get("/checkout/{pay_id}", response_class=HTMLResponse)
async def checkout_redirect(r: Request, pay_id: str):
    from fastapi.responses import RedirectResponse
    if _should_redirect_to_subdomain(r) and settings.CHECKOUT_URL:
        base = settings.CHECKOUT_URL.rstrip("/")
    else:
        base = _get_request_base_url(r)
    qs = str(r.url.query)
    target = f"{base}/invoice/{pay_id}" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=target, status_code=301)


@app.get("/admin")
async def panel_redirect(r: Request):
    from fastapi.responses import RedirectResponse
    is_operator = await _is_operator_request(r)
    if is_operator:
        target = settings.DASHBOARD_URL.rstrip("/") if settings.DASHBOARD_URL else "/dashboard"
        return RedirectResponse(url=target, status_code=302)
    target = settings.login_url if settings.DASHBOARD_URL else "/login"
    return RedirectResponse(url=target, status_code=302)





# ── Health check ──────────────────────────────────────────────────────────────
# Defined BEFORE the `/{tab}` catch-all below otherwise the catch-all
# shadows /health (matching `tab="health"`) and returns 404, which makes the
# panel UI's `loadStats()` see `health.node !== "online"` and render the
# "Node Offline" red dot even when the RPC is perfectly healthy.
@app.get("/health")
async def health():
    from app.services.firo_rpc import get_rpc
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import text

    node_ok = await get_rpc().ping()

    db_ok = False
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    overall = "ok" if (node_ok and db_ok) else "degraded"
    return {
        "status":  overall,
        "node":    "online" if node_ok else "offline",
        "db":      "ok" if db_ok else "error",
        "version": "1.0.0",
        "onion":   bool(settings.ONION_URL),
    }


# ── Dashboard subdomain catch-all MUST be last route ───────────────────────
# Handles dashboard.<your-domain>/{tab} clean URLs.
# Placed last so it never shadows /login, /register, /docs, /api/*, /health, etc.
@app.get("/{tab}", response_class=HTMLResponse)
async def dashboard_subdomain_tab(r: Request, tab: str):
    if _is_dashboard_host(r) and tab in _DASHBOARD_TABS:
        is_operator = await _is_operator_request(r)
        return page("dashboard/index.html", r, is_operator=is_operator, show_market_price=await _dashboard_show_market_price(r), active_tab=tab)
    from fastapi.responses import Response
    return Response(status_code=404)


