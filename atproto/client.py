"""AT Proto client using app password auth."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class AtprotoAuth:
    did: str
    access_jwt: str
    refresh_jwt: str


class AtprotoClient:
    def __init__(self, pds_url: str):
        self.pds_url = pds_url.rstrip("/")

    def create_session(self, handle: str, app_password: str) -> AtprotoAuth:
        url = f"{self.pds_url}/xrpc/com.atproto.server.createSession"
        response = requests.post(
            url,
            json={"identifier": handle, "password": app_password},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return AtprotoAuth(
            did=payload["did"],
            access_jwt=payload["accessJwt"],
            refresh_jwt=payload["refreshJwt"],
        )

    def create_publication_record(
        self,
        auth: AtprotoAuth,
        record: dict[str, Any],
        repo: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.pds_url}/xrpc/com.atproto.repo.createRecord"
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {auth.access_jwt}"},
            json={
                "repo": repo or auth.did,
                "collection": "com.octopus.publication",
                "record": record,
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
