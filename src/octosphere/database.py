"""Database setup and encryption helpers for Octosphere."""
from __future__ import annotations

import os
from cryptography.fernet import Fernet
from fastlite import database

# Re-export NotFoundError for use in other modules
try:
    from fastlite import NotFoundError
except ImportError:
    # Fallback for older versions
    class NotFoundError(Exception):
        pass

# Database setup - use env var for path, default to octosphere.db
db_path = os.getenv("DATABASE_PATH", "octosphere.db")
db = database(db_path)

# SQL-first approach: Don't create tables here - let migrations handle schema.
# Tables are accessed AFTER migrations run via db.t.tablename
# This avoids conflicts with fastmigrate which needs to manage the _meta table.

# Lazy table accessors - these will be initialized after migrations run
_users = None
_synced_publications = None


def get_users_table():
    """Get the users table (lazy initialization after migrations)."""
    global _users
    if _users is None:
        _users = db.t.users
    return _users


def get_synced_publications_table():
    """Get the synced_publications table (lazy initialization after migrations)."""
    global _synced_publications
    if _synced_publications is None:
        _synced_publications = db.t.synced_publications
    return _synced_publications


# For backward compatibility, expose as callable properties
class LazyTable:
    """Lazy table accessor that initializes on first use."""
    def __init__(self, getter):
        self._getter = getter
        self._table = None
    
    def __getattr__(self, name):
        if self._table is None:
            self._table = self._getter()
        return getattr(self._table, name)
    
    def __getitem__(self, key):
        if self._table is None:
            self._table = self._getter()
        return self._table[key]
    
    def __call__(self, *args, **kwargs):
        if self._table is None:
            self._table = self._getter()
        return self._table(*args, **kwargs)
    
    def __iter__(self):
        if self._table is None:
            self._table = self._getter()
        return iter(self._table())


# Expose lazy table accessors that work like the original table objects
users = LazyTable(get_users_table)
synced_publications = LazyTable(get_synced_publications_table)


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
