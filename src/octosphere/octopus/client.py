"""Octopus API client and mapping helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class OctopusPublication:
    publication: dict[str, Any]
    version: dict[str, Any]
    linked_to: list[str]
    linked_from: list[str]

    @property
    def publication_id(self) -> str:
        return str(self.publication.get("id"))

    @property
    def version_id(self) -> str:
        return str(self.version.get("id"))


class OctopusClient:
    def __init__(self, api_url: str, web_url: str, access_token: str | None = None):
        self.api_url = api_url.rstrip("/")
        self.web_url = web_url.rstrip("/")
        self.access_token = access_token

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def get_user_publications(self, user_id: str) -> list[dict[str, Any]]:
        url = f"{self.api_url}/users/{user_id}/publications"
        response = requests.get(url, headers=self._headers(), timeout=30)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        if isinstance(data, list):
            return data
        return []

    def get_publication_chain(self, publication_id: str) -> dict[str, Any]:
        url = f"{self.api_url}/publications/{publication_id}"
        response = requests.get(url, headers=self._headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def get_version_content(self, version_id: str) -> dict[str, Any]:
        url = f"{self.api_url}/publication-versions/{version_id}"
        response = requests.get(url, headers=self._headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def map_publication(self, item: dict[str, Any]) -> OctopusPublication:
        publication = item.get("publication") or item.get("publicationData") or item
        version = item.get("latestVersion") or item.get("publicationVersion") or item
        linked = item.get("linked") or {}
        linked_to = [str(p.get("id")) for p in linked.get("linkedTo", [])]
        linked_from = [str(p.get("id")) for p in linked.get("linkedFrom", [])]
        return OctopusPublication(publication, version, linked_to, linked_from)

    def publication_url(self, publication_id: str, version_id: str) -> str:
        return f"{self.web_url}/publications/{publication_id}/versions/{version_id}"

    def get_user_info(self, user_id: str) -> dict[str, Any]:
        """Get user info by internal Octopus user ID."""
        url = f"{self.api_url}/users/{user_id}"
        response = requests.get(url, headers=self._headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def search_publications_by_orcid(self, orcid: str, limit: int = 100) -> list[dict[str, Any]]:
        """Search publication-versions and filter by author ORCID.
        
        Note: The Octopus API doesn't have a direct ORCID lookup, so we search
        and filter client-side.
        """
        # Search using ORCID - the search will match text but we filter exactly
        url = f"{self.api_url}/publication-versions"
        params = {"limit": limit}
        response = requests.get(url, headers=self._headers(), params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        results = data.get("data", []) if isinstance(data, dict) else data
        
        # Filter to only publications by this exact ORCID
        return [
            item for item in results
            if item.get("user", {}).get("orcid") == orcid
        ]

    @staticmethod
    def extract_user_id_from_url(url: str) -> str | None:
        """Extract internal Octopus user ID from author page URL.
        
        Example: https://www.octopus.ac/authors/cl5smny4a000009ieqml45bhz
        Returns: cl5smny4a000009ieqml45bhz
        """
        import re
        match = re.search(r'/authors/([a-zA-Z0-9]+)', url)
        return match.group(1) if match else None
