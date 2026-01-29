"""Tests for database helpers."""
import os
import pytest
from cryptography.fernet import Fernet


class TestEncryption:
    @pytest.fixture(autouse=True)
    def setup_encryption_key(self, monkeypatch):
        """Set up a test encryption key."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("ENCRYPTION_KEY", key)

    def test_encrypt_decrypt_roundtrip(self):
        """Test password encryption and decryption."""
        from octosphere.database import encrypt_password, decrypt_password
        
        original = "my-super-secret-password"
        encrypted = encrypt_password(original)
        
        # Encrypted should be different from original
        assert encrypted != original
        
        # Decrypted should match original
        decrypted = decrypt_password(encrypted)
        assert decrypted == original

    def test_encrypted_value_is_fernet_token(self):
        """Test that encrypted value looks like a Fernet token."""
        from octosphere.database import encrypt_password
        
        encrypted = encrypt_password("test")
        
        # Fernet tokens are base64 encoded and fairly long
        assert len(encrypted) > 50
        assert "=" in encrypted  # Base64 padding

    def test_different_encryptions_produce_different_tokens(self):
        """Test that encrypting same value twice produces different tokens."""
        from octosphere.database import encrypt_password
        
        password = "same-password"
        encrypted1 = encrypt_password(password)
        encrypted2 = encrypt_password(password)
        
        # Fernet includes timestamp and IV, so same input = different output
        assert encrypted1 != encrypted2

    def test_missing_encryption_key_raises_error(self, monkeypatch):
        """Test that missing ENCRYPTION_KEY raises RuntimeError."""
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        
        # Need to reimport to get fresh state
        from octosphere.database import get_fernet
        
        with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
            get_fernet()
