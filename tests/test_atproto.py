"""Tests for AT Protocol client."""
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from octosphere.atproto.client import AtprotoClient, AtprotoAuth, CreateRecordResult
from octosphere.atproto.models import OCTOSPHERE_PUBLICATION_NSID


class TestAtprotoAuth:
    def test_dataclass_fields(self):
        auth = AtprotoAuth(
            did="did:plc:abc123",
            handle="alice.bsky.social",
            access_jwt="access_token",
            refresh_jwt="refresh_token",
            pds_endpoint="https://bsky.social",
        )
        
        assert auth.did == "did:plc:abc123"
        assert auth.handle == "alice.bsky.social"
        assert auth.access_jwt == "access_token"
        assert auth.refresh_jwt == "refresh_token"
        assert auth.pds_endpoint == "https://bsky.social"


class TestCreateRecordResult:
    def test_dataclass_fields(self):
        result = CreateRecordResult(
            uri="at://did:plc:xxx/social.octosphere.publication/abc",
            cid="bafyrei123",
        )
        
        assert result.uri == "at://did:plc:xxx/social.octosphere.publication/abc"
        assert result.cid == "bafyrei123"


class TestAtprotoClient:
    def test_init_with_default_pds(self):
        client = AtprotoClient()
        assert client.default_pds_url == "https://bsky.social"

    def test_init_with_custom_pds(self):
        client = AtprotoClient("https://custom.pds.example.com/")
        assert client.default_pds_url == "https://custom.pds.example.com"  # Trailing slash removed

    @patch("octosphere.atproto.client.IdResolver")
    def test_resolve_pds_endpoint_success(self, mock_resolver_class):
        """Test PDS resolution from handle."""
        mock_resolver = MagicMock()
        mock_resolver_class.return_value = mock_resolver
        mock_resolver.handle.resolve.return_value = "did:plc:abc"
        mock_did_doc = MagicMock()
        mock_did_doc.pds_endpoint = "https://user.pds.example.com"
        mock_resolver.did.resolve.return_value = mock_did_doc
        
        client = AtprotoClient()
        endpoint = client._resolve_pds_endpoint("alice.example.com")
        
        assert endpoint == "https://user.pds.example.com"
        mock_resolver.handle.resolve.assert_called_once_with("alice.example.com")

    @patch("octosphere.atproto.client.IdResolver")
    def test_resolve_pds_endpoint_fallback(self, mock_resolver_class):
        """Test fallback to default PDS on resolution failure."""
        mock_resolver = MagicMock()
        mock_resolver_class.return_value = mock_resolver
        mock_resolver.handle.resolve.side_effect = Exception("Resolution failed")
        
        client = AtprotoClient("https://fallback.pds.example.com")
        endpoint = client._resolve_pds_endpoint("alice.example.com")
        
        assert endpoint == "https://fallback.pds.example.com"

    @patch("octosphere.atproto.client.Client")
    @patch("octosphere.atproto.client.IdResolver")
    def test_login_success(self, mock_resolver_class, mock_client_class):
        """Test successful login."""
        mock_resolver = MagicMock()
        mock_resolver_class.return_value = mock_resolver
        mock_resolver.handle.resolve.return_value = "did:plc:test"
        mock_did_doc = MagicMock()
        mock_did_doc.pds_endpoint = "https://test.pds.com"
        mock_resolver.did.resolve.return_value = mock_did_doc
        
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_profile = MagicMock()
        mock_profile.did = "did:plc:test"
        mock_profile.handle = "test.bsky.social"
        mock_client.login.return_value = mock_profile
        mock_client._session.access_jwt = "access_jwt_token"
        mock_client._session.refresh_jwt = "refresh_jwt_token"
        
        client = AtprotoClient()
        auth = client.login("test.bsky.social", "app-password")
        
        assert auth.did == "did:plc:test"
        assert auth.handle == "test.bsky.social"
        assert auth.access_jwt == "access_jwt_token"
        mock_client.login.assert_called_once_with("test.bsky.social", "app-password")

    @patch("octosphere.atproto.client.Client")
    @patch("octosphere.atproto.client.IdResolver")
    def test_create_session_calls_login(self, mock_resolver_class, mock_client_class):
        """Test that create_session calls login internally."""
        # Setup mocks
        mock_resolver = MagicMock()
        mock_resolver_class.return_value = mock_resolver
        mock_resolver.handle.resolve.return_value = "did:plc:test"
        mock_did_doc = MagicMock()
        mock_did_doc.pds_endpoint = "https://test.pds.com"
        mock_resolver.did.resolve.return_value = mock_did_doc
        
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_profile = MagicMock()
        mock_profile.did = "did:plc:test"
        mock_profile.handle = "test.bsky.social"
        mock_client.login.return_value = mock_profile
        mock_client._session.access_jwt = "token"
        mock_client._session.refresh_jwt = "refresh"
        
        client = AtprotoClient()
        auth = client.create_session("test.bsky.social", "password")
        
        # Verify it behaves the same as login
        assert auth.did == "did:plc:test"
        assert auth.handle == "test.bsky.social"
        mock_client.login.assert_called_once_with("test.bsky.social", "password")

    def test_create_publication_record_requires_login(self):
        """Test that create_publication_record raises without login."""
        client = AtprotoClient()
        auth = AtprotoAuth(
            did="did:plc:test",
            handle="test.bsky.social",
            access_jwt="token",
            refresh_jwt="refresh",
            pds_endpoint="https://bsky.social",
        )
        
        with pytest.raises(RuntimeError, match="Not logged in"):
            client.create_publication_record(auth, {"title": "Test"})

    @patch("octosphere.atproto.client.Client")
    @patch("octosphere.atproto.client.IdResolver")
    def test_create_publication_record(self, mock_resolver_class, mock_client_class):
        """Test creating a publication record."""
        # Setup mocks for login
        mock_resolver = MagicMock()
        mock_resolver_class.return_value = mock_resolver
        mock_resolver.handle.resolve.return_value = "did:plc:test"
        mock_did_doc = MagicMock()
        mock_did_doc.pds_endpoint = "https://test.pds.com"
        mock_resolver.did.resolve.return_value = mock_did_doc
        
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_profile = MagicMock()
        mock_profile.did = "did:plc:test"
        mock_profile.handle = "test.bsky.social"
        mock_client.login.return_value = mock_profile
        mock_client._session.access_jwt = "token"
        mock_client._session.refresh_jwt = "refresh"
        
        # Mock create_record response
        mock_response = MagicMock()
        mock_response.uri = "at://did:plc:test/social.octosphere.publication/abc"
        mock_response.cid = "bafyrei123"
        mock_client.com.atproto.repo.create_record.return_value = mock_response
        
        client = AtprotoClient()
        auth = client.login("test.bsky.social", "password")
        
        record = {"title": "Test Publication", "octopusId": "pub-123"}
        result = client.create_publication_record(auth, record)
        
        assert result.uri == "at://did:plc:test/social.octosphere.publication/abc"
        assert result.cid == "bafyrei123"
        mock_client.com.atproto.repo.create_record.assert_called_once()

    def test_delete_record_requires_login(self):
        """Test that delete_record raises without login."""
        client = AtprotoClient()
        auth = AtprotoAuth(
            did="did:plc:test",
            handle="test.bsky.social",
            access_jwt="token",
            refresh_jwt="refresh",
            pds_endpoint="https://bsky.social",
        )
        
        with pytest.raises(RuntimeError, match="Not logged in"):
            client.delete_record(auth, "at://did:plc:test/social.octosphere.publication/abc")

    def test_delete_record_validates_uri_format(self):
        """Test that delete_record validates AT URI format."""
        client = AtprotoClient()
        client._client = MagicMock()  # Fake login state
        
        auth = AtprotoAuth(
            did="did:plc:test",
            handle="test.bsky.social",
            access_jwt="token",
            refresh_jwt="refresh",
            pds_endpoint="https://bsky.social",
        )
        
        with pytest.raises(ValueError, match="Invalid AT URI"):
            client.delete_record(auth, "https://invalid-uri")

    def test_delete_record_validates_uri_parts(self):
        """Test that delete_record requires all URI parts."""
        client = AtprotoClient()
        client._client = MagicMock()  # Fake login state
        
        auth = AtprotoAuth(
            did="did:plc:test",
            handle="test.bsky.social",
            access_jwt="token",
            refresh_jwt="refresh",
            pds_endpoint="https://bsky.social",
        )
        
        with pytest.raises(ValueError, match="Invalid AT URI format"):
            client.delete_record(auth, "at://did:plc:test/collection")  # Missing rkey


class TestOctosphereNSID:
    def test_nsid_value(self):
        """Test that the NSID constant is correct."""
        assert OCTOSPHERE_PUBLICATION_NSID == "social.octosphere.publication"
