"""Database setup and encryption helpers for Octosphere."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from cryptography.fernet import Fernet

# Re-export NotFoundError for use in other modules
try:
    from fastlite import NotFoundError
except ImportError:
    # Fallback for older versions
    class NotFoundError(Exception):
        pass

# Database setup - use env var for path, default to octosphere.db
db_path = os.getenv("DATABASE_PATH", "octosphere.db")
migrations_path = os.getenv("MIGRATIONS_PATH", "migrations")

# CRITICAL: Enroll existing databases BEFORE calling fastlite's database()
# This is because fastlite/fastmigrate validates the _meta table on connection
def _ensure_db_enrolled():
    """Ensure database has _meta table before fastlite connects.
    
    This must happen BEFORE fastlite.database() is called, because fastlite
    validates that the database is managed by fastmigrate on connection.
    """
    db_file = Path(db_path)
    if db_file.exists():
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='_meta'")
        has_meta = cursor.fetchone() is not None
        
        if not has_meta:
            # Count migrations to determine current version
            migrations_dir = Path(migrations_path)
            migration_files = sorted(migrations_dir.glob("*.sql")) if migrations_dir.exists() else []
            current_version = len(migration_files)
            
            print(f"[Octosphere] Pre-enrolling existing database at {db_path} (version {current_version})")
            conn.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT OR REPLACE INTO _meta (key, value) VALUES ('version', ?)", (str(current_version),))
            conn.commit()
        
        conn.close()

# Run enrollment before importing fastlite database
_ensure_db_enrolled()

# NOW it's safe to import and connect via fastlite
from fastlite import database, Table
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
        # Use Table constructor with explicit pk to properly configure fastlite
        # This is needed because we use TEXT PRIMARY KEY in our SQL migration
        _users = Table(db, 'users', pk='orcid')
    return _users


def get_synced_publications_table():
    """Get the synced_publications table (lazy initialization after migrations)."""
    global _synced_publications
    if _synced_publications is None:
        # Use Table constructor with explicit pk to fix 'rowid' lookup errors
        _synced_publications = Table(db, 'synced_publications', pk='id')
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
