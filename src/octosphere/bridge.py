"""Bridge logic for mapping Octopus publications to AT Proto records."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from octosphere.atproto.client import AtprotoAuth, AtprotoClient, CreateRecordResult
from octosphere.octopus.client import OctopusClient, OctopusPublication


@dataclass
class SyncResult:
    publication_id: str
    version_id: str
    uri: str
    cid: str


def _safe_text(value: str | None) -> str:
    return (value or "").strip()


def _extract_citations(version: dict[str, Any]) -> list[str]:
    citations: list[str] = []
    raw = version.get("references") or version.get("citations") or []
    if isinstance(raw, list):
        for ref in raw:
            if isinstance(ref, str):
                citations.append(ref)
            elif isinstance(ref, dict):
                text = ref.get("reference") or ref.get("citation") or ref.get("text")
                if text:
                    citations.append(str(text))
    return citations


def _publication_type(version: dict[str, Any], publication: dict[str, Any]) -> str:
    return (
        version.get("publicationType")
        or publication.get("publicationType")
        or publication.get("type")
        or "UNKNOWN"
    )


def _peer_review_of(version: dict[str, Any], publication: dict[str, Any]) -> str | None:
    peer_review = version.get("peerReviewOf") or publication.get("peerReviewOf")
    if isinstance(peer_review, dict):
        return str(peer_review.get("publicationId") or peer_review.get("id") or "") or None
    return str(peer_review) if peer_review else None


def build_record(
    client: OctopusClient,
    publication: OctopusPublication,
    version_content: dict[str, Any],
) -> dict[str, Any]:
    version = publication.version
    pub = publication.publication
    html = _safe_text(version_content.get("content") or version.get("content") or "")
    text = _safe_text(version_content.get("text") or version.get("contentText") or "")
    created_at = version.get("createdAt") or pub.get("createdAt")
    updated_at = version.get("updatedAt") or pub.get("updatedAt")
    title = _safe_text(version.get("title") or pub.get("title") or "Untitled")
    return {
        "octopusId": publication.publication_id,
        "versionId": publication.version_id,
        "publicationType": _publication_type(version, pub),
        "title": title,
        "status": pub.get("status") or version.get("status") or "LIVE",
        "doi": version.get("doi") or version.get("doiUrl"),
        "ownerOrcid": pub.get("ownerId") or pub.get("ownerOrcid"),
        "contentHtml": html,
        "contentText": text or html,
        "citations": _extract_citations(version),
        "linkedTo": publication.linked_to,
        "linkedFrom": publication.linked_from,
        "peerReviewOf": _peer_review_of(version, pub),
        "createdAt": created_at or datetime.utcnow().isoformat(),
        "updatedAt": updated_at or datetime.utcnow().isoformat(),
        "canonicalUrl": client.publication_url(
            publication.publication_id, publication.version_id
        ),
    }


def sync_publications(
    octopus: OctopusClient,
    atproto: AtprotoClient,
    auth: AtprotoAuth,
    user_id: str,
    already_synced: set[tuple[str, str]] | None = None,
) -> list[SyncResult]:
    """Sync Octopus publications to AT Protocol.
    
    Args:
        octopus: Octopus API client
        atproto: AT Protocol client
        auth: AT Protocol authentication
        user_id: Octopus user ID
        already_synced: Set of (publication_id, version_id) tuples that have already been synced.
                       If provided, these will be skipped to prevent duplicates.
    
    Returns:
        List of SyncResult for newly synced publications
    """
    results: list[SyncResult] = []
    already_synced = already_synced or set()
    
    publications = octopus.get_user_publications(user_id)
    for item in publications:
        mapped = octopus.map_publication(item)
        
        # Skip if already synced (duplicate prevention)
        if (mapped.publication_id, mapped.version_id) in already_synced:
            print(f"Skipping already synced: {mapped.publication_id}/{mapped.version_id}")
            continue
        
        # Use get_publication_chain which returns full version data including content
        # (the /publication-versions endpoint returns 403 Forbidden)
        pub_data = octopus.get_publication_chain(mapped.publication_id)
        # Find the matching version content from the publication data
        versions = pub_data.get("versions", [])
        version_content = next(
            (v for v in versions if str(v.get("id")) == mapped.version_id),
            versions[0] if versions else {}
        )
        record = build_record(octopus, mapped, version_content)
        
        # Use deterministic rkey based on publication_id for idempotency
        # This ensures that even if we accidentally sync twice, it updates rather than duplicates
        rkey = f"octopus-{mapped.publication_id}"
        created = atproto.create_publication_record(auth, record, rkey=rkey)
        results.append(
            SyncResult(
                publication_id=mapped.publication_id,
                version_id=mapped.version_id,
                uri=created.uri,
                cid=created.cid,
            )
        )
    return results
