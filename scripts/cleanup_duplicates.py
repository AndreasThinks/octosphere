#!/usr/bin/env python3
"""Script to identify and clean up duplicate AT Protocol records.

This script:
1. Lists all social.octosphere.publication records for a user
2. Groups them by publication_id to identify duplicates
3. Optionally deletes duplicates, keeping only one per publication

Usage:
    # Dry run - just list duplicates
    uv run python scripts/cleanup_duplicates.py --did did:plc:your-did-here

    # Actually delete duplicates (requires auth)
    uv run python scripts/cleanup_duplicates.py --did did:plc:your-did-here --delete --handle your.handle --password your-app-password
"""
import argparse
from collections import defaultdict

from octosphere.atproto.client import AtprotoClient
from octosphere.atproto.models import OCTOSPHERE_PUBLICATION_NSID


def list_and_group_records(client: AtprotoClient, did: str) -> dict[str, list[dict]]:
    """List all records and group them by publication_id."""
    print(f"\nFetching records for {did}...")
    records = client.list_records_public(did, limit=100)
    print(f"Found {len(records)} total records\n")
    
    # Group by octopusId (the publication ID field in our records)
    by_pub_id = defaultdict(list)
    for record in records:
        value = record.get("value", {})
        pub_id = value.get("octopusId", "unknown")
        by_pub_id[pub_id].append(record)
    
    return dict(by_pub_id)


def identify_duplicates(grouped: dict[str, list[dict]]) -> list[tuple[str, list[dict]]]:
    """Identify groups that have duplicates (more than one record)."""
    duplicates = []
    for pub_id, records in grouped.items():
        if len(records) > 1:
            duplicates.append((pub_id, records))
    return duplicates


def select_records_to_delete(pub_id: str, records: list[dict]) -> list[dict]:
    """Select which records to delete, keeping the best one.
    
    Prefers to keep:
    1. Records with deterministic rkey (octopus-{pub_id})
    2. If no deterministic rkey, keeps the first one
    """
    deterministic_rkey = f"octopus-{pub_id}"
    
    # Check if any record has the deterministic rkey
    to_keep = None
    for record in records:
        uri = record.get("uri", "")
        rkey = uri.split("/")[-1] if "/" in uri else ""
        if rkey == deterministic_rkey:
            to_keep = record
            break
    
    # If no deterministic rkey found, keep the first one
    if to_keep is None:
        to_keep = records[0]
    
    # Return all records except the one to keep
    return [r for r in records if r["uri"] != to_keep["uri"]]


def print_record_details(record: dict, indent: str = "  "):
    """Print details of a record."""
    uri = record.get("uri", "unknown")
    value = record.get("value", {})
    title = value.get("title", "No title")
    rkey = uri.split("/")[-1] if "/" in uri else "unknown"
    print(f"{indent}URI: {uri}")
    print(f"{indent}rkey: {rkey}")
    print(f"{indent}Title: {title}")


def main():
    parser = argparse.ArgumentParser(description="Find and clean up duplicate AT Protocol records")
    parser.add_argument("--did", required=True, help="DID of the user (e.g., did:plc:...)")
    parser.add_argument("--delete", action="store_true", help="Actually delete duplicates (requires --handle and --password)")
    parser.add_argument("--handle", help="Handle for authentication (required with --delete)")
    parser.add_argument("--password", help="App password for authentication (required with --delete)")
    args = parser.parse_args()
    
    client = AtprotoClient()
    
    # List and group records
    grouped = list_and_group_records(client, args.did)
    
    # Find duplicates
    duplicates = identify_duplicates(grouped)
    
    if not duplicates:
        print("✅ No duplicates found!")
        return
    
    print(f"⚠️  Found {len(duplicates)} publications with duplicates:\n")
    
    total_to_delete = 0
    all_records_to_delete = []
    
    for pub_id, records in duplicates:
        print(f"Publication ID: {pub_id}")
        print(f"  Has {len(records)} copies:")
        
        to_delete = select_records_to_delete(pub_id, records)
        to_keep = [r for r in records if r not in to_delete][0]
        
        print(f"\n  Will KEEP (has deterministic rkey or is first):")
        print_record_details(to_keep, "    ")
        
        print(f"\n  Will DELETE ({len(to_delete)} record(s)):")
        for record in to_delete:
            print_record_details(record, "    ")
            all_records_to_delete.append(record)
        
        total_to_delete += len(to_delete)
        print()
    
    print(f"\nSummary: {total_to_delete} records to delete across {len(duplicates)} publications")
    
    if not args.delete:
        print("\n💡 This was a DRY RUN. To actually delete duplicates, run with:")
        print(f"   uv run python scripts/cleanup_duplicates.py --did {args.did} --delete --handle YOUR_HANDLE --password YOUR_APP_PASSWORD")
        return
    
    # Verify auth params
    if not args.handle or not args.password:
        print("\n❌ Error: --delete requires --handle and --password")
        return
    
    # Login and delete
    print(f"\n🔐 Logging in as {args.handle}...")
    try:
        auth = client.login(args.handle, args.password)
        print(f"✅ Logged in as {auth.handle} ({auth.did})")
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return
    
    # Verify DID matches
    if auth.did != args.did:
        print(f"❌ Error: Logged in DID ({auth.did}) doesn't match provided DID ({args.did})")
        return
    
    # Delete records
    print(f"\n🗑️  Deleting {len(all_records_to_delete)} duplicate records...")
    deleted = 0
    errors = 0
    
    for record in all_records_to_delete:
        uri = record.get("uri")
        try:
            client.delete_record(auth, uri)
            print(f"  ✅ Deleted: {uri}")
            deleted += 1
        except Exception as e:
            print(f"  ❌ Failed to delete {uri}: {e}")
            errors += 1
    
    print(f"\n✅ Done! Deleted {deleted} records, {errors} errors")


if __name__ == "__main__":
    main()
