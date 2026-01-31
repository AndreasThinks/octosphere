#!/usr/bin/env python3
"""Publish the Octosphere lexicon schema to an atproto repository.

Usage:
    python scripts/publish_lexicon.py <handle> <app_password>

Example:
    python scripts/publish_lexicon.py andreasthinks.me xxxx-xxxx-xxxx-xxxx
"""
import json
import sys
from pathlib import Path

import httpx


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    
    handle = sys.argv[1]
    app_password = sys.argv[2]
    
    # Load the lexicon JSON
    lexicon_path = Path(__file__).parent.parent / "lexicon" / "social.octosphere.publication.json"
    with open(lexicon_path) as f:
        lexicon_data = json.load(f)
    
    # Add $type field required for atproto records
    lexicon_record = {
        "$type": "com.atproto.lexicon.schema",
        **lexicon_data
    }
    
    pds_url = "https://bsky.social"
    
    # Create session (authenticate)
    print(f"Authenticating as {handle}...")
    response = httpx.post(
        f"{pds_url}/xrpc/com.atproto.server.createSession",
        json={"identifier": handle, "password": app_password},
    )
    
    if response.status_code != 200:
        print(f"Authentication failed: {response.text}")
        sys.exit(1)
    
    session = response.json()
    did = session["did"]
    access_jwt = session["accessJwt"]
    
    print(f"Authenticated! DID: {did}")
    
    # Check if record already exists
    print("Checking for existing lexicon record...")
    check_response = httpx.get(
        f"{pds_url}/xrpc/com.atproto.repo.getRecord",
        params={
            "repo": did,
            "collection": "com.atproto.lexicon.schema",
            "rkey": "social.octosphere.publication",
        },
        headers={"Authorization": f"Bearer {access_jwt}"},
    )
    
    if check_response.status_code == 200:
        print("Record already exists! Updating...")
        existing = check_response.json()
        # Delete and recreate (or use putRecord)
        response = httpx.post(
            f"{pds_url}/xrpc/com.atproto.repo.putRecord",
            json={
                "repo": did,
                "collection": "com.atproto.lexicon.schema",
                "rkey": "social.octosphere.publication",
                "record": lexicon_record,
            },
            headers={"Authorization": f"Bearer {access_jwt}"},
        )
    else:
        print("Creating new lexicon record...")
        response = httpx.post(
            f"{pds_url}/xrpc/com.atproto.repo.createRecord",
            json={
                "repo": did,
                "collection": "com.atproto.lexicon.schema",
                "rkey": "social.octosphere.publication",
                "record": lexicon_record,
            },
            headers={"Authorization": f"Bearer {access_jwt}"},
        )
    
    if response.status_code in (200, 201):
        result = response.json()
        print(f"✅ Lexicon published successfully!")
        print(f"   URI: {result.get('uri')}")
        print(f"   CID: {result.get('cid')}")
        print()
        print("You can view it at:")
        print(f"   https://pdsls.dev/at://{did}/com.atproto.lexicon.schema/social.octosphere.publication")
    else:
        print(f"❌ Failed to publish lexicon: {response.status_code}")
        print(response.text)
        sys.exit(1)


if __name__ == "__main__":
    main()
