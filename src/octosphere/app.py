"""FastHTML UI for Octosphere bridge."""
import asyncio
import json
import secrets
from pathlib import Path
from datetime import datetime

import websockets
from fasthtml.common import *
from starlette.responses import RedirectResponse, FileResponse
from starlette.background import BackgroundTask

from octosphere.atproto.client import AtprotoClient
from octosphere.atproto.models import OCTOSPHERE_PUBLICATION_NSID
from octosphere.bridge import sync_publications
from octosphere.database import db, encrypt_password, users, synced_publications
from octosphere.octopus.client import OctopusClient
from octosphere.orcid import OrcidClient, OrcidProfile
from octosphere.settings import Settings
from octosphere.tasks import task_sync_user


import os

# Get static/lexicon paths - try CWD first (works on Railway), then fall back to __file__-relative
def _find_path(name: str) -> Path:
    """Find a path, trying CWD first then __file__-relative."""
    # Try current working directory (works on Railway when run from project root)
    cwd_path = Path.cwd() / name
    if cwd_path.exists():
        return cwd_path
    # Try relative to __file__ (works in development)
    file_path = Path(__file__).parent.parent.parent / name
    if file_path.exists():
        return file_path
    # Fall back to CWD path even if it doesn't exist (will return 404)
    return cwd_path

STATIC_PATH = _find_path("static")
LEXICON_PATH = _find_path("lexicon")

settings: Settings | None
settings_error: str | None = None
try:
    settings = Settings.from_env()
except RuntimeError as exc:
    settings = None
    settings_error = str(exc)

app, rt = fast_app(
    title="Octosphere",
    secret_key=settings.session_secret if settings else None,
)


# Explicit static file serving with absolute path (works on Railway)
@rt("/static/{fname:path}")
def static_files(fname: str):
    fpath = STATIC_PATH / fname
    if fpath.exists():
        return FileResponse(fpath)
    return Response("Not found", status_code=404)


# Serve favicon at root for pdsls.dev and other tools that look for octosphere.social/favicon.ico
@rt("/favicon.ico")
def favicon():
    fpath = STATIC_PATH / "octosphere.ico"
    if fpath.exists():
        return FileResponse(fpath, media_type="image/x-icon")
    return Response("Not found", status_code=404)


# Serve lexicon schemas for discoverability (AT Protocol best practice)
@rt("/lexicon/{fname:path}")
def lexicon_files(fname: str):
    fpath = LEXICON_PATH / fname
    if fpath.exists():
        return FileResponse(fpath, media_type="application/json")
    return Response("Not found", status_code=404)


def _nav(profile: OrcidProfile | None = None):
    """Render navigation bar."""
    nav_items = [
        Li(A("Home", href="/")),
        Li(A("Feed", href="/feed")),
    ]
    if profile:
        nav_items.append(Li(A("Dashboard", href="/dashboard")))
        nav_items.append(Li(A("Sign out", href="/logout")))
    else:
        nav_items.append(Li(A("Sign in", href="/login")))
    
    return Nav(
        Ul(Li(A(
            Img(src="/static/octosphere.png", alt="Octosphere", style="height: 28px; vertical-align: middle; margin-right: 0.5rem;"),
            Strong("Octosphere"),
            href="/",
            style="display: flex; align-items: center;",
        ))),
        Ul(*nav_items),
        cls="container-fluid",
    )


