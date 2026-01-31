"""AT Proto client using the official atproto SDK.

Supports any PDS through identity resolution, not just bsky.social.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx
from atproto import Client, models
from atproto_identity.resolver import IdResolver

from octosphere.atproto.models import OCTOSPHERE_PUBLICATION_NSID


@dataclass
class AtprotoAuth:
    """Authentication state for an AT Protocol session."""
    did: str
    handle: str
    access_jwt: str
    refresh_jwt: str
    pds_endpoint: str


@dataclass
class CreateRecordResult:
    """Result of creating a record."""
    uri: str
    cid: str


class AtprotoClient:
    """AT Protocol client with support for any PDS.
    
    Uses identity resolution to discover the correct PDS for a user,
    rather than assuming bsky.social.
    
    Example:
        client = AtprotoClient()
        auth = client.login("alice.bsky.social", "app-password")
        result = client.create_publication_record(auth, record_dict)
    """
    
    def __init__(self, default_pds_url: Optional[str] = None):
        """Initialize the client.
        
        Args:
            default_pds_url: Fallback PDS URL if identity resolution fails.
                           Defaults to bsky.social.
        """
        self.default_pds_url = (default_pds_url or "https://bsky.social").rstrip("/")
        self._resolver = IdResolver()
        self._client: Optional[Client] = None
        self._auth: Optional[AtprotoAuth] = None
    
    def _resolve_pds_endpoint(self, handle: str) -> str:
        """Resolve the PDS endpoint for a handle.
        
        This allows supporting users on any PDS, not just bsky.social.
        
        Args:
            handle: User handle (e.g., "alice.bsky.social" or "bob.example.com")
            
        Returns:
            PDS endpoint URL
        """
        try:
            # Resolve handle -> DID
            did = self._resolver.handle.resolve(handle)
            if not did:
                return self.default_pds_url
            
            # Resolve DID -> DID Document -> PDS endpoint
            did_doc = self._resolver.did.resolve(did)
            if did_doc and did_doc.pds_endpoint:
                return did_doc.pds_endpoint
        except Exception:
            # Fall back to default if resolution fails
            pass
        
        return self.default_pds_url
    
    def login(self, handle: str, app_password: str) -> AtprotoAuth:
        """Authenticate with AT Protocol.
        
        Resolves the user's PDS automatically based on their handle.
        
        Args:
            handle: User handle or DID
            app_password: App password (not main account password)
            
        Returns:
            AtprotoAuth with session tokens
        """
        # Resolve the correct PDS for this user
        pds_endpoint = self._resolve_pds_endpoint(handle)
        
        # Create client for the user's PDS
        self._client = Client(base_url=pds_endpoint)
        
        # Login and get session
        profile = self._client.login(handle, app_password)
        
        # Extract session info
        session = self._client._session  # Access internal session for JWT tokens
        
        self._auth = AtprotoAuth(
            did=profile.did,
            handle=profile.handle,
            access_jwt=session.access_jwt,
            refresh_jwt=session.refresh_jwt,
            pds_endpoint=pds_endpoint,
        )
        
        return self._auth
    
    def create_session(self, handle: str, app_password: str) -> AtprotoAuth:
        """Alias for login() to maintain backward compatibility."""
        return self.login(handle, app_password)
    
    def _ensure_client(self, auth: AtprotoAuth) -> Client:
        """Ensure we have a client for the given auth session.
        
        If the auth is different from current session, create a new client.
        """
        if self._client and self._auth and self._auth.did == auth.did:
            return self._client
        
        # Create new client for this auth
        client = Client(base_url=auth.pds_endpoint)
        # Restore session using the login method with stored credentials would be ideal,
        # but for simplicity we'll create a fresh client
        # In production, you'd want to use session string export/import
        return client
    
    def create_publication_record(
        self,
        auth: AtprotoAuth,
        record: dict[str, Any],
        repo: Optional[str] = None,
        rkey: Optional[str] = None,
    ) -> CreateRecordResult:
        """Create an Octopus publication record in the user's repository.
        
        Args:
            auth: Authentication from login()
            record: Record data (camelCase keys as per lexicon)
            repo: Repository DID (defaults to auth.did)
            rkey: Record key (optional). If provided, uses a deterministic key
                  which makes the operation idempotent - re-creating with the same
                  rkey will update the existing record rather than create a duplicate.
            
        Returns:
            CreateRecordResult with URI and CID
        """
        if not self._client:
            raise RuntimeError("Not logged in. Call login() first.")
        
        # Use the SDK's typed method
        response = self._client.com.atproto.repo.create_record(
            models.ComAtprotoRepoCreateRecord.Data(
                repo=repo or auth.did,
                collection=OCTOSPHERE_PUBLICATION_NSID,
                record=record,
                rkey=rkey,  # Deterministic key for idempotency
            )
        )
        
        return CreateRecordResult(
            uri=response.uri,
            cid=response.cid,
        )
    
    def delete_record(
        self,
        auth: AtprotoAuth,
        uri: str,
    ) -> None:
        """Delete a record from the user's repository.
        
        Args:
            auth: Authentication from login()
            uri: AT URI of the record to delete (format: at://{did}/{collection}/{rkey})
        """
        if not self._client:
            raise RuntimeError("Not logged in. Call login() first.")
        
        # Parse AT URI manually: at://{did}/{collection}/{rkey}
        if not uri.startswith("at://"):
            raise ValueError(f"Invalid AT URI: {uri}")
        
        parts = uri[5:].split("/")  # Remove "at://" prefix and split
        if len(parts) < 3:
            raise ValueError(f"Invalid AT URI format: {uri}")
        
        repo = parts[0]  # DID
        collection = parts[1]  # Collection NSID
        rkey = parts[2]  # Record key
        
        self._client.com.atproto.repo.delete_record(
            models.ComAtprotoRepoDeleteRecord.Data(
                repo=repo,
                collection=collection,
                rkey=rkey,
            )
        )
    
    def list_records(
        self,
        did: str,
        collection: str = OCTOSPHERE_PUBLICATION_NSID,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List records in a repository collection.
        
        Args:
            did: Repository DID
            collection: Collection NSID (defaults to social.octosphere.publication)
            limit: Maximum records to return
            
        Returns:
            List of record dicts
        """
        if not self._client:
            raise RuntimeError("Not logged in. Call login() first.")
        
        response = self._client.com.atproto.repo.list_records(
            models.ComAtprotoRepoListRecords.Params(
                repo=did,
                collection=collection,
                limit=limit,
            )
        )
        
        return [
            {"uri": r.uri, "cid": r.cid, "value": r.value}
            for r in response.records
        ]
    
    def list_records_public(
        self,
        did: str,
        collection: str = OCTOSPHERE_PUBLICATION_NSID,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List records in a repository collection without authentication.
        
        This method makes unauthenticated requests directly to the user's PDS,
        which is allowed by the AT Protocol for public records.
        
        Args:
            did: Repository DID (e.g., "did:plc:...")
            collection: Collection NSID (defaults to social.octosphere.publication)
            limit: Maximum records to return
            
        Returns:
            List of record dicts with uri, cid, and value
        """
        # Resolve DID to find their PDS endpoint
        try:
            did_doc = self._resolver.did.resolve(did)
            if did_doc and did_doc.pds_endpoint:
                pds_url = did_doc.pds_endpoint.rstrip("/")
            else:
                pds_url = self.default_pds_url
        except Exception:
            pds_url = self.default_pds_url
        
        # Make unauthenticated request to com.atproto.repo.listRecords
        url = f"{pds_url}/xrpc/com.atproto.repo.listRecords"
        params = {
            "repo": did,
            "collection": collection,
            "limit": limit,
        }
        
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                
                return [
                    {"uri": r["uri"], "cid": r["cid"], "value": r["value"]}
                    for r in data.get("records", [])
                ]
        except Exception as e:
            # Log error but return empty list rather than failing
            print(f"Error listing records for {did}: {e}")
            return []
