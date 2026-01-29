"""Tests for Octopus API client."""
import pytest
import responses

from octosphere.octopus.client import OctopusClient, OctopusPublication


@pytest.fixture
def client():
    return OctopusClient(
        api_url="https://prod.api.octopus.ac/v1",
        web_url="https://www.octopus.ac",
        access_token=None,
    )


class TestOctopusClient:
    @responses.activate
    def test_get_user_publications_returns_list(self, client):
        """Test fetching user publications returns a list."""
        responses.add(
            responses.GET,
            "https://prod.api.octopus.ac/v1/users/0000-0001-2345-6789/publications",
            json={"data": [{"id": "pub-1", "title": "Test Publication"}]},
            status=200,
        )
        
        result = client.get_user_publications("0000-0001-2345-6789")
        
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == "pub-1"

    @responses.activate
    def test_get_user_publications_handles_direct_list(self, client):
        """Test API returning list directly (no data wrapper)."""
        responses.add(
            responses.GET,
            "https://prod.api.octopus.ac/v1/users/0000-0001-2345-6789/publications",
            json=[{"id": "pub-1"}],
            status=200,
        )
        
        result = client.get_user_publications("0000-0001-2345-6789")
        
        assert len(result) == 1

    @responses.activate
    def test_get_version_content(self, client):
        """Test fetching publication version content."""
        responses.add(
            responses.GET,
            "https://prod.api.octopus.ac/v1/publication-versions/ver-123",
            json={"id": "ver-123", "content": "<p>Test content</p>"},
            status=200,
        )
        
        result = client.get_version_content("ver-123")
        
        assert result["id"] == "ver-123"
        assert "<p>" in result["content"]

    def test_map_publication_with_nested_structure(self, client):
        """Test mapping publication with nested publication/latestVersion."""
        item = {
            "publication": {"id": "pub-1", "title": "Test"},
            "latestVersion": {"id": "ver-1", "title": "Test v1"},
            "linked": {
                "linkedTo": [{"id": "pub-2"}],
                "linkedFrom": [{"id": "pub-3"}],
            },
        }
        
        result = client.map_publication(item)
        
        assert isinstance(result, OctopusPublication)
        assert result.publication_id == "pub-1"
        assert result.version_id == "ver-1"
        assert result.linked_to == ["pub-2"]
        assert result.linked_from == ["pub-3"]

    def test_map_publication_flat_structure(self, client):
        """Test mapping publication with flat structure."""
        item = {"id": "pub-flat", "title": "Flat Publication"}
        
        result = client.map_publication(item)
        
        assert result.publication_id == "pub-flat"

    def test_publication_url(self, client):
        """Test generating canonical publication URL."""
        url = client.publication_url("pub-123", "ver-456")
        
        assert url == "https://www.octopus.ac/publications/pub-123/versions/ver-456"

    def test_extract_user_id_from_url(self):
        """Test extracting internal user ID from author page URL."""
        url = "https://www.octopus.ac/authors/cl5smny4a000009ieqml45bhz"
        result = OctopusClient.extract_user_id_from_url(url)
        assert result == "cl5smny4a000009ieqml45bhz"

    def test_extract_user_id_from_url_invalid(self):
        """Test extract returns None for invalid URLs."""
        assert OctopusClient.extract_user_id_from_url("https://example.com") is None
        assert OctopusClient.extract_user_id_from_url("not-a-url") is None
