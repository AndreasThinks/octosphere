"""Tests for background sync tasks."""
import logging
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta


@pytest.fixture(autouse=True)
def enable_log_capture():
    """Enable log propagation for caplog to capture logs during tests."""
    logger = logging.getLogger("octosphere")
    original_propagate = logger.propagate
    logger.propagate = True
    yield
    logger.propagate = original_propagate


class TestGetSyncIntervalDays:
    def test_default_interval(self, monkeypatch):
        monkeypatch.delenv("SYNC_INTERVAL_DAYS", raising=False)
        from octosphere.tasks import get_sync_interval_days
        assert get_sync_interval_days() == 7

    def test_custom_interval(self, monkeypatch):
        monkeypatch.setenv("SYNC_INTERVAL_DAYS", "14")
        from octosphere.tasks import get_sync_interval_days
        assert get_sync_interval_days() == 14


class TestGetUsersNeedingSync:
    @pytest.fixture
    def mock_users_table(self):
        """Create a mock users table."""
        users_data = []
        
        def users_callable():
            return users_data
        
        return users_callable, users_data

    def test_returns_users_with_no_last_sync(self, mock_users_table, monkeypatch):
        users_callable, users_data = mock_users_table
        users_data.extend([
            {"orcid": "0000-0001", "active": True, "last_sync": None},
            {"orcid": "0000-0002", "active": True, "last_sync": "2024-01-01T00:00:00"},
        ])
        
        with patch("octosphere.tasks.users", users_callable):
            from octosphere.tasks import get_users_needing_sync
            result = get_users_needing_sync()
        
        # User with no last_sync should need sync
        orcids = [u["orcid"] for u in result]
        assert "0000-0001" in orcids

    def test_returns_users_past_interval(self, mock_users_table, monkeypatch):
        users_callable, users_data = mock_users_table
        old_date = (datetime.utcnow() - timedelta(days=10)).isoformat()
        recent_date = (datetime.utcnow() - timedelta(days=1)).isoformat()
        
        users_data.extend([
            {"orcid": "0000-0001", "active": True, "last_sync": old_date},
            {"orcid": "0000-0002", "active": True, "last_sync": recent_date},
        ])
        
        monkeypatch.setenv("SYNC_INTERVAL_DAYS", "7")
        
        with patch("octosphere.tasks.users", users_callable):
            from octosphere.tasks import get_users_needing_sync
            result = get_users_needing_sync()
        
        # Only user with old last_sync should need sync
        orcids = [u["orcid"] for u in result]
        assert "0000-0001" in orcids
        assert "0000-0002" not in orcids

    def test_excludes_inactive_users(self, mock_users_table, monkeypatch):
        users_callable, users_data = mock_users_table
        users_data.extend([
            {"orcid": "0000-0001", "active": False, "last_sync": None},
            {"orcid": "0000-0002", "active": True, "last_sync": None},
        ])
        
        with patch("octosphere.tasks.users", users_callable):
            from octosphere.tasks import get_users_needing_sync
            result = get_users_needing_sync()
        
        orcids = [u["orcid"] for u in result]
        assert "0000-0001" not in orcids
        assert "0000-0002" in orcids


class TestTaskSyncUser:
    @pytest.fixture
    def mock_user(self):
        return {
            "orcid": "0000-0001-2345-6789",
            "bsky_handle": "test.bsky.social",
            "encrypted_app_password": "encrypted_password",
            "octopus_user_id": "cl5smny4a000009ieqml45bhz",
            "active": True,
        }

    def test_skips_inactive_user(self, mock_user):
        mock_user["active"] = False
        
        with patch("octosphere.tasks.users") as mock_users:
            mock_users.__getitem__.return_value = mock_user
            
            from octosphere.tasks import task_sync_user
            task_sync_user("0000-0001-2345-6789")
            
            # Should exit early, no further calls made

    def test_skips_user_without_octopus_id(self, mock_user, caplog):
        mock_user["octopus_user_id"] = None

        with patch("octosphere.tasks.users") as mock_users:
            mock_users.__getitem__.return_value = mock_user

            with caplog.at_level(logging.WARNING):
                from octosphere.tasks import task_sync_user
                task_sync_user("0000-0001-2345-6789")

            assert "No octopus_user_id" in caplog.text

    @patch("octosphere.tasks.sync_publications")
    @patch("octosphere.tasks.AtprotoClient")
    @patch("octosphere.tasks.OctopusClient")
    @patch("octosphere.tasks.decrypt_password")
    @patch("octosphere.tasks.synced_publications")
    def test_syncs_publications_successfully(
        self,
        mock_synced_pubs,
        mock_decrypt,
        mock_octopus_class,
        mock_atproto_class,
        mock_sync,
        mock_user,
        monkeypatch,
    ):
        mock_decrypt.return_value = "decrypted_password"
        
        mock_octopus = MagicMock()
        mock_octopus_class.return_value = mock_octopus
        
        mock_atproto = MagicMock()
        mock_atproto_class.return_value = mock_atproto
        mock_auth = MagicMock()
        mock_atproto.create_session.return_value = mock_auth
        
        mock_result = MagicMock()
        mock_result.publication_id = "pub-1"
        mock_result.version_id = "ver-1"
        mock_result.uri = "at://did:plc:test/collection/rkey"
        mock_sync.return_value = [mock_result]
        
        monkeypatch.setenv("OCTOPUS_API_URL", "https://api.octopus.ac")
        monkeypatch.setenv("OCTOPUS_WEB_URL", "https://www.octopus.ac")
        monkeypatch.setenv("ATPROTO_PDS_URL", "https://bsky.social")
        
        with patch("octosphere.tasks.users") as mock_users:
            mock_users.__getitem__.return_value = mock_user
            mock_users.update = MagicMock()
            
            from octosphere.tasks import task_sync_user
            task_sync_user("0000-0001-2345-6789")
        
        # Verify sync was called
        mock_sync.assert_called_once()
        
        # Verify publications were recorded
        mock_synced_pubs.insert.assert_called_once()

    @patch("octosphere.tasks.decrypt_password")
    def test_handles_sync_errors_gracefully(self, mock_decrypt, mock_user, caplog, monkeypatch):
        mock_decrypt.side_effect = Exception("Decryption failed")

        monkeypatch.setenv("OCTOPUS_API_URL", "https://api.octopus.ac")
        monkeypatch.setenv("OCTOPUS_WEB_URL", "https://www.octopus.ac")

        with patch("octosphere.tasks.users") as mock_users:
            mock_users.__getitem__.return_value = mock_user

            with caplog.at_level(logging.ERROR):
                from octosphere.tasks import task_sync_user
                # Should not raise, just log error
                task_sync_user("0000-0001-2345-6789")

        assert "Sync failed" in caplog.text
        assert "Decryption failed" in caplog.text
