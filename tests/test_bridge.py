"""Tests for bridge logic - mapping Octopus publications to AT Proto records."""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

from octosphere.bridge import (
    build_record,
    _safe_text,
    _extract_citations,
    _publication_type,
    _peer_review_of,
    SyncResult,
)
from octosphere.octopus.client import OctopusPublication


class TestSafeText:
    def test_strips_whitespace(self):
        assert _safe_text("  hello  ") == "hello"

    def test_handles_none(self):
        assert _safe_text(None) == ""

    def test_handles_empty_string(self):
        assert _safe_text("") == ""


class TestExtractCitations:
    def test_extracts_string_list(self):
        version = {"references": ["Citation 1", "Citation 2"]}
        result = _extract_citations(version)
        assert result == ["Citation 1", "Citation 2"]

    def test_extracts_from_dict_list_with_reference(self):
        version = {"references": [
            {"reference": "Ref A"},
            {"reference": "Ref B"},
        ]}
        result = _extract_citations(version)
        assert result == ["Ref A", "Ref B"]

    def test_extracts_from_dict_list_with_citation(self):
        version = {"citations": [{"citation": "Cite 1"}]}
        result = _extract_citations(version)
        assert result == ["Cite 1"]

    def test_extracts_from_dict_list_with_text(self):
        version = {"references": [{"text": "Text ref"}]}
        result = _extract_citations(version)
        assert result == ["Text ref"]

    def test_handles_empty_list(self):
        assert _extract_citations({"references": []}) == []

    def test_handles_missing_field(self):
        assert _extract_citations({}) == []

    def test_handles_none_values(self):
        assert _extract_citations({"references": None}) == []


class TestPublicationType:
    def test_uses_version_type_first(self):
        version = {"publicationType": "HYPOTHESIS"}
        publication = {"type": "REVIEW"}
        assert _publication_type(version, publication) == "HYPOTHESIS"

    def test_falls_back_to_publication_type(self):
        version = {}
        publication = {"publicationType": "DATA"}
        assert _publication_type(version, publication) == "DATA"

    def test_falls_back_to_type_field(self):
        version = {}
        publication = {"type": "ANALYSIS"}
        assert _publication_type(version, publication) == "ANALYSIS"

    def test_defaults_to_unknown(self):
        assert _publication_type({}, {}) == "UNKNOWN"


class TestPeerReviewOf:
    def test_extracts_from_dict(self):
        version = {"peerReviewOf": {"publicationId": "pub-123"}}
        result = _peer_review_of(version, {})
        assert result == "pub-123"

    def test_extracts_from_dict_with_id(self):
        version = {"peerReviewOf": {"id": "pub-456"}}
        result = _peer_review_of(version, {})
        assert result == "pub-456"

    def test_extracts_string_value(self):
        version = {"peerReviewOf": "pub-789"}
        result = _peer_review_of(version, {})
        assert result == "pub-789"

    def test_falls_back_to_publication(self):
        version = {}
        publication = {"peerReviewOf": "pub-abc"}
        result = _peer_review_of(version, publication)
        assert result == "pub-abc"

    def test_returns_none_when_missing(self):
        assert _peer_review_of({}, {}) is None


class TestBuildRecord:
    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.publication_url.return_value = "https://octopus.ac/pubs/pub-1/v1"
        return client

    @pytest.fixture
    def sample_publication(self):
        return OctopusPublication(
            publication={"id": "pub-123", "type": "HYPOTHESIS", "ownerId": "0000-0001-2345-6789"},
            version={"id": "ver-456", "title": "Test Publication", "doi": "10.1234/test"},
            linked_to=["pub-linked-1"],
            linked_from=["pub-linked-2"],
        )

    def test_builds_complete_record(self, mock_client, sample_publication):
        version_content = {
            "content": "<p>HTML content</p>",
            "text": "Plain text content",
        }
        
        record = build_record(mock_client, sample_publication, version_content)
        
        assert record["octopusId"] == "pub-123"
        assert record["versionId"] == "ver-456"
        assert record["title"] == "Test Publication"
        assert record["contentHtml"] == "<p>HTML content</p>"
        assert record["contentText"] == "Plain text content"
        assert record["doi"] == "https://doi.org/10.1234/test"
        assert record["linkedTo"] == ["pub-linked-1"]
        assert record["linkedFrom"] == ["pub-linked-2"]
        assert record["canonicalUrl"] == "https://octopus.ac/pubs/pub-1/v1"

    def test_uses_html_as_fallback_for_text(self, mock_client, sample_publication):
        version_content = {"content": "<p>Only HTML</p>"}
        
        record = build_record(mock_client, sample_publication, version_content)
        
        assert record["contentHtml"] == "<p>Only HTML</p>"
        assert record["contentText"] == "<p>Only HTML</p>"

    def test_handles_missing_content(self, mock_client, sample_publication):
        record = build_record(mock_client, sample_publication, {})
        
        assert record["contentHtml"] == ""
        assert record["contentText"] == ""

    def test_extracts_publication_type(self, mock_client, sample_publication):
        record = build_record(mock_client, sample_publication, {})
        assert record["publicationType"] == "HYPOTHESIS"

    def test_uses_created_at_from_version(self, mock_client, sample_publication):
        sample_publication.version["createdAt"] = "2024-01-15T10:00:00Z"
        
        record = build_record(mock_client, sample_publication, {})
        
        assert record["createdAt"] == "2024-01-15T10:00:00Z"


class TestSyncResult:
    def test_dataclass_fields(self):
        result = SyncResult(
            publication_id="pub-1",
            version_id="ver-1",
            uri="at://did:plc:xxx/social.octosphere.publication/abc",
            cid="bafyrei...",
        )
        
        assert result.publication_id == "pub-1"
        assert result.version_id == "ver-1"
        assert result.uri == "at://did:plc:xxx/social.octosphere.publication/abc"
        assert result.cid == "bafyrei..."
