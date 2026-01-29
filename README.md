# Octosphere (Octopus → AT Proto bridge)

This proof-of-concept service syncs **Octopus LIVE publications** to **AT Proto/Bluesky** using ORCID OAuth and AT Proto app passwords.

## Contents

- `app.py` FastHTML UI with ORCID login and “sync all live publications” flow.
- `cli.py` CLI for batch syncs.
- `lexicon/com.octopus.publication.json` AT Proto lexicon.

## Environment

Set these environment variables before running the UI or CLI:

```bash
export OCTOPUS_API_URL="https://api.example.org"
export OCTOPUS_WEB_URL="https://octopus.example.org"
export ORCID_CLIENT_ID="..."
export ORCID_CLIENT_SECRET="..."
export ORCID_REDIRECT_URI="http://localhost:5001/callback"
export ORCID_BASE_URL="https://orcid.org" # optional
export ORCID_TOKEN_URL="https://orcid.org/oauth/token" # optional
export ORCID_SCOPE="/authenticate" # optional
export ATPROTO_PDS_URL="https://bsky.social" # optional
export OCTOSPHERE_SESSION_SECRET="replace-me" # optional but recommended
```

## FastHTML UI

```bash
uv run --project octosphere uvicorn octosphere.app:app --port 5001
# visit http://localhost:5001
```

Flow:

1. Login with ORCID (OAuth).
2. Enter your Bluesky handle and app password.
3. Click “Sync all live publications”.

## CLI

```bash
uv run --project octosphere python -m octosphere.cli \
  --orcid 0000-0002-1825-0097 \
  --handle alice.bsky.social \
  --app-password xxxx-xxxx-xxxx-xxxx \
  --octopus-token "<optional api token>"
```

FastHTML is installed from the latest GitHub main branch (package name `python-fasthtml`) since it's not published to PyPI yet.

## Notes

- The bridge uses Octopus API `GET /users/:id/publications` and individual publication versions to build AT Proto records.
- Records are created under `com.octopus.publication` with fields for linked publications, peer review relationships, raw HTML, and plain text/citations.
- This is a PoC. For production, add token refresh, rate limiting, and pagination support.
```