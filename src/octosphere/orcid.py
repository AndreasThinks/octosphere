"""Minimal ORCID OAuth helper."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class OrcidProfile:
    orcid: str
    access_token: str
    name: str | None = None


class OrcidClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        base_url: str,
        token_url: str,
        scope: str,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url
        self.scope = scope

    def auth_url(self, state: str) -> str:
        return (
            f"{self.base_url}/oauth/authorize?client_id={self.client_id}"
            f"&response_type=code&scope={self.scope}"
            f"&redirect_uri={self.redirect_uri}&state={state}"
        )

    def exchange_code(self, code: str) -> OrcidProfile:
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        response = requests.post(self.token_url, data=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        return OrcidProfile(
            orcid=data.get("orcid") or data.get("orcid_id") or "",
            access_token=data.get("access_token") or "",
            name=data.get("name"),
        )

    def fetch_record(self, profile: OrcidProfile) -> dict[str, Any]:
        if not profile.orcid:
            return {}
        url = f"{self.base_url}/v3.0/{profile.orcid}/record"
        response = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {profile.access_token}",
                "Accept": "application/json",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            return {}
        return response.json()
