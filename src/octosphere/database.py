"""Database setup and encryption helpers for Octosphere."""
from __future__ import annotations

import os
from cryptography.fernet import Fernet
from fastlite import database

# Database setup - use env var for path, default to octosphere.db
db_path = os.getenv("DATABASE_PATH", "octosphere.db")
db = database(db_path)

# Tables will be created by fastmigrate migrations

def get_fernet() -> Fernet:
    """Get Fernet instance for encrypting/decrypting passwords."""
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("ENCRYPTION_KEY environment variable not set")
    return Fernet(key.encode())

def encrypt_password(password: str) -> str:
    """Encrypt a password for storage."""
    return get_fernet().encrypt(password.encode()).decode()

def decrypt_password(encrypted: str) -> str:
    """Decrypt a stored password."""
    return get_fernet().decrypt(encrypted.encode()).decode()
