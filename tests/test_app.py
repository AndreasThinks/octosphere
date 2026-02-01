"""Tests for app.py - sync status polling functionality."""
import pytest
import threading
import time
from unittest.mock import MagicMock, patch

# Mock settings before importing app
import os
os.environ.setdefault("SESSION_SECRET", "test-secret-key")
os.environ.setdefault("ORCID_CLIENT_ID", "test-client-id")
os.environ.setdefault("ORCID_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("ORCID_REDIRECT_URI", "http://localhost:5001/callback")
os.environ.setdefault("ATPROTO_PDS_URL", "https://bsky.social")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODkwMTI=")  # base64 test key

from octosphere.bridge import SyncResult


class TestSyncStatusTracking:
    """Test the in-memory sync status tracking."""
    
    def test_sync_status_dict_is_thread_safe(self):
        """Test that multiple threads can safely update sync status."""
        from octosphere.app import _sync_status, _sync_lock
        
        # Clear any existing state
        with _sync_lock:
            _sync_status.clear()
        
        def update_status(orcid, value):
            with _sync_lock:
                _sync_status[orcid] = {"status": value}
        
        threads = []
        for i in range(10):
            t = threading.Thread(target=update_status, args=(f"orcid-{i}", f"value-{i}"))
            threads.append(t)
            t.start()
        
        for t in threads:
            t.join()
        
        # All 10 entries should be present
        with _sync_lock:
            assert len(_sync_status) == 10
            # Cleanup
            _sync_status.clear()

    def test_sync_status_structure(self):
        """Test the expected sync status structure."""
        from octosphere.app import _sync_status, _sync_lock
        
        # Set up a sample status
        with _sync_lock:
            _sync_status["test-orcid"] = {
                "status": "syncing",
                "bsky_handle": "test.bsky.social",
            }
        
        with _sync_lock:
            status = _sync_status.get("test-orcid")
            assert status is not None
            assert status["status"] == "syncing"
            assert status["bsky_handle"] == "test.bsky.social"
            # Cleanup
            _sync_status.clear()

    def test_sync_complete_status_includes_results(self):
        """Test that complete status includes results list."""
        from octosphere.app import _sync_status, _sync_lock
        
        results = [
            SyncResult(publication_id="pub-1", version_id="v1", uri="at://did/nsid/rkey1", cid="cid1"),
            SyncResult(publication_id="pub-2", version_id="v2", uri="at://did/nsid/rkey2", cid="cid2"),
        ]
        
        with _sync_lock:
            _sync_status["test-orcid"] = {
                "status": "complete",
                "results": results,
                "bsky_handle": "test.bsky.social",
            }
        
        with _sync_lock:
            status = _sync_status.get("test-orcid")
            assert status["status"] == "complete"
            assert len(status["results"]) == 2
            assert status["results"][0].publication_id == "pub-1"
            # Cleanup
            _sync_status.clear()

    def test_sync_error_status_includes_error_message(self):
        """Test that error status includes error message."""
        from octosphere.app import _sync_status, _sync_lock
        
        with _sync_lock:
            _sync_status["test-orcid"] = {
                "status": "error",
                "error": "Connection failed",
                "bsky_handle": "test.bsky.social",
            }
        
        with _sync_lock:
            status = _sync_status.get("test-orcid")
            assert status["status"] == "error"
            assert status["error"] == "Connection failed"
            # Cleanup
            _sync_status.clear()


class TestRunSyncInBackground:
    """Test the background sync function."""
    
    @patch('octosphere.app._octopus_client')
    @patch('octosphere.app._atproto_client')
    @patch('octosphere.app.sync_publications')
    @patch('octosphere.app.synced_publications')
    def test_updates_status_to_complete_on_success(
        self, mock_synced_pubs, mock_sync_pubs, mock_atproto, mock_octopus
    ):
        """Test that successful sync updates status to complete."""
        from octosphere.app import _run_sync_in_background, _sync_status, _sync_lock
        
        # Setup mocks
        mock_atproto_instance = MagicMock()
        mock_atproto.return_value = mock_atproto_instance
        mock_octopus_instance = MagicMock()
        mock_octopus.return_value = mock_octopus_instance
        
        results = [
            SyncResult(publication_id="pub-1", version_id="v1", uri="at://did/nsid/rkey1", cid="cid1"),
        ]
        mock_sync_pubs.return_value = results
        mock_synced_pubs.insert = MagicMock()
        
        # Clear status
        with _sync_lock:
            _sync_status.clear()
        
        # Run the function
        _run_sync_in_background(
            orcid="test-orcid",
            octopus_user_id="octopus-123",
            bsky_handle="test.bsky.social",
            bsky_password="test-password",
            already_synced=set(),
        )
        
        # Check status was updated
        with _sync_lock:
            status = _sync_status.get("test-orcid")
            assert status is not None
            assert status["status"] == "complete"
            assert len(status["results"]) == 1
            # Cleanup
            _sync_status.clear()

    @patch('octosphere.app._octopus_client')
    @patch('octosphere.app._atproto_client')
    @patch('octosphere.app.sync_publications')
    def test_updates_status_to_error_on_failure(
        self, mock_sync_pubs, mock_atproto, mock_octopus
    ):
        """Test that failed sync updates status to error."""
        from octosphere.app import _run_sync_in_background, _sync_status, _sync_lock
        
        # Setup mocks
        mock_atproto_instance = MagicMock()
        mock_atproto.return_value = mock_atproto_instance
        mock_octopus_instance = MagicMock()
        mock_octopus.return_value = mock_octopus_instance
        
        # Make sync_publications raise an error
        mock_sync_pubs.side_effect = RuntimeError("Network error")
        
        # Clear status
        with _sync_lock:
            _sync_status.clear()
        
        # Run the function
        _run_sync_in_background(
            orcid="test-orcid",
            octopus_user_id="octopus-123",
            bsky_handle="test.bsky.social",
            bsky_password="test-password",
            already_synced=set(),
        )
        
        # Check status was updated to error
        with _sync_lock:
            status = _sync_status.get("test-orcid")
            assert status is not None
            assert status["status"] == "error"
            assert "Network error" in status["error"]
            # Cleanup
            _sync_status.clear()


class TestSyncResultDataclass:
    """Test the SyncResult dataclass used in results."""
    
    def test_sync_result_attributes(self):
        """Test SyncResult has all required attributes."""
        result = SyncResult(
            publication_id="pub-123",
            version_id="ver-456",
            uri="at://did:plc:xxx/social.octosphere.publication/abc",
            cid="bafyrei...",
        )
        
        assert result.publication_id == "pub-123"
        assert result.version_id == "ver-456"
        assert result.uri == "at://did:plc:xxx/social.octosphere.publication/abc"
        assert result.cid == "bafyrei..."

    def test_sync_result_with_none_uri(self):
        """Test SyncResult can have None uri (for failed syncs)."""
        result = SyncResult(
            publication_id="pub-123",
            version_id="ver-456",
            uri=None,
            cid=None,
        )
        
        assert result.uri is None
        assert result.cid is None
