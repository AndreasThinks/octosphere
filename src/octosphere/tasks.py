"""Background sync tasks for Octosphere."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from octosphere.database import decrypt_password, users, synced_publications
from octosphere.atproto.client import AtprotoClient
from octosphere.bridge import sync_publications
from octosphere.octopus.client import OctopusClient


def get_sync_interval_days() -> int:
    """Get sync interval from env var, default 7 days."""
    return int(os.getenv("SYNC_INTERVAL_DAYS", "7"))


def task_sync_user(orcid: str) -> None:
    """Sync publications for a single user (runs as background task)."""
    user = users[orcid]
    if not user or not user.get("active"):
        return
    
    # Need octopus_user_id to fetch publications
    octopus_user_id = user.get("octopus_user_id")
    if not octopus_user_id:
        print(f"No octopus_user_id for {orcid}, skipping sync")
        return
    
    try:
        password = decrypt_password(user["encrypted_app_password"])
        
        octopus = OctopusClient(
            api_url=os.getenv("OCTOPUS_API_URL", ""),
            web_url=os.getenv("OCTOPUS_WEB_URL", ""),
            access_token=None,  # Public API doesn't need auth
        )
        atproto = AtprotoClient(os.getenv("ATPROTO_PDS_URL", "https://bsky.social"))
        auth = atproto.create_session(user["bsky_handle"], password)
        
        # Use octopus_user_id (internal ID) not orcid
        results = sync_publications(octopus, atproto, auth, octopus_user_id)
        
        # Record synced publications
        for r in results:
            synced_publications.insert(
                orcid=orcid,
                octopus_pub_id=r.publication_id,
                octopus_version_id=r.version_id,
                at_uri=r.uri,
            )
        
        # Update last sync time
        users.update({"orcid": orcid, "last_sync": datetime.utcnow().isoformat()})
        
    except Exception as e:
        # Log error but don't crash - this is a background task
        print(f"Sync failed for {orcid}: {e}")


def get_users_needing_sync() -> list[dict]:
    """Get users who need syncing based on interval."""
    interval = get_sync_interval_days()
    cutoff = (datetime.utcnow() - timedelta(days=interval)).isoformat()
    
    return [
        u for u in users()
        if u.get("active") and (not u.get("last_sync") or u["last_sync"] < cutoff)
    ]
