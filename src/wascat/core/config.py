"""Application settings, loaded from the environment.

Every setting is prefixed ``WASCAT_`` so the process environment stays legible
next to whatever else runs beside it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WASCAT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = "local"
    debug: bool = False

    # -- Database ----------------------------------------------------------
    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+asyncpg://wascat:wascat@localhost:5432/wascat"),
    )
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # -- Public URLs -------------------------------------------------------
    # The frontend proxies /api/* to this service, so the request the backend
    # sees carries an internal host. `links.self` must echo what the browser
    # actually asked for, so the public origin is configured rather than
    # inferred. See risk R7 in the plan.
    public_base_url: str = ""
    # Prefix for object keys. Empty renders "/frames/vid1/2-source.jpg", which
    # is byte-identical to what the bundled catalogue served.
    public_asset_base_url: str = ""

    # -- Object storage ----------------------------------------------------
    s3_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "wascat"
    s3_region: str = "us-east-1"
    s3_access_key: str = "wascat"
    # Local MinIO credentials. Production supplies real ones via the
    # environment; these exist so `docker compose up` works out of the box.
    s3_secret_key: str = "wascat-dev-secret"  # noqa: S105
    storage_backend: Literal["s3", "local"] = "s3"
    local_storage_root: str = ".storage"

    # -- Security ----------------------------------------------------------
    # Refused at startup outside local/test; see _refuse_dev_defaults_in_production.
    secret_key: str = "dev-only-change-me"  # noqa: S105
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 30 * 24 * 60 * 60
    cookie_secure: bool = True
    cookie_domain: str | None = None
    admin_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # -- Uploads -----------------------------------------------------------
    max_upload_bytes: int = 32 * 1024 * 1024

    # -- Behaviour flags ---------------------------------------------------
    # The pre-migration API emitted static rate-limit headers. Turning on real
    # enforcement changes X-RateLimit-Remaining on every request, which the
    # recorded contract fixtures pin, so it is opt-in. See risk R19.
    rate_limit_enforce: bool = False
    # Adding a vocabulary term to a kind the public API validates as an enum
    # widens the contract, so it requires an explicit opt-in.
    allow_vocab_expansion: bool = False

    # -- Frontend revalidation --------------------------------------------
    frontend_revalidate_url: str = ""
    frontend_revalidate_secret: str = ""

    @field_validator("public_base_url", "public_asset_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _refuse_dev_defaults_in_production(self) -> Settings:
        """Fail fast rather than sign tokens with a public constant.

        A default secret that reaches production would let anyone mint a valid
        admin session, and the failure is silent - everything works, which is
        exactly what makes it dangerous. Better to refuse to boot.
        """
        if not self.is_production:
            return self
        unsafe = [
            name
            for name, value in (
                ("WASCAT_SECRET_KEY", self.secret_key),
                ("WASCAT_S3_SECRET_KEY", self.s3_secret_key),
            )
            if value in {"dev-only-change-me", "wascat-dev-secret"}
        ]
        if unsafe:
            raise ValueError(
                f"{', '.join(unsafe)} still hold development defaults in "
                f"environment={self.environment!r}. Set them explicitly."
            )
        # HMAC-SHA256 keys shorter than the 32-byte digest add no security
        # over a 32-byte one and PyJWT warns about them; refusing is clearer
        # than a warning nobody reads in production logs.
        if len(self.secret_key.encode()) < 32:
            raise ValueError(
                "WASCAT_SECRET_KEY must be at least 32 bytes. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if not self.public_base_url:
            # Without it, links.self leaks the internal origin the frontend
            # proxies to. See risk R7.
            raise ValueError(
                "WASCAT_PUBLIC_BASE_URL must be set outside local development so "
                "links.self echoes the public URL rather than the internal one."
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment in ("staging", "production")

    @property
    def database_url_str(self) -> str:
        return str(self.database_url)

    @property
    def sync_database_url(self) -> str:
        """Alembic's offline mode and a few tools want a non-async driver."""
        return self.database_url_str.replace("+asyncpg", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()
