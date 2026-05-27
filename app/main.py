from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from app.core.config import get_settings
from app.core.database import create_tables
from app.core import i18n as fg_i18n
from app.api import auth, auth_fb, payments, withdrawals, users, analytics
from app.api import i18n as i18n_api
from app.api import payment_links
from app.api import events as events_api
from app.api import theme as theme_api
from app.api import api_keys as api_keys_api

# ── Enterprise modules (Community Edition runs without these) ─────────────────
try:
    from app.enterprise import register as _register_enterprise
    from app.enterprise.core.shield import SecurityMiddleware
    from app.enterprise.core.admin_route import get_or_create_admin_route
    ENTERPRISE = True
except ImportError:
    ENTERPRISE = False
    _register_enterprise = None
    get_or_create_admin_route = lambda: "/api/admin"

    # Community fallback: basic rate-limiting middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    class SecurityMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            return await call_next(request)

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


ADMIN_ROUTE = get_or_create_admin_route()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {settings.APP_NAME}")
    logger.info(f"BASE_URL : {settings.BASE_URL}")

    # Cross-subdomain cookie diagnostics — helps diagnose "logged in on firogate.com
    # but not on dashboard.firogate.com" type issues at a glance.
    try:
        from app.core.security import _derive_cookie_domain
        eff_domain = (settings.COOKIE_DOMAIN or "").strip() or _derive_cookie_domain(settings.BASE_URL)
        logger.info(f"COOKIE_DOMAIN (effective): {eff_domain or '(none — host-only cookies)'}")
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
        logger.info(f"Onion panel: {settings.ONION_URL}{ADMIN_ROUTE}")
    if settings.TOR_ENABLED:
        logger.info(f"Tor SOCKS5 enabled at socks5://127.0.0.1:{settings.TOR_SOCKS_PORT}")
        if settings.TOR_ALL_TRAFFIC:
            logger.info("   All outbound traffic routed through Tor")
        else:
            logger.info("   Only .onion webhooks + RPC routed through Tor")
    else:
        logger.warning("Tor disabled - .onion webhook calls will fail if not on same host")
    logger.info(f"Panel path: {ADMIN_ROUTE}")
    logger.info(f"   Reachable at: {settings.BASE_URL}{ADMIN_ROUTE}")


    if not settings.PANEL_ACCESS_KEY:
        logger.warning("PANEL_ACCESS_KEY not set - access key layer disabled")
    else:
        logger.info("Admin access key: configured")

    if not settings.PANEL_ALLOWED_IPS:
        logger.warning("PANEL_ALLOWED_IPS not set - no IP restriction")
    else:
        logger.info(f"Panel IP whitelist active")

    if not settings.PANEL_REQUIRE_2FA:
        logger.warning("PANEL_REQUIRE_2FA=false - 2FA not enforced")
    else:
        logger.info("Admin 2FA: required")

    if settings.ONION_URL and settings.PANEL_ONION_ONLY:
        logger.info("Admin onion mode: localhost-only enforced")
    await create_tables()
    await _seed()


    from app.core.security import validate_encryption_key_on_startup
    validate_encryption_key_on_startup()

    from app.core.rate_limit import log_rate_limiter_info
    log_rate_limiter_info()

    from app.services.payment_monitor import check_pending_payments
    scheduler.add_job(check_pending_payments, "interval", seconds=12, id="payment_monitor")
    if ENTERPRISE:
        try:
            from app.services.payment_monitor import check_plan_orders
            scheduler.add_job(check_plan_orders, "interval", seconds=30, id="plan_monitor")
        except ImportError:
            pass


    from app.services.webhook import retry_failed_webhooks
    scheduler.add_job(retry_failed_webhooks, "interval", seconds=120, id="webhook_retry")

    from app.services.withdrawal_service import process_queued_withdrawals
    scheduler.add_job(process_queued_withdrawals, "interval", seconds=15, id="withdrawal_worker")

    # Enterprise-only scheduler jobs
    if ENTERPRISE:
        try:
            from app.enterprise.services.shield_engine import run_periodic_time_check
            scheduler.add_job(run_periodic_time_check, "interval", seconds=300, id="shield_time_check")
        except ImportError:
            pass
        try:
            from app.enterprise.services.admin_withdrawal_service import check_auto_withdrawal
            scheduler.add_job(check_auto_withdrawal, "interval", seconds=300, id="admin_auto_withdraw")
        except ImportError:
            pass
        try:
            from app.enterprise.services.routing_engine import advance_hops_job, cleanup_expired_job
            scheduler.add_job(advance_hops_job,    "interval", seconds=60,  id="routing_advance")
            scheduler.add_job(cleanup_expired_job, "cron",     hour=4, minute=0, id="routing_cleanup")
        except ImportError:
            pass


    from app.services.db_cleanup import run_db_cleanup

    scheduler.add_job(run_db_cleanup, "cron", hour=3, minute=0, id="db_cleanup")
    scheduler.start()
    logger.info("Schedulers started")
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
        # ── Admin via OPERATOR_EMAILS (preferred) ─────────────────────────
        # Any existing user whose email is listed in OPERATOR_EMAILS is
        # promoted to admin on every startup. Brand new admins don't
        # exist yet — they'll be promoted the moment they register / log
        # in with their listed Gmail (see register_with_email / login_
        # with_email / google sign-in paths).
        operator_emails = settings.admin_email_set
        promoted = 0
        if operator_emails:
            res = await db.execute(
                select(User).where(User.email.in_(list(operator_emails)))
            )
            for u in res.scalars().all():
                if u.role != UserRole.admin:
                    u.role = UserRole.admin
                    db.add(u)
                    promoted += 1
            if promoted:
                logger.info(f"[seed] Promoted {promoted} operator account(s)")

        # ── Legacy username/password admin seed ────────────────────────
        # Runs ONLY when ADMIN_USERNAME *and* ADMIN_PASSWORD are both set.
        # Skipped entirely when OPERATOR_EMAILS is the only admin source.
        if settings.ADMIN_USERNAME and settings.ADMIN_PASSWORD:
            res = await db.execute(
                select(User).where(User.username == settings.ADMIN_USERNAME.lower())
            )
            if not res.scalar_one_or_none():
                db.add(User(
                    username=settings.ADMIN_USERNAME.lower(),
                    email=(settings.ADMIN_EMAIL or f"{settings.ADMIN_USERNAME}@example.com"),
                    hashed_password=hash_password(settings.ADMIN_PASSWORD),
                    role=UserRole.admin,
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


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=r"https?://.*\.onion(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


if settings.TRUST_PROXY_HEADERS:
    from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware

    try:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
        logger.info("ProxyHeadersMiddleware enabled (TRUST_PROXY_HEADERS=true)")
    except ImportError:
        pass

app.add_middleware(SecurityMiddleware)

# Privacy Middleware - Must be added before SecurityMiddleware processes the request
from app.core.privacy_middleware import PrivacyMiddleware
app.add_middleware(PrivacyMiddleware)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def admin_page_guard(request: Request, call_next):
    path = request.url.path
    admin_paths = [ADMIN_ROUTE, ADMIN_ROUTE.rstrip("/")]

    if path in admin_paths and ENTERPRISE:
        try:
            from app.enterprise.core.admin_guard import (
                _get_real_ip, _normalize_ip, _parse_allowed_ips, _ip_allowed,
                _check_admin_rate, _log_deny,
            )
            from fastapi.responses import Response as FR

            ip   = _get_real_ip(request)
            host = (
                request.headers.get("X-Forwarded-Host", "")
                or request.headers.get("Host", "")
            ).split(",")[0].strip().lower()
            is_onion_request = host.endswith(".onion")

            if settings.ONION_URL and settings.PANEL_ONION_ONLY and is_onion_request:
                normalized_ip = _normalize_ip(ip)
                if normalized_ip != "127.0.0.1":
                    _log_deny(ip, path, "onion-mode page: non-localhost")
                    return FR(status_code=403, content="Forbidden")

            raw = settings.PANEL_ALLOWED_IPS.strip()
            if raw:
                allowed = _parse_allowed_ips(raw)
                if not _ip_allowed(ip, allowed):
                    _log_deny(ip, path, f"page IP not in whitelist: {ip}")
                    return FR(status_code=403, content="Forbidden")

            if not _check_admin_rate(ip):
                _log_deny(ip, path, "page rate limit")
                from fastapi.responses import Response as FR2
                return FR2(status_code=429, content="Too many requests")
        except ImportError:
            pass

    return await call_next(request)


app.include_router(auth.router)
app.include_router(auth_fb.router)
app.include_router(payments.public_router)
app.include_router(payments.router)
app.include_router(withdrawals.router)
app.include_router(users.router)
app.include_router(analytics.router)
app.include_router(i18n_api.router)
app.include_router(payment_links.router)
app.include_router(events_api.router)
app.include_router(theme_api.router)
app.include_router(api_keys_api.router)

@app.get("/api/health")
async def health():
    return {"status": "ok", "edition": "community"}

# Enterprise routers (plans, admin, reports)
if ENTERPRISE and _register_enterprise:
    _register_enterprise(app)


# ── Price feed endpoint ────────────────────────────────────────────────────────
@app.get("/api/price")
async def get_price(fresh: bool = False):
    """
    Returns current FIRO/USDT price from MEXC.
    ?fresh=true  → bypass cache (checkout, withdrawal)
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

    Detection is done in two ways — both independent of whether ONION_URL
    is configured in .env:
      1. The host header contains '.onion'  (primary — always reliable)
      2. Nginx set X-Onion-Request: true    (secondary fallback header)

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
        # ONION_URL not configured — reconstruct from the host header so the
        # operator still gets correct behaviour without extra .env config.
        host   = _get_host(r)
        scheme = r.headers.get("x-forwarded-proto", "http")
        return f"{scheme}://{host}"
    return settings.BASE_URL.rstrip("/")


def _should_redirect_to_subdomain(r: Request) -> bool:
    """
    Return True only when the request arrived on the configured main domain
    and we should redirect to a subdomain (dashboard.firogate.com etc.).

    Returns False (serve directly) when:
      - Tor/onion:  request came via .onion hidden service
      - Local/dev:  request came from localhost / 127.0.0.1 (admin setup, dev)
    """
    # Check the host header directly — never rely on ONION_URL being set.
    if _is_onion_request(r):
        return False
    host = _get_host(r).lower().split(":")[0]   # strip port
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False
    return True



def _is_dashboard_host(r: Request) -> bool:
    """True when request comes from dashboard.firogate.com (or equivalent)."""
    if not settings.DASHBOARD_URL:
        return False
    host = _get_host(r)
    from urllib.parse import urlparse
    dashboard_host = urlparse(settings.DASHBOARD_URL).netloc
    return host == dashboard_host


def _is_checkout_host(r: Request) -> bool:
    if not settings.CHECKOUT_URL:
        return False
    host = _get_host(r)
    from urllib.parse import urlparse
    return host == urlparse(settings.CHECKOUT_URL).netloc


def _is_plan_host(r: Request) -> bool:
    if not settings.PLAN_URL:
        return False
    host = _get_host(r)
    from urllib.parse import urlparse
    return host == urlparse(settings.PLAN_URL).netloc


def page(name: str, r: Request, **ctx):
    base_url    = _get_request_base_url(r)
    _via_onion  = _is_onion_request(r)          # host-header check — no ONION_URL dependency
    _host_plain = _get_host(r).lower().split(":")[0]
    _is_local   = _host_plain in ("localhost", "127.0.0.1", "::1", "0.0.0.0")

    if _via_onion:
        # Tor/onion session — keep ALL navigation within the .onion domain.
        # Never redirect to clearnet subdomains (dashboard.firogate.com etc.)
        # Use ONION_URL if configured; fall back to the reconstructed base_url.
        _onion = (settings.ONION_URL.rstrip("/") if settings.ONION_URL else base_url)
        dashboard_url = _onion + "/dashboard"
        checkout_url  = _onion
        plan_url      = _onion + "/dashboard/plan"
        api_url       = _onion
        login_url     = _onion + "/login"
    elif _is_local:
        # Local / direct access (admin dev/setup, LAN) — use path-relative URLs
        # so "← Back to Dashboard" never redirects off-server to a clearnet subdomain.
        dashboard_url = "/dashboard"
        checkout_url  = ""
        plan_url      = "/dashboard/plan"
        api_url       = ""
        login_url     = "/login"
    else:
        dashboard_url = settings.DASHBOARD_URL or (settings.BASE_URL.rstrip("/") + "/dashboard")
        checkout_url  = settings.CHECKOUT_URL  or settings.BASE_URL.rstrip("/")
        plan_url      = settings.PLAN_URL      or (settings.BASE_URL.rstrip("/") + "/dashboard/plan")
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
        "plan_url":      plan_url,
        "api_url":       api_url,
        "login_url":     login_url,
        "show_testnet_store": settings.SHOW_TESTNET_STORE,
        # ── public Firebase / Turnstile config (safe to expose) ──
        "firebase_config": {
            "apiKey":     settings.FIREBASE_API_KEY,
            "authDomain": settings.FIREBASE_AUTH_DOMAIN,
            "projectId":  settings.FIREBASE_PROJECT_ID,
            "appId":      settings.FIREBASE_APP_ID,
        },
        "firebase_enabled":  bool(settings.FIREBASE_API_KEY and settings.FIREBASE_PROJECT_ID),
        "turnstile_site_key": settings.TURNSTILE_SITE_KEY,
        "google_client_id":  settings.GOOGLE_CLIENT_ID,
        **ctx,
    })
    # Critical: prevent the browser from caching authenticated HTML.
    # A cached stale dashboard/login page was the root cause of the
    # dashboard ↔ /login navigation loop — browsers were re-serving an
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
def _check_is_logged_in(r: Request) -> bool:
    """Server-side auth check so templates can render the correct nav
    (Dashboard vs Sign-In) on the very first HTML — no flash, no Chrome
    speculative prefetch of /login."""
    try:
        from app.core.security import verify_access_token
        tok = r.cookies.get("access_token") or ""
        if not tok:
            return False
        return bool(verify_access_token(tok))
    except Exception:
        return False


async def _is_admin_request(r: Request) -> bool:
    """Server-side admin check used to gate the hidden admin URL out of
    HTML rendered for merchant accounts. Reads the access_token cookie,
    looks up the user, returns True only when role == admin."""
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
            if u.role == UserRole.admin:
                return True
            # Auto-promote on the fly when the email matches OPERATOR_EMAILS.
            if settings.is_admin_email(u.email):
                return True
        return False
    except Exception:
        return False


def _admin_route_for(is_admin: bool) -> str:
    """Return the real hidden admin path when the requester is admin,
    otherwise the placeholder /admin which redirects to /login if the
    visitor is not authorised. This keeps the secret URL out of HTML
    served to merchant accounts."""
    return ADMIN_ROUTE if is_admin else "/admin"


@app.get("/", response_class=HTMLResponse)
async def home(r: Request):
    # If request is from dashboard subdomain root → serve dashboard, not landing page
    if _is_dashboard_host(r):
        is_admin = await _is_admin_request(r)
        return page("dashboard/index.html", r, admin_route=_admin_route_for(is_admin), active_tab="home")

    import os
    banner_dir  = os.path.join("static", "images", "banner")
    allowed_ext = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    banner_images: list[str] = []
    if os.path.isdir(banner_dir):
        for fname in sorted(os.listdir(banner_dir)):
            if os.path.splitext(fname)[1].lower() in allowed_ext:
                # Append the file's mtime as a cache-busting query so the
                # browser always fetches the new image after you swap it
                # in /static/images/banner/ — no manual cache clear needed.
                fpath = os.path.join(banner_dir, fname)
                try:
                    mtime = int(os.path.getmtime(fpath))
                except OSError:
                    mtime = 0
                banner_images.append(f"/static/images/banner/{fname}?v={mtime}")
    if not banner_images:
        banner_images = ["", "", "", ""]
    elif len(banner_images) < 4:
        while len(banner_images) < 4:
            banner_images += banner_images
        banner_images = banner_images[:4]

    plan_prices = []  # Pricing section removed in Community Edition

    return page(
        "index.html", r,
        banner_images=banner_images,
        plan_prices=plan_prices,
        is_logged_in=_check_is_logged_in(r),
    )


# ── Auth pages ────────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login(r: Request):
    # Serve the login page. We intentionally DO NOT server-side-redirect
    # logged-in users to the dashboard here — doing so creates a redirect loop
    # with the dashboard's Auth.requireAuth() whenever auth state is even
    # briefly inconsistent (e.g. account flagged inactive mid-session),
    # which nginx's burst limit then 503s. The `_guardLogin()` script at the
    # top of login.html performs the same check client-side using the exact
    # same /api/auth/me endpoint the dashboard uses — so truth is consistent
    # across the whole app.
    from fastapi.responses import Response as _R
    resp = page("auth/login.html", r)
    if isinstance(resp, _R):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp

@app.get("/register", response_class=HTMLResponse)
async def register(r: Request):
    from fastapi.responses import Response as _R
    resp = page("auth/register.html", r)
    if isinstance(resp, _R):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.get("/auth/verify-email", response_class=HTMLResponse)
async def auth_verify_email_page(r: Request):
    return page("auth/verify-email.html", r)


@app.get("/auth/reset-password", response_class=HTMLResponse)
async def auth_reset_password_page(r: Request):
    return page("auth/reset-password.html", r)


@app.get("/auth/action", response_class=HTMLResponse)
async def auth_action_page(r: Request, mode: str = "", oobCode: str = ""):
    """
    Unified Firebase-style email-action handler. The email links we send for
    verification and password reset point at
        {AUTH_URL}/auth/action?mode=<verifyEmail|resetPassword>&oobCode=<token>
    This route dispatches to the correct existing template and forwards the
    `oobCode` under the `token` query param our templates already expect.
    Unknown modes fall back to a generic error page.
    """
    mode  = (mode or "").strip()
    token = (oobCode or r.query_params.get("token", "") or "").strip()
    if mode == "verifyEmail":
        # Pre-fill the legacy `token` query param on the verify-email template.
        return page("auth/verify-email.html", r, action_token=token)
    if mode == "resetPassword":
        return page("auth/reset-password.html", r, action_token=token)
    # Fallback: render reset-password template with a clear error so the
    # user sees something polished even when the link is malformed.
    return page("auth/reset-password.html", r, action_token="", action_error="This link is invalid or malformed.")


@app.get("/report", response_class=HTMLResponse)
async def report_page(r: Request):
    return page("report.html", r)


# ── Payment Links — public page ───────────────────────────────────────────────
@app.get("/pay/{slug}", response_class=HTMLResponse)
async def payment_link_page(r: Request, slug: str):
    """Public-facing page for a payment link — no auth required."""
    from app.core.database import AsyncSessionLocal
    from app.models.models import PaymentLink as _PL
    from sqlalchemy import select as _sel
    from datetime import datetime, timezone as _tz
    async with AsyncSessionLocal() as db:
        res = await db.execute(_sel(_PL).where(_PL.slug == slug))
        link = res.scalar_one_or_none()
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
    )


# ── Static content pages ──────────────────────────────────────────────────────
@app.get("/about", response_class=HTMLResponse)
async def about(r: Request):     return page("about.html", r)

@app.get("/docs", response_class=HTMLResponse)
async def api_docs_page(r: Request): return page("api-docs.html", r)

@app.get("/legal", response_class=HTMLResponse)
async def legal(r: Request):    return page("legal.html", r)


# ── Dashboard ─────────────────────────────────────────────────────────────────
# Supports two routing modes:
#   Subdomain mode:  dashboard.firogate.com/{tab}   (clean URLs)
#   Path mode:       firogate.com/dashboard/{tab}   (fallback / no subdomain)
#
# Both modes serve the same SPA. JS reads active_tab injected by server.

_DASHBOARD_TABS = [
    "home", "api", "payments", "balance",
    "withdraw", "plan", "security", "analytics",
]


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_root(r: Request):
    from fastapi.responses import RedirectResponse
    if settings.DASHBOARD_URL and _should_redirect_to_subdomain(r):
        return RedirectResponse(url=settings.DASHBOARD_URL.rstrip("/") + "/", status_code=301)
    is_admin = await _is_admin_request(r)
    return page("dashboard/index.html", r, admin_route=_admin_route_for(is_admin), active_tab="home")


@app.get("/dashboard/{tab}", response_class=HTMLResponse)
async def dashboard_tab(r: Request, tab: str):
    from fastapi.responses import RedirectResponse
    safe_tab = tab if tab in _DASHBOARD_TABS else "home"
    if settings.DASHBOARD_URL and _should_redirect_to_subdomain(r):
        return RedirectResponse(
            url=settings.DASHBOARD_URL.rstrip("/") + f"/{safe_tab}",
            status_code=301,
        )
    is_admin = await _is_admin_request(r)
    return page("dashboard/index.html", r, admin_route=_admin_route_for(is_admin), active_tab=safe_tab)



# ── Checkout / Invoice ────────────────────────────────────────────────────────
# Primary URL: checkout.firogate.com/invoice/{id}
# Legacy /checkout/{id} → 301 redirect to /invoice/{id}

@app.get("/theme-preview", response_class=HTMLResponse)
async def theme_preview(r: Request):
    """Preview page — shows how checkout looks with given theme params."""
    return page("theme_preview.html", r)

@app.get("/invoice/{pay_id}", response_class=HTMLResponse)
async def invoice(r: Request, pay_id: str):
    return page("checkout/index.html", r, payment_id=pay_id)

@app.get("/checkout/{pay_id}", response_class=HTMLResponse)
async def checkout_redirect(r: Request, pay_id: str):
    from fastapi.responses import RedirectResponse
    if _should_redirect_to_subdomain(r) and settings.CHECKOUT_URL:
        base = settings.CHECKOUT_URL.rstrip("/")
    else:
        base = _get_request_base_url(r)
    target = f"{base}/invoice/{pay_id}"
    return RedirectResponse(url=target, status_code=301)


# ── Plan page ─────────────────────────────────────────────────────────────────
@app.get("/plan", response_class=HTMLResponse)
async def plan_page(r: Request):
    if _is_plan_host(r):
        is_admin = await _is_admin_request(r)
        return page("dashboard/index.html", r, admin_route=_admin_route_for(is_admin), active_tab="plan")
    if not _should_redirect_to_subdomain(r):
        is_admin = await _is_admin_request(r)
        return page("dashboard/index.html", r, admin_route=_admin_route_for(is_admin), active_tab="plan")
    from fastapi.responses import RedirectResponse
    target = (settings.PLAN_URL or settings.dashboard_base_url + "/plan").rstrip("/")
    return RedirectResponse(url=target, status_code=302)

@app.get("/plan/{plan_name}", response_class=HTMLResponse)
async def plan_detail(r: Request, plan_name: str):
    is_admin = await _is_admin_request(r)
    return page("dashboard/index.html", r, admin_route=_admin_route_for(is_admin), active_tab="plan")


@app.get(ADMIN_ROUTE, response_class=HTMLResponse)
async def admin_page(r: Request):
    if not ENTERPRISE:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=302)
    is_admin = await _is_admin_request(r)
    return page("admin/index.html", r, admin_route=_admin_route_for(is_admin))

@app.get(ADMIN_ROUTE.rstrip("/"), response_class=HTMLResponse)
async def admin_page_no_slash(r: Request):
    if not ENTERPRISE:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=302)
    is_admin = await _is_admin_request(r)
    return page("admin/index.html", r, admin_route=_admin_route_for(is_admin))


@app.get("/admin")
async def admin_redirect(r: Request):
    """Public placeholder for the admin URL.

    Non-admin visitors are bounced to /login instead of being told the
    real ADMIN_ROUTE — the secret path stays out of HTML, redirects, and
    referrer headers. Authenticated admins are forwarded to the real path.
    """
    from fastapi.responses import RedirectResponse
    is_admin = await _is_admin_request(r)
    if is_admin:
        return RedirectResponse(url=ADMIN_ROUTE, status_code=302)
    target = settings.login_url if settings.DASHBOARD_URL else "/login"
    return RedirectResponse(url=target, status_code=302)





# ── Health check ──────────────────────────────────────────────────────────────
# Defined BEFORE the `/{tab}` catch-all below — otherwise the catch-all
# shadows /health (matching `tab="health"`) and returns 404, which makes the
# admin UI's `loadStats()` see `health.node !== "online"` and render the
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


# ── Dashboard subdomain catch-all — MUST be last route ───────────────────────
# Handles dashboard.firogate.com/{tab} clean URLs.
# Placed last so it never shadows /login, /register, /docs, /api/*, /health, etc.
@app.get("/{tab}", response_class=HTMLResponse)
async def dashboard_subdomain_tab(r: Request, tab: str):
    if _is_dashboard_host(r) and tab in _DASHBOARD_TABS:
        is_admin = await _is_admin_request(r)
        return page("dashboard/index.html", r, admin_route=_admin_route_for(is_admin), active_tab=tab)
    from fastapi.responses import Response
    return Response(status_code=404)


