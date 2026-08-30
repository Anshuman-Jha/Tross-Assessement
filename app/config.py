"""Application configuration.

Every secret is read from the environment. Nothing sensitive is ever committed;
see `.env.example` for the shape of a working configuration.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_name: str = "linkedin-profile-api"
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    log_json: bool = True
    debug: bool = False

    # ------------------------------------------------------- our own API auth
    # Comma-separated list. When empty the API is open — fine locally, but the
    # deployed instance should always set at least one key.
    # NoDecode stops pydantic-settings JSON-decoding the env var, so the
    # comma-splitting validator below sees the raw string.
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)
    require_api_key: bool = True

    # --------------------------------------------------- LinkedIn credentials
    # Primary auth path: session cookies lifted from a logged-in browser.
    # `li_at` is the session cookie; `JSESSIONID` doubles as the CSRF token.
    linkedin_li_at: str | None = None
    linkedin_jsessionid: str | None = None

    # Multi-account pool. JSON list of {"li_at": ..., "jsessionid": ..., "label": ...}
    # Takes precedence over the single-cookie fields above when set.
    linkedin_accounts_json: str | None = None

    # Fallback auth path: programmatic login. Usually hits a CAPTCHA/2FA
    # checkpoint from a datacenter IP, so it is documented as best-effort only.
    linkedin_email: str | None = None
    linkedin_password: str | None = None
    enable_password_login: bool = False

    # ------------------------------------------------------------- behaviour
    # Which acquisition tiers are permitted, in preference order.
    enable_graphql_tier: bool = True
    enable_rest_tier: bool = True
    enable_html_tier: bool = True

    request_timeout_seconds: float = 20.0
    max_retries: int = 3
    # LinkedIn is quick to throttle. These defaults are deliberately gentle.
    max_concurrent_upstream: int = 4
    requests_per_minute_per_session: int = 30
    min_delay_between_requests_ms: int = 350
    jitter_ms: int = 400
    session_cooldown_seconds: int = 900

    # ---------------------------------------------------------------- cache
    redis_url: str | None = None
    cache_ttl_seconds: int = 3600
    cache_max_entries: int = 512

    # --------------------------------------------------------- query id file
    # Last-known-good GraphQL query id hashes. Operators can update this file
    # without a redeploy when LinkedIn rotates them.
    query_id_file: str = "query_ids.json"
    query_id_discovery_ttl_seconds: int = 21600  # 6h
    enable_query_id_discovery: bool = True

    # ------------------------------------------------------------ validators
    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_api_keys(cls, v: object) -> object:
        if isinstance(v, str):
            return [k.strip() for k in v.split(",") if k.strip()]
        return v

    # ------------------------------------------------------------- derived
    @property
    def accounts(self) -> list[dict[str, str]]:
        """Normalised list of credential dicts feeding the session pool."""
        if self.linkedin_accounts_json:
            try:
                raw = json.loads(self.linkedin_accounts_json)
            except json.JSONDecodeError as exc:  # pragma: no cover - config error
                raise ValueError(f"LINKEDIN_ACCOUNTS_JSON is not valid JSON: {exc}") from exc
            if not isinstance(raw, list):
                raise ValueError("LINKEDIN_ACCOUNTS_JSON must be a JSON list of objects")
            return [
                {
                    "li_at": a["li_at"],
                    "jsessionid": a.get("jsessionid", ""),
                    "label": a.get("label", f"account-{i}"),
                }
                for i, a in enumerate(raw)
                if a.get("li_at")
            ]
        if self.linkedin_li_at:
            return [
                {
                    "li_at": self.linkedin_li_at,
                    "jsessionid": self.linkedin_jsessionid or "",
                    "label": "primary",
                }
            ]
        return []

    @property
    def has_credentials(self) -> bool:
        return bool(self.accounts) or bool(
            self.enable_password_login and self.linkedin_email and self.linkedin_password
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
