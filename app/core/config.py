from functools import lru_cache
from pydantic_settings import BaseSettings
from pydantic import model_validator
import secrets
import sys


class Settings(BaseSettings):
    APP_NAME:               str   = "FiroGate"

    # REQUIRED in production — must be set in .env
    # Generate: python -c "import secrets; print(secrets.token_hex(32))"
    # WARNING: if empty, a random key is generated per-restart which
    # invalidates all JWT tokens and HMAC checkout tokens on every restart.
    SECRET_KEY:             str   = ""

    BASE_URL:               str   = "http://localhost:8000"
    # Cookie domain for cross-subdomain sessions. When the app is served from
    # firogate.com + dashboard.firogate.com + checkout.firogate.com, set this
    # to '.firogate.com' (leading dot) so the access_token cookie is shared
    # across every subdomain. Leave empty for single-host / local dev.
    COOKIE_DOMAIN:          str   = ""

    # ─ Subdomain URLs (leave empty to fall back to BASE_URL paths) ─
    # Set these in .env to enable subdomain routing:
    #   DASHBOARD_URL=https://dashboard.firogate.com
    #   CHECKOUT_URL=https://checkout.firogate.com
    #   PLAN_URL=https://plan.firogate.com
    #   API_URL=https://api.firogate.com
    DASHBOARD_URL:          str   = ""
    CHECKOUT_URL:           str   = ""
    PLAN_URL:               str   = ""
    API_URL:                str   = ""
    # Auth action subdomain — hosts /auth/action (verify email + reset password
    # entry point). Example: AUTH_URL=https://dashboard.firogate.com so the
    # links in verification + password-reset emails point at your own
    # dashboard subdomain. Falls back to BASE_URL when left blank.
    AUTH_URL:               str   = ""

    ONION_URL:              str   = ""
    DEBUG:                  bool  = False
    DATABASE_URL:           str   = "sqlite+aiosqlite:///./data/gateway.db"
    FIELD_ENCRYPTION_KEY:   str   = ""


    FIELD_ENCRYPTION_KEY_OLD: str = ""


    FIRO_RPC_HOST:          str   = "127.0.0.1"
    FIRO_RPC_PORT:          int   = 18888
    FIRO_RPC_USER:          str   = ""
    FIRO_RPC_PASSWORD:      str   = ""


    FEE_RATE_PCT:           float = 0.5
    FEE_MIN_FIRO:           float = 0.05
    FEE_MAX_FIRO:           float = 1.0   # withdrawal fee never exceeds this

    # ─ Price feed (CoinMarketCap) ──
    CMC_API_KEY:            str   = ""    # set in .env: CMC_API_KEY=your_key_here
    PRICE_CACHE_SECONDS:    int   = 300   # 5 min cache for home/dashboard
    PRICE_FRESH_SECONDS:    int   = 170   # ~3 min for checkout/paylink (slightly under client interval)


    PLATFORM_FEE_PCT:       float = 0.5
    WITHDRAWAL_FEE_PCT:     float = 0.5
    MIN_WITHDRAWAL_FIRO:    float = 1.0


    ADMIN_USERNAME:         str   = ""   # legacy — leave blank to disable default-admin seeding
    ADMIN_PASSWORD:         str   = ""   # legacy — leave blank (use OPERATOR_EMAILS instead)
    ADMIN_EMAIL:            str   = ""   # legacy — kept for backwards compat only
    # OPERATOR_EMAILS — comma-separated list of email addresses with operator access.
    # list of Gmail (or any email) addresses. Any user who registers or
    # signs in with one of these emails is auto-promoted to role=admin.
    # No admin user is pre-created in the DB — the admin creates their
    # own account normally (email verify or Google sign-in) and the moment
    # they log in they get admin access. Keep this list short.
    # Example: OPERATOR_EMAILS=alice@gmail.com,bob@myco.com
    OPERATOR_EMAILS:           str   = ""
    ADMIN_ROUTE_PATH:       str   = ""


    PANEL_ALLOWED_IPS:      str   = ""


    PANEL_ACCESS_KEY:       str   = ""


    PANEL_REQUIRE_2FA:      bool  = True


    ADMIN_RATE_LIMIT:       int   = 20


    PANEL_ONION_ONLY: bool = True


    MAX_DAILY_WITHDRAWAL_FIRO: float = 100.0
    MAX_WITHDRAWALS_PER_DAY:   int   = 3
    MIN_BALANCE_HOLD_HOURS:    int   = 24
    WITHDRAWAL_COOLDOWN_MIN:   int   = 10


    AUTO_TIER_MAX_FIRO:        float = 50.0
    SOFT_TIER_MAX_FIRO:        float = 200.0


    WITHDRAWAL_DELAY_SECONDS:  int   = 45


    REQUIRED_CONFIRMATIONS: int   = 2
    PAYMENT_TIMEOUT_MINUTES: int  = 20


    WALLET_PASSPHRASE:      str   = ""

    WALLET_UNLOCK_SECONDS:  int   = 60


    SPARK_ENABLED:          bool  = False


    SPARK_SOURCE_ADDRESS:   str   = ""


    SPARK_MIN_CONFIRMATIONS: int  = 2


    REDIS_URL:                  str   = ""
    REDIS_PASSWORD:             str   = ""
    REDIS_SSL:                  bool  = False
    REDIS_MAX_CONNECTIONS:      int   = 20
    REDIS_KEY_PREFIX:           str   = "firogate:rl:"
    REDIS_KEY_TTL_MULTIPLIER:   int   = 3


    TOR_ENABLED:            bool  = False
    TOR_SOCKS_PORT:         int   = 9050

    TOR_ALL_TRAFFIC:        bool  = False

    TRUST_PROXY_HEADERS:    bool  = False


    PLATFORM_SPARK_ADDRESS:        str   = ""


    SHOW_TESTNET_STORE:            bool  = True

    PLATFORM_AUTO_WITHDRAW:        bool  = False


    PLATFORM_AUTO_WITHDRAW_THRESHOLD: float = 10.0


    PLATFORM_WITHDRAWAL_KEY:       str   = ""

    # ─ Firebase (Server) ─
    FIREBASE_PROJECT_ID:           str   = ""
    FIREBASE_CLIENT_EMAIL:         str   = ""
    FIREBASE_PRIVATE_KEY:          str   = ""

    # ─ Firebase (Client / public — injected into templates) ─
    FIREBASE_API_KEY:              str   = ""
    FIREBASE_AUTH_DOMAIN:          str   = ""
    FIREBASE_APP_ID:               str   = ""

    # ─ Google Auth ─
    GOOGLE_CLIENT_ID:              str   = ""

    # ─ Cloudflare Turnstile ─
    TURNSTILE_SITE_KEY:            str   = ""
    TURNSTILE_SECRET_KEY:          str   = ""

    # ─ Email: SendGrid HTTP API (SMTP removed) ─
    SENDGRID_API_KEY:              str   = ""
    FROM_EMAIL:                    str   = ""
    FROM_NAME:                     str   = "FiroGate"

    # ─ Auth token expiry / cooldown ─
    EMAIL_VERIFICATION_EXPIRE_SECONDS: int = 14400
    PASSWORD_RESET_EXPIRE_SECONDS:     int = 7200
    RESET_COOLDOWN_SECONDS:            int = 60

    class Config:
        env_file = ".env"
        extra    = "ignore"

    @model_validator(mode="after")
    def _validate_critical_secrets(self) -> "Settings":
        """
        Validate critical secrets at startup.
        SECRET_KEY and FIELD_ENCRYPTION_KEY must be set in .env for production.
        In development (DEBUG=True or localhost), we auto-generate SECRET_KEY
        with a loud warning. In production this is a hard failure.
        """
        is_local = (
            self.DEBUG or
            "localhost" in self.BASE_URL or
            "127.0.0.1" in self.BASE_URL
        )

        # SECRET_KEY
        if not self.SECRET_KEY:
            if is_local:
                # Auto-generate for local dev with warning
                object.__setattr__(self, "SECRET_KEY", secrets.token_hex(32))
                print(
                    "\n⚠️  WARNING: SECRET_KEY not set in .env — using a random key.\n"
                    "   All sessions and tokens will be invalidated on every restart.\n"
                    "   Set SECRET_KEY in .env for stable sessions.\n"
                    "   Generate: python -c \"import secrets; print(secrets.token_hex(32))\"\n",
                    file=sys.stderr
                )
            else:
                raise ValueError(
                    "\n\n❌  FATAL: SECRET_KEY must be set in .env\n"
                    "   Generate: python -c \"import secrets; print(secrets.token_hex(32))\"\n"
                    "   Then add: SECRET_KEY=<generated_value> to your .env file\n"
                )

        # FIELD_ENCRYPTION_KEY — warn but don't hard-fail (some deploys may not use encryption)
        if not self.FIELD_ENCRYPTION_KEY:
            print(
                "\n⚠️  WARNING: FIELD_ENCRYPTION_KEY not set in .env\n"
                "   Webhook secrets and encrypted fields will use degraded security.\n"
                "   Generate: python -c \"import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\"\n",
                file=sys.stderr
            )

        return self


    @property
    def tor_socks_url(self) -> str:
        return f"socks5://127.0.0.1:{self.TOR_SOCKS_PORT}"

    @property
    def rpc_url(self) -> str:
        return f"http://{self.FIRO_RPC_HOST}:{self.FIRO_RPC_PORT}/"

    @property
    def is_testnet(self) -> bool:
        """
        Automatic detection — no manual toggle needed.
        Testnet:  RPC port 18888 (Firo testnet default)
        Mainnet:  RPC port 8888  (Firo mainnet default)
        """
        return self.FIRO_RPC_PORT == 18888

    @property
    def network_name(self) -> str:
        """Short network identifier used in API responses and logs."""
        return "testnet" if self.is_testnet else "mainnet"

    @property
    def network_label(self) -> str:
        """Human-readable label for UI display."""
        return "Testnet" if self.is_testnet else "Mainnet"

    @property
    def network_warning(self) -> str | None:
        """Warning message shown globally when running on testnet."""
        if self.is_testnet:
            return "Testnet Mode — No real funds. This is a testing environment only."
        return None

    @property
    def explorer_base_url(self) -> str:
        """Firo blockchain explorer for the current network."""
        return "https://testexplorer.firo.org" if self.is_testnet else "https://explorer.firo.org"

    def tx_explorer_url(self, txid: str) -> str:
        """Full explorer URL for a transaction."""
        return f"{self.explorer_base_url}/tx/{txid}" if txid else ""


    @property
    def effective_base_url(self) -> str:
        if self.ONION_URL and self.TOR_ENABLED and self.TOR_ALL_TRAFFIC:
            return self.ONION_URL.rstrip("/")
        return self.BASE_URL.rstrip("/")

    def get_checkout_base_url(self, request_origin: str | None = None) -> str:
        if request_origin and ".onion" in request_origin and self.ONION_URL:
            return self.ONION_URL.rstrip("/")
        if self.CHECKOUT_URL:
            return self.CHECKOUT_URL.rstrip("/")
        return self.BASE_URL.rstrip("/")

    @property
    def dashboard_base_url(self) -> str:
        """URL where the dashboard lives (subdomain or BASE_URL/dashboard fallback)."""
        if self.DASHBOARD_URL:
            return self.DASHBOARD_URL.rstrip("/")
        return self.BASE_URL.rstrip("/") + "/dashboard"

    @property
    def login_url(self) -> str:
        """
        Canonical login page URL. Login is served on the dashboard subdomain
        (DASHBOARD_URL) when configured — this keeps every Firebase+Turnstile
        flow on ONE origin where the Turnstile widget is registered and the
        cookie scope matches. Falls back to BASE_URL/login for single-host.
        """
        if self.DASHBOARD_URL:
            return self.DASHBOARD_URL.rstrip("/") + "/login"
        return self.BASE_URL.rstrip("/") + "/login"

    @property
    def admin_email_set(self) -> set[str]:
        """
        Parsed set of lowercase operator emails from OPERATOR_EMAILS. Used by
        login/register flows to auto-promote matching users to role=admin
        and by `require_admin` to accept a fresh admin before the DB row
        has been updated.
        """
        raw = (self.OPERATOR_EMAILS or "").strip()
        if not raw:
            # Fall back to the legacy single ADMIN_EMAIL so existing deploys
            # don't break the minute they pull this change.
            legacy = (self.ADMIN_EMAIL or "").strip().lower()
            return {legacy} if legacy else set()
        return {e.strip().lower() for e in raw.split(",") if e.strip()}

    def is_admin_email(self, email: str) -> bool:
        if not email:
            return False
        return email.strip().lower() in self.admin_email_set

    @property
    def allowed_origins(self) -> list[str]:
        origins = [self.BASE_URL.rstrip("/")]
        if self.ONION_URL:
            origins.append(self.ONION_URL.rstrip("/"))
        for url in [self.DASHBOARD_URL, self.CHECKOUT_URL, self.PLAN_URL, self.API_URL]:
            if url:
                origins.append(url.rstrip("/"))
        return list(dict.fromkeys(origins))  # deduplicate, preserve order

    def should_use_tor_for_url(self, url: str) -> bool:
        if not self.TOR_ENABLED:
            return False

        if any(local in url for local in ("127.0.0.1", "::1", "localhost")):
            return False
        if ".onion" in url:
            return True
        return self.TOR_ALL_TRAFFIC


@lru_cache
def get_settings() -> Settings:
    return Settings()
