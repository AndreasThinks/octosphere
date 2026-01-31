#!/usr/bin/env python3
"""Script to delete ALL social.octosphere.publication records for a user.

Usage:
    # Dry run - just list publications
    uv run python scripts/delete_all_publications.py --handle your.handle

    # Actually delete all publications
    uv run python scripts/delete_all_publications.py --handle your.handle --password your-app-password --delete
"""
import argparse

from octosphere.atproto.client import AtprotoClient
from octosphere.atproto.models import OCTOSPHERE_PUBLICATION_NSID


def main():
    parser = argparse.ArgumentParser(description="Delete ALL AT Protocol publication records")
    parser.add_argument("--handle", required=True, help="Your Bluesky handle (e.g., user.bsky.social)")
    parser.add_argument("--password", help="App password for authentication (required with --delete)")
    parser.add_argument("--delete", action="store_true", help="Actually delete all publications")
    args = parser.parse_args()
    
    client = AtprotoClient()
    
    # Need to login first to get the DID
    if args.password:
        print(f"🔐 Logging in as {args.handle}...")
        try:
            auth = client.login(args.handle, args.password)
            print(f"✅ Logged in as {auth.handle} ({auth.did})")
            did = auth.did
        except Exception as e:
            print(f"❌ Login failed: {e}")
            return
    else:
        # Resolve handle to DID for dry run
        print(f"🔍 Resolving handle {args.handle}...")
        did = client._resolver.handle.resolve(args.handle)
        if not did:
            print(f"❌ Could not resolve handle: {args.handle}")
            return
        print(f"✅ Resolved to DID: {did}")
        auth = None
    
    # List all records
    print(f"\n📋 Fetching all publication records...")
    records = client.list_records_public(did, limit=100)
    print(f"Found {len(records)} publications\n")
    
    if not records:
        print("✅ No publications to delete!")
        return
    
    # Show what we'll delete
    for i, record in enumerate(records, 1):
        value = record.get("value", {})
        title = value.get("title", "No title")[:50]
        uri = record.get("uri", "unknown")
        print(f"  {i}. {title}...")
        print(f"     URI: {uri}")
    
    if not args.delete:
        print(f"\n💡 This was a DRY RUN. Found {len(records)} publications.")
        print(f"   To delete ALL of them, run with:")
        print(f"   uv run python scripts/delete_all_publications.py --handle {args.handle} --password YOUR_APP_PASSWORD --delete")
        return
    
    if not auth:
        print("\n❌ Error: --delete requires --password")
        return
    
    # Confirm deletion
    print(f"\n⚠️  About to delete ALL {len(records)} publications!")
    confirm = input("Type 'yes' to confirm: ")
    if confirm.lower() != 'yes':
        print("Aborted.")
        return
    
    # Delete all records
    print(f"\n🗑️  Deleting {len(records)} publications...")
    deleted = 0
    errors = 0
    
    for record in records:
        uri = record.get("uri")
        try:
            client.delete_record(auth, uri)
            print(f"  ✅ Deleted: {uri}")
            deleted += 1
        except Exception as e:
            print(f"  ❌ Failed to delete {uri}: {e}")
            errors += 1
    
    print(f"\n✅ Done! Deleted {deleted} records, {errors} errors")
    
    # Also clear the synced_publications table for this user if needed
    print("\n📝 Note: You may also want to clear the synced_publications database table")
    print("   to allow re-syncing. This can be done via the Railway database console.")


if __name__ == "__main__":
    main()
