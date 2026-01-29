"""Configuration helpers for the Octosphere bridge."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None or value == "":
        return None
    return value


@dataclass
class Settings:
    octopus_api_url: str
    octopus_web_url: str
    orcid_client_id: str
    orcid_client_secret: str
    orcid_redirect_uri: str
    orcid_base_url: str = "https://orcid.org"
    orcid_token_url: str = "https://orcid.org/oauth/token"
    orcid_scope: str = "/authenticate"
    atproto_pds_url: str = "https://bsky.social"
    session_secret: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        missing: list[str] = []

        def req(name: str, default: str | None = None) -> str:
            value = _env(name, default)
            if not value:
                missing.append(name)
                return ""
            return value

        settings = cls(
            octopus_api_url=req("OCTOPUS_API_URL"),
            octopus_web_url=req("OCTOPUS_WEB_URL"),
            orcid_client_id=req("ORCID_CLIENT_ID"),
            orcid_client_secret=req("ORCID_CLIENT_SECRET"),
            orcid_redirect_uri=req("ORCID_REDIRECT_URI"),
            orcid_base_url=_env("ORCID_BASE_URL", "https://orcid.org") or "https://orcid.org",
            orcid_token_url=_env("ORCID_TOKEN_URL", "https://orcid.org/oauth/token")
            or "https://orcid.org/oauth/token",
            orcid_scope=_env("ORCID_SCOPE", "/authenticate") or "/authenticate",
            atproto_pds_url=_env("ATPROTO_PDS_URL", "https://bsky.social")
            or "https://bsky.social",
            session_secret=_env("OCTOSPHERE_SESSION_SECRET"),
        )

        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )
        return settings
