"""Generated models for custom Octosphere lexicon records.

These models are based on lexicon/social.octosphere.publication.json
and follow the atproto SDK model patterns.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OctospherePublicationRecord:
    """Scientific publication record bridged from Octopus.ac via Octosphere.
    
    Maps to lexicon: social.octosphere.publication
    """
    # Required fields
    octopus_id: str
    version_id: str
    publication_type: str
    title: str
    status: str
    content_html: str
    content_text: str
    citations: list[str]
    linked_to: list[str]
    linked_from: list[str]
    created_at: str
    updated_at: str
    
    # Optional fields
    doi: Optional[str] = None
    owner_orcid: Optional[str] = None
    peer_review_of: Optional[str] = None
    canonical_url: Optional[str] = None

    def to_record_dict(self) -> dict:
        """Convert to AT Protocol record format (camelCase keys)."""
        record = {
            "octopusId": self.octopus_id,
            "versionId": self.version_id,
            "publicationType": self.publication_type,
            "title": self.title,
            "status": self.status,
            "contentHtml": self.content_html,
            "contentText": self.content_text,
            "citations": self.citations,
            "linkedTo": self.linked_to,
            "linkedFrom": self.linked_from,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        
        # Add optional fields only if present
        if self.doi:
            record["doi"] = self.doi
        if self.owner_orcid:
            record["ownerOrcid"] = self.owner_orcid
        if self.peer_review_of:
            record["peerReviewOf"] = self.peer_review_of
        if self.canonical_url:
            record["canonicalUrl"] = self.canonical_url
            
        return record

    @classmethod
    def from_dict(cls, data: dict) -> "OctospherePublicationRecord":
        """Create from a dict with camelCase keys."""
        return cls(
            octopus_id=data["octopusId"],
            version_id=data["versionId"],
            publication_type=data["publicationType"],
            title=data["title"],
            status=data["status"],
            content_html=data["contentHtml"],
            content_text=data["contentText"],
            citations=data.get("citations", []),
            linked_to=data.get("linkedTo", []),
            linked_from=data.get("linkedFrom", []),
            created_at=data["createdAt"],
            updated_at=data["updatedAt"],
            doi=data.get("doi"),
            owner_orcid=data.get("ownerOrcid"),
            peer_review_of=data.get("peerReviewOf"),
            canonical_url=data.get("canonicalUrl"),
        )


# Lexicon collection identifier
OCTOSPHERE_PUBLICATION_NSID = "social.octosphere.publication"

# Backwards compatibility alias (deprecated)
OCTOPUS_PUBLICATION_NSID = OCTOSPHERE_PUBLICATION_NSID

# Known publication types from the lexicon
class PublicationType:
    RESEARCH_PROBLEM = "RESEARCH_PROBLEM"
    HYPOTHESIS = "HYPOTHESIS"
    PROTOCOL = "PROTOCOL"
    ANALYSIS = "ANALYSIS"
    INTERPRETATION = "INTERPRETATION"
    REAL_WORLD_APPLICATION = "REAL_WORLD_APPLICATION"
    DATA = "DATA"
    PEER_REVIEW = "PEER_REVIEW"