def _page(title: str, *content, profile: OrcidProfile | None = None):
    """Wrap content in a standard page layout."""
    return (
        Title(f"{title} - Octosphere"),
        Favicon('/static/octosphere.ico', '/static/octosphere.ico'),
        Link(rel="stylesheet", href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css"),
        _nav(profile),
        Main(*content, cls="container"),
        Footer(
            P(
                A(I(cls="fa-brands fa-github"), href="https://github.com/AndreasThinks/octosphere", style="margin-right: 1rem;"),
                "Created by ",
                A("AndreasThinks", href="https://andreasthinks.me/"),
                " with help from some ✨vibes✨",
                style="font-size: 0.875rem; color: var(--pico-muted-color);",
            ),
            cls="container",
            style="margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--pico-muted-border-color); text-align: center;",
        ),
    )


# Run migrations on startup
def run_migrations():
    from fastmigrate.core import run_migrations as fm_migrate
    import os
    migrations_path = os.getenv("MIGRATIONS_PATH", "migrations")
    db_path = os.getenv("DATABASE_PATH", "octosphere.db")
    fm_migrate(db_path, migrations_path)

run_migrations()


def _orcid_client() -> OrcidClient:
    if settings is None:
        raise RuntimeError("Settings not configured")
    return OrcidClient(
        client_id=settings.orcid_client_id,
        client_secret=settings.orcid_client_secret,
        redirect_uri=settings.orcid_redirect_uri,
        base_url=settings.orcid_base_url,
        token_url=settings.orcid_token_url,
        scope=settings.orcid_scope,
    )


def _octopus_client(profile: OrcidProfile | None = None) -> OctopusClient:
    if settings is None:
        raise RuntimeError("Settings not configured")
    return OctopusClient(
        api_url=settings.octopus_api_url,
        web_url=settings.octopus_web_url,
        access_token=profile.access_token if profile else None,
    )


def _atproto_client() -> AtprotoClient:
    if settings is None:
        raise RuntimeError("Settings not configured")
    return AtprotoClient(settings.atproto_pds_url)


def _profile_from_session(sess) -> OrcidProfile | None:
    data = sess.get("orcid")
    if not data:
        return None
    return OrcidProfile(
        orcid=data.get("orcid", ""),
        access_token=data.get("access_token", ""),
        name=data.get("name"),
    )


def _require_login(sess) -> OrcidProfile | None:
    profile = _profile_from_session(sess)
    if profile and profile.access_token:
        return profile
    return None


def _status_panel(message: str, status: str = "info"):
    cls = {
        "info": "secondary",
        "success": "",
        "error": "contrast",
    }.get(status, "secondary")
    return Article(
        Header(H3("Status")),
        P(message),
        cls=cls,
    )


@rt("/")
def index(sess):
    """Homepage - explains what Octosphere is."""
    profile = _profile_from_session(sess)
    
    if settings_error:
        return _page(
            "Configuration Error",
            Article(
                Header(H3("Missing configuration")),
                Pre(settings_error),
                P("Set the required environment variables and restart."),
            ),
            profile=profile,
        )
    
    return _page(
        "Home",
        # Experimental banner
        Div(
            Strong("Experimental"), " — Exploring what distributed science on AT Protocol could look like.",
            style="background: var(--pico-secondary-background); padding: 0.5rem 1rem; border-radius: var(--pico-border-radius); text-align: center; margin-bottom: 1rem;",
        ),
        # Hero section
        Header(
            H1("Octosphere"),
            P(
                "Connecting open science with the social web",
                style="font-size: 1.25rem; color: var(--pico-muted-color);",
            ),
            style="text-align: center; padding: 2rem 0;",
        ),
        # What it does
        Section(
            H2("What is Octosphere?"),
            P(
                "Octosphere bridges the gap between academic publishing and the social web. "
                "It automatically syncs your research publications from ",
                A("Octopus", href="https://www.octopus.ac"),
                " to the ",
                A("AT Protocol", href="https://atproto.com"),
                " (the atmosphere) — an open, decentralized network for social apps like ",
                A("Bluesky", href="https://bsky.app"),
                "."
            ),
            P(
                "By sharing your work on the atmosphere, you can reach broader audiences, "
                "engage with the public, and increase the visibility of your research beyond "
                "traditional academic channels."
            ),
        ),
        # How it works
        Section(
            H2("How it works"),
            Ol(
                Li(
                    Strong("Sign in with ORCID"),
                    " — Authenticate using your researcher identifier.",
                ),
                Li(
                    Strong("Connect to the atmosphere"),
                    " — Sign in with your Bluesky account (or any AT Protocol app).",
                ),
                Li(
                    Strong("Link your Octopus profile"),
                    " — Connect your Octopus author page.",
                ),
                Li(
                    Strong("Sync your publications"),
                    " — Choose one-time sync or enable automatic syncing of future publications.",
                ),
            ),
        ),
        # CTA
        Section(
            A(
                "Get started" if not profile else "Go to Dashboard",
                href="/login" if not profile else "/dashboard",
                role="button",
                cls="contrast",
            ),
            style="text-align: center; padding: 2rem 0;",
        ),
        profile=profile,
    )


# Jetstream URL for subscribing to social.octosphere.publication records
JETSTREAM_URL = f"wss://jetstream2.us-east.bsky.network/subscribe?wantedCollections={OCTOSPHERE_PUBLICATION_NSID}"

# Get shutdown event for graceful WebSocket closing
shutdown_event = signal_shutdown()


def PublicationCard(record: dict, did: str, handle: str | None = None, timestamp: str | None = None):
    """Render a publication as a social media-style card."""
    title = record.get("title", "Untitled Publication")
    description = record.get("description", "")
    pub_type = record.get("publicationType", "")
    octopus_id = record.get("octopusId", "")
    octopus_url = record.get("octopusUrl", "")
    
    # Build peer review URL: https://www.octopus.ac/create?for={octopusId}&type=PEER_REVIEW
    peer_review_url = f"https://www.octopus.ac/create?for={octopus_id}&type=PEER_REVIEW" if octopus_id else None
    
    # Format timestamp for display
    time_display = ""
    if timestamp:
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            time_display = dt.strftime("%b %d, %Y at %H:%M")
        except Exception:
            time_display = timestamp
    
    # Display handle or truncated DID
    author_display = f"@{handle}" if handle else f"{did[:20]}..."
    
    return Article(
        # Header with author and timestamp
        Header(
            Div(
                Strong(author_display),
                Small(f" · {time_display}" if time_display else "", style="color: var(--pico-muted-color);"),
                style="display: flex; align-items: center; gap: 0.5rem;",
            ),
            Small(pub_type, style="color: var(--pico-primary);") if pub_type else None,
        ),
        # Main content
        H4(title, style="margin-bottom: 0.5rem;"),
        P(
            description[:300] + "..." if len(description) > 300 else description,
            style="color: var(--pico-muted-color); margin-bottom: 1rem;",
        ) if description else None,
        # Footer with action links
        Footer(
            Div(
                A(
                    I(cls="fa-solid fa-book-open", style="margin-right: 0.25rem;"),
                    "View on Octopus",
                    href=octopus_url,
                    target="_blank",
                    role="button",
                    cls="outline",
                    style="font-size: 0.875rem; padding: 0.25rem 0.75rem;",
                ) if octopus_url else None,
                A(
                    I(cls="fa-solid fa-comments", style="margin-right: 0.25rem;"),
                    "Post a Peer Review",
                    href=peer_review_url,
                    target="_blank",
                    role="button",
                    cls="contrast",
                    style="font-size: 0.875rem; padding: 0.25rem 0.75rem;",
                ) if peer_review_url else None,
                style="display: flex; gap: 0.5rem; flex-wrap: wrap;",
            ),
        ),
        style="margin-bottom: 1rem;",
    )


async def jetstream_consumer():
    """Async generator that consumes Jetstream and yields SSE messages."""
    while not shutdown_event.is_set():
        try:
            async with websockets.connect(JETSTREAM_URL) as ws:
                while not shutdown_event.is_set():
                    try:
                        # Wait for message with timeout to check shutdown
                        msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        data = json.loads(msg)
                        
                        # Jetstream message structure:
                        # {"did": "did:plc:...", "time_us": ..., "kind": "commit", 
                        #  "commit": {"operation": "create", "collection": "...", "rkey": "...", "record": {...}}}
                        
                        if data.get("kind") == "commit":
                            commit = data.get("commit", {})
                            if commit.get("operation") == "create" and commit.get("collection") == OCTOSPHERE_PUBLICATION_NSID:
                                record = commit.get("record", {})
                                did = data.get("did", "")
                                timestamp = record.get("createdAt") or datetime.utcnow().isoformat()
                                
                                # Render the publication card
                                card = PublicationCard(record, did, timestamp=timestamp)
                                yield sse_message(card)
                                
                    except asyncio.TimeoutError:
                        # Send keepalive comment to prevent connection timeout
                        continue
                    except websockets.ConnectionClosed:
                        break
                        
        except Exception as e:
            # Log error and retry after delay
            print(f"Jetstream connection error: {e}")
            await asyncio.sleep(5)


@rt("/feed/stream")
async def feed_stream():
    """SSE endpoint for live feed."""
    return EventStream(jetstream_consumer())


@rt("/feed")
def feed(sess):
    """Live feed page - real-time stream of research publications."""
    profile = _profile_from_session(sess)
    
    return (
        Title("Feed - Octosphere"),
        Favicon('/static/octosphere.ico', '/static/octosphere.ico'),
        Link(rel="stylesheet", href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css"),
        Script(src="https://unpkg.com/htmx-ext-sse@2.2.3/sse.js"),
        _nav(profile),
        Main(
            # Experimental banner
            Div(
                Strong("Experimental"), " — This feed shows publications synced as part of an AT Protocol experiment.",
                style="background: var(--pico-secondary-background); padding: 0.5rem 1rem; border-radius: var(--pico-border-radius); text-align: center; margin-bottom: 1rem;",
            ),
            Header(
                H1("Live Feed"),
                P(
                    "Real-time stream of research publications",
                    style="font-size: 1.25rem; color: var(--pico-muted-color);",
                ),
                style="text-align: center; padding: 2rem 0;",
            ),
            # SSE container - new publications appear at the top
            Div(
                P(
                    Span(aria_busy="true", style="margin-right: 0.5rem;"),
                    "Waiting for new publications...",
                    style="text-align: center; color: var(--pico-muted-color);",
                ),
                id="feed-container",
                hx_ext="sse",
                sse_connect="/feed/stream",
                hx_swap="afterbegin",
                sse_swap="message",
            ),
            cls="container",
        ),
        Footer(
            P(
                A(I(cls="fa-brands fa-github"), href="https://github.com/AndreasThinks/octosphere", style="margin-right: 1rem;"),
                "Created by ",
                A("AndreasThinks", href="https://andreasthinks.me/"),
                " with help from some ✨vibes✨",
                style="font-size: 0.875rem; color: var(--pico-muted-color);",
            ),
            cls="container",
            style="margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--pico-muted-border-color); text-align: center;",
        ),
    )


@rt("/dashboard")
def dashboard(sess):
    """Dashboard - sync panel for logged in users."""
    profile = _profile_from_session(sess)
    
    if not profile:
        return RedirectResponse(url="/login", status_code=303)
    
    return _page(
        "Dashboard",
        Header(
            H1("Dashboard"),
            P(
                f"Signed in as {profile.name or profile.orcid}",
                style="color: var(--pico-muted-color);",
            ),
            style="text-align: center; padding: 1rem 0;",
        ),
        Div(id="sync-panel", hx_get="/sync_panel", hx_trigger="load"),
        profile=profile,
    )


@rt
def login(sess, request):
    state = secrets.token_urlsafe(16)
    sess["orcid_state"] = state
    url = _orcid_client().auth_url(state)
    return RedirectResponse(url=url, status_code=303)


@rt
def callback(code: str | None = None, state: str | None = None, sess=None):
    if not code or not state or state != sess.get("orcid_state"):
        return _status_panel("Invalid ORCID callback state.", "error")
    profile = _orcid_client().exchange_code(code)
    sess["orcid"] = {
        "orcid": profile.orcid,
        "access_token": profile.access_token,
        "name": profile.name,
    }
    return RedirectResponse(url="/dashboard", status_code=303)


@rt
def logout(sess):
    sess.pop("orcid", None)
    sess.pop("orcid_state", None)
    sess.pop("octopus_user_id", None)
    sess.pop("bsky_handle", None)
    sess.pop("bsky_app_password", None)
    sess.pop("bsky_authenticated", None)
    return RedirectResponse(url="/", status_code=303)


@rt
def sync_panel(sess):
    profile = _profile_from_session(sess)
    if not profile:
        return _status_panel("Login with ORCID to continue.", "error")
    
    # Check if user already has auto-sync enabled
    existing = users[profile.orcid] if profile.orcid in [u["orcid"] for u in users()] else None
    
    if existing and existing.get("active"):
        pub_count = 0
        synced_count = 0
        if existing.get("octopus_user_id"):
            octopus = _octopus_client()
            try:
                publications = octopus.get_user_publications(existing["octopus_user_id"])
                pub_count = len(publications)
            except Exception:
                pass
            # Count already synced
            synced_count = len([s for s in synced_publications() if s.get("orcid") == profile.orcid])
        
        return Article(
            Header(H3("Auto-sync enabled")),
            P(f"Your publications are being synced to @{existing['bsky_handle']}"),
            P(f"Total publications: {pub_count}"),
            P(f"Already synced: {synced_count}"),
            P(f"Last sync: {existing.get('last_sync') or 'Never'}"),
            Form(
                Button("Disable auto-sync", type="submit", cls="secondary"),
                hx_post="/disable_sync",
                hx_target="#sync-panel",
                hx_swap="outerHTML",
            ),
            id="sync-panel",
        )
    
    # Step 1: Check if Bluesky is connected (stored in session)
    bsky_handle = sess.get("bsky_handle")
    bsky_authenticated = sess.get("bsky_authenticated")
    
    if not bsky_authenticated:
        # Step 1: Sign in with Bluesky/AT Proto first
        return Article(
            Header(H3("Step 1: Sign in with Bluesky")),
            P("First, connect your Bluesky account to sync your publications."),
            Form(
                Fieldset(
                    Label("Bluesky handle", Input(id="handle", placeholder="user.bsky.social", required=True)),
                    Label("App password", Input(id="app_password", type="password", required=True)),
                    Small("Create an app password at bsky.app Settings → App Passwords"),
                ),
                Button("Sign in with Bluesky", type="submit", cls="contrast"),
                Div(
                    Span("Connecting to Bluesky...", aria_busy="true"),
                    id="loading",
                    cls="htmx-indicator",
                    style="display:none;",
                ),
                hx_post="/validate_bluesky",
                hx_target="#sync-panel",
                hx_swap="outerHTML",
                hx_indicator="#loading",
            ),
            id="sync-panel",
        )
    
    # Step 2: Ask for Octopus author URL (only shown after Bluesky is connected)
    return Article(
        Header(H3("Step 2: Connect your Octopus profile")),
        P(f"Connected to Bluesky as @{bsky_handle}"),
        P("Now, let's find your Octopus publications."),
        Form(
            Fieldset(
                Label(
                    "Octopus author page URL",
                    Input(
                        id="octopus_url",
                        placeholder="https://www.octopus.ac/authors/your-id",
                        required=True,
                    ),
                ),
                Small(
                    "Find this at octopus.ac by clicking your profile. "
                    "Example: https://www.octopus.ac/authors/cl5smny4a000009ieqml45bhz"
                ),
            ),
            Button("Find my publications", type="submit", cls="contrast"),
            Div(
                Span("Looking up publications...", aria_busy="true"),
                id="loading",
                cls="htmx-indicator",
                style="display:none;",
            ),
            hx_post="/validate_octopus",
            hx_target="#sync-panel",
            hx_swap="outerHTML",
            hx_indicator="#loading",
        ),
        P(A("Disconnect Bluesky", href="/disconnect_bluesky", hx_get="/disconnect_bluesky", hx_target="#sync-panel", cls="secondary")),
        id="sync-panel",
    )


@rt
def validate_bluesky(handle: str, app_password: str, sess):
    """Step 1: Validate Bluesky credentials and store in session."""
    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID first.", "error")
    
    # Validate Bluesky credentials
    atproto = _atproto_client()
    try:
        auth = atproto.create_session(handle, app_password)
    except Exception as e:
        return _status_panel(f"Invalid Bluesky credentials: {e}", "error")
    
    # Store Bluesky connection in session
    sess["bsky_handle"] = handle
    sess["bsky_app_password"] = app_password
    sess["bsky_authenticated"] = True
    
    # Return the sync panel which will now show Step 2 (Octopus connection)
    return Article(
        Header(H3("Step 2: Connect your Octopus profile")),
        P(f"Connected to Bluesky as @{handle}"),
        P("Now, let's find your Octopus publications."),
        Form(
            Fieldset(
                Label(
                    "Octopus author page URL",
                    Input(
                        id="octopus_url",
                        placeholder="https://www.octopus.ac/authors/your-id",
                        required=True,
                    ),
                ),
                Small(
                    "Find this at octopus.ac by clicking your profile. "
                    "Example: https://www.octopus.ac/authors/cl5smny4a000009ieqml45bhz"
                ),
            ),
            Button("Find my publications", type="submit", cls="contrast"),
            Div(
                Span("Looking up publications...", aria_busy="true"),
                id="loading",
                cls="htmx-indicator",
                style="display:none;",
            ),
            hx_post="/validate_octopus",
            hx_target="#sync-panel",
            hx_swap="outerHTML",
            hx_indicator="#loading",
        ),
        P(A("Disconnect Bluesky", href="/disconnect_bluesky", hx_get="/disconnect_bluesky", hx_target="#sync-panel", cls="secondary")),
        id="sync-panel",
    )


@rt
def disconnect_bluesky(sess):
    """Disconnect Bluesky and return to Step 1."""
    sess.pop("bsky_handle", None)
    sess.pop("bsky_app_password", None)
    sess.pop("bsky_authenticated", None)
    
    # Return the Bluesky login form (Step 1)
    return Article(
        Header(H3("Step 1: Sign in with Bluesky")),
        P("First, connect your Bluesky account to sync your publications."),
        Form(
            Fieldset(
                Label("Bluesky handle", Input(id="handle", placeholder="user.bsky.social", required=True)),
                Label("App password", Input(id="app_password", type="password", required=True)),
                Small("Create an app password at bsky.app Settings → App Passwords"),
            ),
            Button("Sign in with Bluesky", type="submit", cls="contrast"),
            Div(
                Span("Connecting to Bluesky...", aria_busy="true"),
                id="loading",
                cls="htmx-indicator",
                style="display:none;",
            ),
            hx_post="/validate_bluesky",
            hx_target="#sync-panel",
            hx_swap="outerHTML",
            hx_indicator="#loading",
        ),
        id="sync-panel",
    )


@rt
def validate_octopus(octopus_url: str, sess):
    """Step 2 result: Validate Octopus URL and show publications with sync button."""
    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID first.", "error")
    
    # Check that Bluesky is connected
    bsky_handle = sess.get("bsky_handle")
    if not sess.get("bsky_authenticated"):
        return _status_panel("Please connect to Bluesky first.", "error")
    
    # Extract Octopus user ID from URL
    octopus_user_id = OctopusClient.extract_user_id_from_url(octopus_url)
    if not octopus_user_id:
        return _status_panel(
            "Invalid Octopus author URL. Should look like: "
            "https://www.octopus.ac/authors/cl5smny4a000009ieqml45bhz",
            "error"
        )
    
    # Verify the Octopus user exists and fetch publications
    octopus = _octopus_client()
    try:
        user_info = octopus.get_user_info(octopus_user_id)
        if not user_info:
            return _status_panel("Octopus user not found. Check your author URL.", "error")
    except Exception as e:
        return _status_panel(f"Could not verify Octopus profile: {e}", "error")
    
    # Fetch publications
    try:
        publications = octopus.get_user_publications(octopus_user_id)
        pub_count = len(publications)
    except Exception:
        publications = []
        pub_count = 0
    
    # Store octopus_user_id in session for next step
    sess["octopus_user_id"] = octopus_user_id
    
    # Build publication preview (show up to 5)
    pub_items = []
    for pub in publications[:5]:
        # API structure: pub.versions[0].title contains the title
        versions = pub.get("versions", [])
        if versions:
            title = versions[0].get("title") or "Untitled"
        else:
            title = pub.get("title") or "Untitled"
        pub_type = pub.get("type") or ""
        pub_items.append(Li(f"{pub_type}: {title[:60]}{'...' if len(title) > 60 else ''}"))
    
    if pub_count > 5:
        pub_items.append(Li(f"...and {pub_count - 5} more"))
    
    # Step 3: Show publications and sync button
    if pub_count == 0:
        # No publications - go straight to auto-sync setup
        return Article(
            Header(H3("Set up auto-sync")),
            P(f"Connected to @{bsky_handle}"),
            P(
                "You don't have any Octopus publications yet, but that's okay! "
                "Enable auto-sync now and we'll automatically publish your research "
                "to the atmosphere as soon as you publish on Octopus."
            ),
            Form(
                Button("Enable auto-sync", type="submit", cls="contrast", style="width: 100%;"),
                Input(type="hidden", name="action", value="auto_sync"),
                Div(
                    Span("Setting up auto-sync...", aria_busy="true"),
                    id="loading-sync",
                    cls="htmx-indicator",
                    style="display:none;",
                ),
                hx_post="/setup_sync",
                hx_target="#sync-panel",
                hx_swap="outerHTML",
                hx_indicator="#loading-sync",
            ),
            P(A("Back", href="/sync_panel", hx_get="/sync_panel", hx_target="#sync-panel"), style="margin-top: 1rem;"),
            id="sync-panel",
        )
    
    return Article(
        Header(H3(f"Found {pub_count} publications")),
        P(f"Ready to sync to @{bsky_handle}"),
        Ul(*pub_items),
        Hr(),
        H4("Step 3: Sync your publications"),
        P("Click below to sync your existing Octopus publications to the atmosphere."),
        Form(
            Button("Sync Now", type="submit", cls="contrast", style="width: 100%;"),
            Input(type="hidden", name="action", value="sync_once"),
            Div(
                P(
                    Span(aria_busy="true", style="margin-right: 0.5rem;"),
                    "Syncing your publications to the atmosphere...",
                    style="text-align: center; padding: 1rem 0;",
                ),
                P(
                    Small("This may take a moment depending on how many publications you have."),
                    style="text-align: center; color: var(--pico-muted-color);",
                ),
                id="loading-sync",
                cls="htmx-indicator",
                style="display:none;",
            ),
            hx_post="/setup_sync",
            hx_target="#sync-panel",
            hx_swap="outerHTML",
            hx_indicator="#loading-sync",
        ),
        P(A("Back", href="/sync_panel", hx_get="/sync_panel", hx_target="#sync-panel"), style="margin-top: 1rem;"),
        id="sync-panel",
    )


@rt
def setup_sync(action: str, sess, handle: str | None = None, app_password: str | None = None):
    """Handle both one-time sync and auto-sync setup."""
    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID first.", "error")
    
    octopus_user_id = sess.get("octopus_user_id")
    if not octopus_user_id:
        return _status_panel("Session expired. Please start over.", "error")
    
    # Get Bluesky credentials from session (or from form for backward compatibility)
    bsky_handle = handle or sess.get("bsky_handle")
    bsky_password = app_password or sess.get("bsky_app_password")
    
    if not bsky_handle or not bsky_password:
        return _status_panel("Bluesky credentials not found. Please start over.", "error")
    
    # Validate Bluesky credentials
    atproto = _atproto_client()
    try:
        auth = atproto.create_session(bsky_handle, bsky_password)
    except Exception as e:
        return _status_panel(f"Invalid Bluesky credentials: {e}", "error")
    
    # Get publication count
    octopus = _octopus_client()
    try:
        publications = octopus.get_user_publications(octopus_user_id)
        pub_count = len(publications)
    except Exception:
        pub_count = 0
    
    encrypted_pw = encrypt_password(bsky_password)
    
    if action == "auto_sync":
        # Store credentials for ongoing sync
        users.insert(
            orcid=profile.orcid,
            bsky_handle=bsky_handle,
            encrypted_app_password=encrypted_pw,
            octopus_user_id=octopus_user_id,
            active=1,
            pk="orcid",
        )
        
        if pub_count > 0:
            message = P(f"Syncing {pub_count} publications in the background...")
            background = BackgroundTask(task_sync_user, orcid=profile.orcid)
        else:
            message = P("We'll sync your publications when you publish on Octopus.")
            background = None
        
        return Response(
            content=Article(
                Header(H3("Auto-sync enabled")),
                P(f"Your publications will be synced to @{bsky_handle}"),
                message,
                P(A("Back to home", href="/")),
                id="sync-panel",
            ),
            background=background,
        )
    
    else:  # sync_once
        # Don't store credentials permanently, just sync now
        if pub_count == 0:
            return Article(
                Header(H3("Nothing to sync")),
                P("You don't have any publications on Octopus yet."),
                P("Come back after you've published!"),
                P(A("Back to home", href="/")),
                id="sync-panel",
            )
        
        # Sync publications synchronously and show results
        try:
            results = sync_publications(octopus, atproto, auth, octopus_user_id)
            
            # Record synced publications
            for r in results:
                synced_publications.insert(
                    orcid=profile.orcid,
                    octopus_pub_id=r.publication_id,
                    octopus_version_id=r.version_id,
                    at_uri=r.uri,
                )
            
            # Build results table
            rows = [
                Tr(
                    Td(r.publication_id[:12] + "..."),
                    Td(A("View on pdsls", href=f"https://pdsls.dev/{r.uri}" if r.uri else "#", target="_blank")),
                )
                for r in results[:10]
            ]
            
            # Step 4: Show success and prompt for auto-sync
            return Article(
                # Success header with checkmark
                Header(
                    H3("✅ Synced ", Strong(f"{len(results)}"), " publications!"),
                    style="text-align: center;",
                ),
                P(
                    f"Your research is now live on @{bsky_handle}",
                    style="text-align: center; color: var(--pico-muted-color);",
                ),
                # Results table
                Table(
                    Thead(Tr(Th("Publication ID"), Th("Link"))),
                    Tbody(*rows),
                ) if rows else None,
                P(
                    Small(f"Showing {min(len(results), 10)} of {len(results)} publications"),
                    style="text-align: center;",
                ) if len(results) > 10 else None,
                Hr(),
                # Step 4: Auto-sync CTA
                H4("Step 4: Keep your publications in sync"),
                P(
                    "Enable auto-sync to automatically publish future Octopus publications "
                    "to the atmosphere. We'll check for new publications every 7 days."
                ),
                Form(
                    Input(type="hidden", name="handle", value=bsky_handle),
                    Input(type="hidden", name="app_password", value=bsky_password),
                    Input(type="hidden", name="action", value="auto_sync"),
                    Button("Enable auto-sync", type="submit", cls="contrast", style="width: 100%;"),
                    Div(
                        Span("Setting up auto-sync...", aria_busy="true"),
                        id="loading-autosync",
                        cls="htmx-indicator",
                        style="display:none;",
                    ),
                    hx_post="/setup_sync",
                    hx_target="#sync-panel",
                    hx_swap="outerHTML",
                    hx_indicator="#loading-autosync",
                ),
                P(
                    A("No thanks, I'm done", href="/"),
                    style="text-align: center; margin-top: 1rem;",
                ),
                id="sync-panel",
            )
        except Exception as e:
            return _status_panel(f"Sync failed: {e}", "error")


@rt
def disable_sync(sess):
    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID first.", "error")
    
    users.update({"orcid": profile.orcid, "active": 0})
    
    return Article(
        Header(H3("Auto-sync disabled")),
        P("Your publications will no longer be synced automatically."),
        P(A("Back to home", href="/")),
        id="sync-panel",
    )
