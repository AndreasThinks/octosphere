"""Database setup and encryption helpers for Octosphere."""
from __future__ import annotations

import os
from dataclasses import dataclass
from cryptography.fernet import Fernet
from fastlite import database

# Database setup - use env var for path, default to octosphere.db
db_path = os.getenv("DATABASE_PATH", "octosphere.db")
db = database(db_path)


# Define table schemas with explicit primary keys and table names
@dataclass
class User:
    orcid: str
    bsky_handle: str
    encrypted_app_password: str
    octopus_user_id: str = None
    last_sync: str = None
    active: int = 1
    created_at: str = None


@dataclass
class SyncedPublication:
    id: int = None
    orcid: str = None
    octopus_pub_id: str = None
    octopus_version_id: str = None
    at_uri: str = None
    synced_at: str = None


# Create tables with explicit pk and name parameters
# Note: transform=False because the tables already exist from SQL migrations with UNIQUE constraints
users = db.create(User, pk='orcid', name='users', transform=False, if_not_exists=True)
synced_publications = db.create(SyncedPublication, pk='id', name='synced_publications', transform=False, if_not_exists=True)


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
