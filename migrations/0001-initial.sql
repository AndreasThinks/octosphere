-- Initial schema for Octosphere
CREATE TABLE IF NOT EXISTS users (
    orcid TEXT PRIMARY KEY,
    bsky_handle TEXT NOT NULL,
    encrypted_app_password TEXT NOT NULL,
    last_sync TEXT,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS synced_publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    orcid TEXT NOT NULL,
    octopus_pub_id TEXT NOT NULL,
    octopus_version_id TEXT NOT NULL,
    at_uri TEXT NOT NULL,
    synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (orcid) REFERENCES users(orcid),
    UNIQUE(orcid, octopus_pub_id, octopus_version_id)
);

CREATE INDEX IF NOT EXISTS idx_synced_publications_orcid ON synced_publications(orcid);
