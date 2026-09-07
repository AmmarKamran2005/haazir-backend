"""Settings. Every environment variable the API reads is declared here and nowhere else.

The one piece of real logic in this module is `sqlalchemy_url`. Neon hands out libpq URLs
carrying `sslmode` and `channel_binding` query parameters. asyncpg does not accept either as a
DSN parameter and raises `TypeError: connect() got an unexpected keyword argument 'sslmode'`
at the first connection attempt, which looks like a credentials problem and is not. The
parameters are stripped here and TLS is turned on through `connect_args` instead.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pathlib

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# asyncpg rejects these; libpq accepts them. Neon includes them by default.
_LIBPQ_ONLY_PARAMS = {"sslmode", "channel_binding", "options", "target_session_attrs"}


# The package lives at api/src/haazir, so api/.env is three levels up. Anchoring to the file
# rather than to the working directory matters: launched from the repo root — which is what a
# dev-server config or a `python -m uvicorn --app-dir api/src` does — a CWD-relative path finds
# nothing, and the API starts up healthy with no database and fails on the first request. That
# looked like a connection problem for longer than it should have.
_PACKAGE_ENV = pathlib.Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Still CWD-relative first, so a deliberate local override keeps working.
        env_file=(".env", "../.env", _PACKAGE_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── environment ───────────────────────────────────────────────────────────
    app_env: str = "dev"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:3000,http://localhost:4173"

    # ── database ──────────────────────────────────────────────────────────────
    database_url: str = Field(default="", description="Neon POOLED connection string")
    database_url_direct: str = Field(default="", description="session-scoped features only")
    db_echo: bool = False

    # Background jobs run inside the API process (plan §11), so exactly one instance may
    # have this on. Two processes refreshing live_state every minute would not corrupt
    # anything and would double the compute bill on a hundred-hour-a-month plan.
    run_scheduler: bool = True

    # The blanket per-IP write throttle (auth/throttle.py). On everywhere except the
    # test suite, where it would turn every multi-step test into a race against a
    # per-minute ceiling. The middleware has its own tests instead.
    rate_limit_enabled: bool = True

    # ── auth ──────────────────────────────────────────────────────────────────
    jwt_secret: str = Field(default="", description="32+ random bytes. Server only.")
    jwt_issuer: str = "haazir"
    jwt_access_ttl: int = 1800  # 30 minutes
    jwt_refresh_ttl: int = 7_776_000  # 90 days
    jwt_guest_ttl: int = 86_400  # 24 hours, group invite scope
    jwt_device_ttl: int = 7_776_000  # 90 days, staff device
    jwt_admin_ttl: int = 604_800  # 7 days

    magic_link_ttl: int = 900  # 15 minutes, §5 rule 3
    cookie_domain: str = ""
    cookie_secure: bool = True
    # `lax` while the API and the web app share a site — localhost:3000 and localhost:8000 do,
    # so development never exercises the alternative. Deployed apart (vercel.app calling
    # fly.dev) they are cross-site, `lax` is silently not sent, and the refresh cookie stops
    # working the moment the 30-minute access token expires. That must be `none`.
    #
    # `none` widens CSRF only in principle here: the cookie authenticates exactly one
    # endpoint, `/v1/auth/refresh`, and its response is unreadable to any origin CORS does not
    # already allow. Every other authenticated route takes a Bearer token, which a browser
    # never attaches on its own.
    cookie_samesite: str = "lax"

    @field_validator("cookie_samesite")
    @classmethod
    def _samesite_is_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"lax", "strict", "none"}:
            raise ValueError(f"cookie_samesite must be lax, strict or none, not {v!r}")
        return v

    admin_emails: str = ""  # comma separated allowlist

    # ── rate limits (§5 rule 4) ───────────────────────────────────────────────
    rl_link_per_email: int = 3
    rl_link_per_email_window: int = 900
    rl_link_per_ip: int = 10
    rl_link_per_ip_window: int = 3600
    rl_staff_per_venue_hour: int = 20

    # ── mail ──────────────────────────────────────────────────────────────────
    resend_api_key: str = ""
    mail_from: str = "HAAZIR <login@haazir.pk>"
    web_base_url: str = "http://localhost:3000"

    # ── llm ───────────────────────────────────────────────────────────────────
    # Gemini is the provider. `anthropic_api_key` is kept because nothing is gained by
    # deleting it and a deployment may still have it set; only Gemini is called.
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    llm_monthly_ceiling_usd: float = 25.0

    # ── routing ───────────────────────────────────────────────────────────────
    osrm_url: str = ""
    mapbox_token: str = ""

    # ── storage / observability ───────────────────────────────────────────────
    r2_account_id: str = ""
    r2_access_key: str = ""
    r2_secret: str = ""
    r2_bucket: str = ""
    sentry_dsn: str = ""

    @field_validator("app_env")
    @classmethod
    def _env_known(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"dev", "test", "prod"}:
            raise ValueError("APP_ENV must be dev, test or prod")
        return v

    # ── derived ───────────────────────────────────────────────────────────────
    @model_validator(mode="after")
    def _samesite_none_requires_secure(self) -> "Settings":
        """A browser drops `SameSite=None` outright unless `Secure` is also set.

        Caught here rather than in a browser: the symptom is a refresh cookie that is never
        stored and a session that dies after thirty minutes, with nothing in any log.
        """
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise ValueError(
                "COOKIE_SAMESITE=none requires COOKIE_SECURE=true; browsers discard the "
                "cookie otherwise and sign-in silently stops surviving a refresh."
            )
        return self

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.admin_emails.split(",") if e.strip()]

    @property
    def sqlalchemy_url(self) -> str:
        return _to_asyncpg(self.database_url)

    @property
    def sqlalchemy_url_direct(self) -> str:
        return _to_asyncpg(self.database_url_direct or self.database_url)


def _normalise_scheme(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url.split("://", 1)[1]
    if url.startswith("postgres://"):
        return "postgresql://" + url.split("://", 1)[1]
    return url


def _to_asyncpg(url: str) -> str:
    """`postgres://…?sslmode=require&channel_binding=require` -> `postgresql+asyncpg://…`."""
    if not url:
        return ""
    parts = urlsplit(_normalise_scheme(url))
    kept = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _LIBPQ_ONLY_PARAMS]
    return urlunsplit(
        ("postgresql+asyncpg", parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )


def to_psycopg_url(url: str) -> str:
    """Sync driver form, for Alembic's offline mode and psql-style tooling."""
    if not url:
        return ""
    parts = urlsplit(_normalise_scheme(url))
    return urlunsplit(("postgresql", parts.netloc, parts.path, parts.query, parts.fragment))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
