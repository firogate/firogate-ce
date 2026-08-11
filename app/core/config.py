from functools import lru_cache
from pydantic_settings import BaseSettings
from pydantic import model_validator
import secrets
import sys


class Settings(BaseSettings):
    APP_NAME:               str   = "FiroGate"
    SECRET_KEY:             str   = ""

    BASE_URL:               str   = "http://localhost:8000"
    COOKIE_DOMAIN:          str   = ""
    DASHBOARD_URL:          str   = ""
    CHECKOUT_URL:           str   = ""
    API_URL:                str   = ""
    AUTH_URL:               str   = ""

    ONION_URL:              str   = ""
    DEBUG:                  bool  = False
    DATABASE_URL:           str   = "sqlite+aiosqlite:///./data/gateway.db"
    DB_POOL_SIZE:           int   = 20
    DB_MAX_OVERFLOW:        int   = 10
    FIELD_ENCRYPTION_KEY:   str   = ""


    FIELD_ENCRYPTION_KEY_OLD: str = ""
    FIELD_ENCRYPTION_SALT:    str = ""


    FIRO_RPC_HOST:          str   = "127.0.0.1"
    FIRO_RPC_PORT:          int   = 18888
    FIRO_RPC_USER:          str   = ""
    FIRO_RPC_PASSWORD:      str   = ""

    CMC_API_KEY:            str   = ""
    PRICE_PROXY_URL:        str   = "http://127.0.0.1:8899/cmc/v1/cryptocurrency/quotes/latest"
    PRICE_CACHE_SECONDS:    int   = 60    # dashboard/home cache TTL add more if you want
    PRICE_FRESH_SECONDS:    int   = 30    # checkout/paylink cache TTL (both stale-while-revalidate)

    TIER_ENABLED:           bool  = False


    ADMIN_USERNAME:         str   = ""   # legacy leave blank to disable default-operator seeding
    ADMIN_PASSWORD:         str   = ""   # legacy leave blank (use OPERATOR_EMAILS instead)
    ADMIN_EMAIL:            str   = ""   # legacy kept for backwards compat only

    OPERATOR_EMAILS:        str   = ""
    OPERATOR_USERNAMES:     str   = ""

    PANEL_REQUIRE_2FA:      bool  = True
    REQUIRED_CONFIRMATIONS: int   = 1
    PAYMENT_TIMEOUT_MINUTES: int  = 20
    FORCE_AUTH_PAGES_VISIBLE: bool = False
    DEFAULT_PAYMENT_TOLERANCE_FIRO: float = 0.001
    BLOCKNOTIFY_SECRET: str = ""

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

    # Firebase (server)
    FIREBASE_PROJECT_ID:           str   = ""
    FIREBASE_CLIENT_EMAIL:         str   = ""
    FIREBASE_PRIVATE_KEY:          str   = ""

    # Firebase (client / public, injected into templates)
    FIREBASE_API_KEY:              str   = ""
    FIREBASE_AUTH_DOMAIN:          str   = ""
    FIREBASE_APP_ID:               str   = ""

    GOOGLE_CLIENT_ID:              str   = ""

    TELEGRAM_BOT_USERNAME:         str   = ""
    TELEGRAM_BOT_TOKEN:            str   = ""

    @property
    def telegram_bot_enabled(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_BOT_USERNAME)

    RESEND_API_KEY:                str   = ""
    FROM_EMAIL:                    str   = ""
    FROM_NAME:                     str   = "FiroGate"

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

        if not self.SECRET_KEY:
            if is_local:
                object.__setattr__(self, "SECRET_KEY", secrets.token_hex(32))
                print(
                    "\n⚠️  WARNING: SECRET_KEY not set in .env using a random key.\n"
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

        # Warn but don't hard-fail: some deploys may not use encryption at all.
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
        Automatic detection no manual toggle needed.
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
            return "Testnet Mode No real funds. This is a testing environment only."
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
        (DASHBOARD_URL) when configured this keeps every Firebase+Turnstile
        flow on ONE origin where the Turnstile widget is registered and the
        cookie scope matches. Falls back to BASE_URL/login for single-host.
        """
        if self.DASHBOARD_URL:
            return self.DASHBOARD_URL.rstrip("/") + "/login"
        return self.BASE_URL.rstrip("/") + "/login"

    @property
    def operator_email_set(self) -> set[str]:
        """
        Parsed set of lowercase operator emails from OPERATOR_EMAILS. Used by
        login/register flows to auto-promote matching users to role=operator
        and by `require_operator` to accept a fresh operator before the DB row
        has been updated.
        """
        raw = (self.OPERATOR_EMAILS or "").strip()
        if not raw:
            # Fall back to the legacy single ADMIN_EMAIL so existing deploys
            # don't break the minute they pull this change.
            legacy = (self.ADMIN_EMAIL or "").strip().lower()
            return {legacy} if legacy else set()
        return {e.strip().lower() for e in raw.split(",") if e.strip()}

    def is_operator_email(self, email: str) -> bool:
        if not email:
            return False
        return email.strip().lower() in self.operator_email_set

    @property
    def operator_username_set(self) -> set[str]:
        raw = (self.OPERATOR_USERNAMES or "").strip()
        if not raw:
            return set()
        return {u.strip().lower() for u in raw.split(",") if u.strip()}

    def is_operator_username(self, username: str) -> bool:
        if not username:
            return False
        return username.strip().lower() in self.operator_username_set

    @property
    def allowed_origins(self) -> list[str]:
        origins = [self.BASE_URL.rstrip("/")]
        if self.ONION_URL:
            origins.append(self.ONION_URL.rstrip("/"))
        for url in [self.DASHBOARD_URL, self.CHECKOUT_URL, self.API_URL]:
            if url:
                origins.append(url.rstrip("/"))
        return list(dict.fromkeys(origins))  # deduplicate, preserve order

    @property
    def allowed_hosts(self) -> list[str]:
        """Host names accepted by TrustedHostMiddleware (Host-header defense).
        Derived from the configured URLs. Localhost is always allowed so local
        dev and the Docker healthcheck keep working. Returns ['*'] only if the
        deployment is still on the default localhost BASE_URL (dev mode)."""
        from urllib.parse import urlparse
        hosts: list[str] = []
        for url in self.allowed_origins:
            try:
                h = urlparse(url).hostname
                if h:
                    hosts.append(h)
            except Exception:
                pass
        # Always permit loopback for health checks / local access.
        hosts += ["localhost", "127.0.0.1"]
        hosts = list(dict.fromkeys(hosts))
        # If only localhost is configured (untouched default), don't lock down.
        non_local = [h for h in hosts if h not in ("localhost", "127.0.0.1")]
        if not non_local:
            return ["*"]
        return hosts

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
