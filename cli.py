"""CLI for syncing Octopus publications to AT Proto."""
from __future__ import annotations

import argparse
import sys

from octosphere.atproto.client import AtprotoClient
from octosphere.bridge import sync_publications
from octosphere.octopus.client import OctopusClient
from octosphere.settings import Settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Octopus LIVE publications to AT Proto"
    )
    parser.add_argument("--orcid", required=True, help="ORCID of the Octopus user")
    parser.add_argument("--octopus-token", help="Octopus API access token")
    parser.add_argument("--handle", required=True, help="Bluesky handle")
    parser.add_argument("--app-password", required=True, help="AT Proto app password")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = Settings.from_env()
    octopus = OctopusClient(
        api_url=settings.octopus_api_url,
        web_url=settings.octopus_web_url,
        access_token=args.octopus_token,
    )
    atproto = AtprotoClient(settings.atproto_pds_url)
    auth = atproto.create_session(args.handle, args.app_password)
    results = sync_publications(octopus, atproto, auth, args.orcid)
    print(f"Created {len(results)} records")
    for result in results:
        print(
            f"{result.publication_id} (version {result.version_id}) -> {result.uri}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
