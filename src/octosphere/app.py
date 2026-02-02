"""FastHTML UI for Octosphere bridge."""
import asyncio
import json
import logging
import os
import re
import secrets
import sys
from pathlib import Path
from datetime import datetime

import websockets
from fasthtml.common import *
from starlette.responses import RedirectResponse, FileResponse
from starlette.background import BackgroundTask

from octosphere.atproto.client import AtprotoClient
from octosphere.atproto.models import OCTOSPHERE_PUBLICATION_NSID
from octosphere.bridge import sync_publications
from octosphere.database import db, encrypt_password, users, synced_publications, NotFoundError
from octosphere.octopus.client import OctopusClient
from octosphere.orcid import OrcidClient, OrcidProfile
from octosphere.settings import Settings
from octosphere.tasks import task_sync_user
import threading


# --- Railway-compatible logging configuration ---
def _configure_logging():
    """Configure logging for Railway deployment.

    Railway captures stdout/stderr and recommends structured logging.
    Uses JSON format in production for better log searchability.
    """
    log_level = logging.DEBUG if os.getenv("ENVIRONMENT") == "development" else logging.INFO

    # JSON formatter for structured logging (Railway-friendly)
    class JsonFormatter(logging.Formatter):
        def format(self, record):
            log_entry = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if record.exc_info:
                log_entry["exception"] = self.formatException(record.exc_info)
            return json.dumps(log_entry)

    # Simple formatter for development
    class SimpleFormatter(logging.Formatter):
        def format(self, record):
            return f"[{record.levelname}] {record.name}: {record.getMessage()}"

    # Configure root logger to output to stdout (Railway captures this)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)

    # Use JSON in production, simple format in development
    if os.getenv("ENVIRONMENT") == "production":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(SimpleFormatter())

    # Configure the octosphere logger
    root_logger = logging.getLogger("octosphere")
    root_logger.setLevel(log_level)
    root_logger.addHandler(handler)
    root_logger.propagate = False  # Don't duplicate logs to root logger


_configure_logging()
logger = logging.getLogger(__name__)

# In-memory sync status tracking (for polling-based loading indicator)
# Format: {orcid: {"status": "syncing"|"complete"|"error", "results": [...], "error": str, "bsky_handle": str}}
_sync_status: dict[str, dict] = {}
_sync_lock = threading.Lock()


def _run_sync_in_background(
    orcid: str,
    octopus_user_id: str,
    bsky_handle: str,
    bsky_password: str,
    already_synced: set,
):
    """Run sync in background thread and update _sync_status when done."""
    try:
        octopus = _octopus_client()
        atproto = _atproto_client()
        auth = atproto.create_session(bsky_handle, bsky_password)
        
        results = sync_publications(octopus, atproto, auth, octopus_user_id, already_synced=already_synced)
        
        # Record synced publications in database
        for r in results:
            synced_publications.insert(
                orcid=orcid,
                octopus_pub_id=r.publication_id,
                octopus_version_id=r.version_id,
                at_uri=r.uri,
            )
        
        with _sync_lock:
            _sync_status[orcid] = {
                "status": "complete",
                "results": results,
                "bsky_handle": bsky_handle,
            }
    except Exception as e:
        with _sync_lock:
            _sync_status[orcid] = {
                "status": "error",
                "error": str(e),
                "bsky_handle": bsky_handle,
            }

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


# --- CSRF Protection Helpers ---
def generate_csrf_token(sess) -> str:
    """Generate or retrieve CSRF token for the session."""
    if 'csrf_token' not in sess:
        sess['csrf_token'] = secrets.token_urlsafe(32)
    return sess['csrf_token']


def verify_csrf_token(sess, token: str | None) -> bool:
    """Verify CSRF token matches session token using constant-time comparison."""
    expected = sess.get('csrf_token', '')
    if not expected or not token:
        return False
    return secrets.compare_digest(expected, token)


def csrf_input(sess):
    """Return a hidden input field with CSRF token for forms."""
    return Input(type="hidden", name="csrf_token", value=generate_csrf_token(sess))


# --- Authentication Beforeware ---
# Public routes that don't require authentication
PUBLIC_ROUTES = [
    r'^/$',
    r'^/favicon\.ico$',
    r'^/static/.*$',
    r'^/lexicon/.*$',
    r'^/login$',
    r'^/callback$',
    r'^/logout$',
    r'^/feed.*$',
]


def auth_before(req, sess):
    """Beforeware to set auth in request scope and protect private routes."""
    # Set auth from session (prevents injection via query params)
    orcid_data = sess.get('orcid')
    if orcid_data and orcid_data.get('access_token'):
        req.scope['auth'] = orcid_data
    else:
        req.scope['auth'] = None

    # Check if route requires authentication
    path = req.url.path
    for pattern in PUBLIC_ROUTES:
        if re.match(pattern, path):
            return None  # Allow access to public routes

    # Require auth for all other routes
    if not req.scope['auth']:
        return RedirectResponse('/login', status_code=303)

    return None


# Create Beforeware instance
bware = Beforeware(auth_before, skip=[])  # We handle skip logic inside auth_before


app, rt = fast_app(
    title="Octosphere",
    secret_key=settings.session_secret if settings else None,
    before=bware,
    sess_https_only=os.getenv("ENVIRONMENT", "development") == "production",  # HTTPS-only in production
    same_site='lax',  # Prevent CSRF via cross-site requests
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




def _orcid_button(text: str = "Sign in with ORCID", href: str = "/login", compact: bool = False):
    """Render an ORCID-branded sign-in button."""
    # ORCID iD icon SVG (inline, white icon on green background)
    # Using Octopus's ORCID green color: #437405
    icon_svg = NotStr("""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" style="height: 1.25rem; width: 1.25rem; flex-shrink: 0;">
      <circle cx="128" cy="128" r="128" fill="#fff"/>
      <g fill="#437405">
        <path d="M86.3 186.2H70.9V79.1h15.4v107.1z"/>
        <path d="M108.9 79.1h41.6c39.6 0 57 28.3 57 53.6 0 27.5-21.5 53.6-56.8 53.6h-41.8V79.1zm15.4 93.3h24.5c34.9 0 42.9-26.5 42.9-39.7 0-21.5-13.7-39.7-43-39.7h-24.4v79.4z"/>
        <ellipse cx="78.6" cy="58.7" rx="9.4" ry="9.4"/>
      </g>
    </svg>""")
    
    button_style = (
        "background-color: #437405; "  # Darker ORCID green matching Octopus
        "color: white; "
        "border: none; "
        "border-radius: 0.375rem; "
        f"padding: {'0.375rem 0.75rem' if compact else '0.5rem 1rem'}; "
        "display: inline-flex; "
        "align-items: center; "
        "gap: 0.5rem; "
        "text-decoration: none; "
        "font-weight: 600; "
        f"font-size: {'0.875rem' if compact else '1rem'}; "
        "cursor: pointer; "
        "transition: filter 0.2s; "
        "white-space: nowrap; "  # Prevent text wrapping
    )
    
    return A(
        icon_svg,
        Span(text),
        href=href,
        aria_label="Sign in with ORCID",
        style=button_style,
        cls="octo-orcid-btn",
    )


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
        # Wrap ORCID button in a styled Li with proper alignment
        nav_items.append(Li(
            _orcid_button(text="Sign in", compact=True),
            style="display: flex; align-items: center;",
        ))
    
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


def _custom_styles():
    """Return custom CSS styles that work in both light and dark modes."""
    return Style("""
        /* Custom theme-aware colors for Octosphere */
        :root {
            /* Success colors */
            --octo-success: #16a34a;
            --octo-success-bg: rgba(22, 163, 74, 0.1);

            /* Danger colors */
            --octo-danger: #dc2626;
            --octo-danger-bg: rgba(220, 38, 38, 0.08);
            --octo-danger-border: #dc2626;

            /* ORCID brand color */
            --octo-orcid: #437405;

            /* Card and panel backgrounds for light mode */
            --octo-card-bg: #ffffff;
            --octo-card-border: #e5e7eb;
            --octo-panel-bg: #f9fafb;
            --octo-muted-text: #6b7280;
            --octo-body-bg: #ffffff;
            --octo-surface-bg: #f3f4f6;
        }

        /* Dark mode adjustments */
        @media (prefers-color-scheme: dark) {
            :root {
                --octo-success: #22c55e;
                --octo-success-bg: rgba(34, 197, 94, 0.15);
                --octo-danger: #f87171;
                --octo-danger-bg: rgba(248, 113, 113, 0.12);
                --octo-danger-border: #f87171;
                --octo-card-bg: #1f2937;
                --octo-card-border: #374151;
                --octo-panel-bg: #111827;
                --octo-muted-text: #9ca3af;
                --octo-body-bg: #111827;
                --octo-surface-bg: #1f2937;
            }
        }

        /* Light mode specific fixes */
        @media (prefers-color-scheme: light) {
            /* Ensure articles have white backgrounds with subtle borders */
            article {
                background: var(--octo-card-bg) !important;
                border: 1px solid var(--octo-card-border);
                box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
            }

            /* Fix article headers - remove dark backgrounds */
            article > header {
                background: transparent !important;
                border-bottom: 1px solid var(--octo-card-border);
            }

            /* Fix article footers */
            article > footer {
                background: transparent !important;
                border-top: 1px solid var(--octo-card-border);
            }

            /* Ensure proper text color in articles */
            article, article p, article h1, article h2, article h3, article h4, article h5, article h6 {
                color: #1f2937;
            }

            /* Muted text should be readable but subtle */
            article small, article .muted {
                color: var(--octo-muted-text);
            }

            /* Fix code elements in light mode */
            code, pre {
                background: #f3f4f6;
                color: #1f2937;
            }

            /* Fix details/summary elements */
            details {
                background: transparent;
            }

            details > summary {
                color: var(--octo-muted-text);
            }

            /* Fix fieldset backgrounds */
            fieldset {
                background: transparent;
                border-color: var(--octo-card-border);
            }

            /* Fix input backgrounds */
            input, textarea, select {
                background: var(--octo-card-bg);
                border-color: var(--octo-card-border);
                color: #1f2937;
            }

            input:focus, textarea:focus, select:focus {
                border-color: var(--pico-primary);
            }

            /* Fix table styling */
            table {
                border-color: var(--octo-card-border);
            }

            th, td {
                border-color: var(--octo-card-border);
            }

            thead {
                background: var(--octo-panel-bg);
            }

            /* Fix the experimental banner */
            .octo-banner {
                background: var(--octo-panel-bg) !important;
                border: 1px solid var(--octo-card-border);
            }
        }

        /* Status badge styling */
        .octo-badge-success {
            background: var(--octo-success-bg);
            color: var(--octo-success);
            padding: 0.25rem 0.75rem;
            border-radius: 1rem;
            font-size: 0.875rem;
            font-weight: 600;
        }

        /* Danger zone styling */
        .octo-danger-zone {
            border: 1px solid var(--octo-danger-border);
            border-radius: var(--pico-border-radius);
            padding: 1rem;
            background: var(--octo-danger-bg);
        }

        .octo-danger-text {
            color: var(--octo-danger);
        }

        .octo-danger-btn {
            background-color: var(--octo-danger) !important;
            border-color: var(--octo-danger) !important;
            color: white !important;
        }

        .octo-danger-btn:hover {
            background-color: color-mix(in srgb, var(--octo-danger) 85%, black) !important;
            border-color: color-mix(in srgb, var(--octo-danger) 85%, black) !important;
        }

        /* Success text styling */
        .octo-success-text {
            color: var(--octo-success);
        }

        /* ORCID button hover effect */
        .octo-orcid-btn:hover {
            filter: brightness(1.1);
        }

        /* Feed card button container - always single row */
        .octo-feed-buttons {
            display: flex;
            gap: 0.375rem;
            flex-wrap: nowrap;
        }

        /* Feed card buttons - compact styling */
        .octo-feed-buttons [role="button"] {
            font-size: 0.75rem;
            padding: 0.25rem 0.5rem;
            white-space: nowrap;
            flex-shrink: 1;
            min-width: 0;
        }

        /* Hide button text on very small screens, show only icons */
        @media (max-width: 400px) {
            .octo-feed-buttons .octo-btn-text {
                display: none;
            }
            .octo-feed-buttons [role="button"] {
                padding: 0.25rem 0.5rem;
            }
            .octo-feed-buttons i {
                margin-right: 0 !important;
            }
        }

        /* Status panel styling - softer colors for light mode */
        .octo-status-panel {
            border-radius: var(--pico-border-radius);
            padding: 1rem;
        }

        .octo-status-info {
            background: #eff6ff;
            border: 1px solid #bfdbfe;
            color: #1e40af;
        }

        .octo-status-success {
            background: #f0fdf4;
            border: 1px solid #bbf7d0;
            color: #166534;
        }

        .octo-status-error {
            background: #fef2f2;
            border: 1px solid #fecaca;
            color: #991b1b;
        }

        @media (prefers-color-scheme: dark) {
            .octo-status-info {
                background: rgba(59, 130, 246, 0.15);
                border-color: rgba(59, 130, 246, 0.3);
                color: #93c5fd;
            }

            .octo-status-success {
                background: rgba(34, 197, 94, 0.15);
                border-color: rgba(34, 197, 94, 0.3);
                color: #86efac;
            }

            .octo-status-error {
                background: rgba(239, 68, 68, 0.15);
                border-color: rgba(239, 68, 68, 0.3);
                color: #fca5a5;
            }
        }
    """)


def _page(title: str, *content, profile: OrcidProfile | None = None):
    """Wrap content in a standard page layout."""
    return (
        Title(f"{title} - Octosphere"),
        Meta(name="color-scheme", content="light dark"),
        Favicon('/static/octosphere.ico', '/static/octosphere.ico'),
        Link(rel="stylesheet", href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css"),
        _custom_styles(),
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


# Run migrations on startup (only for NEW databases)
def run_migrations():
    """Handle database creation for new deployments.
    
    NOTE: Enrollment of existing databases is handled in database.py BEFORE
    fastlite.database() is called. This function only creates new databases.
    """
    from pathlib import Path
    from fastmigrate import create_db, run_migrations as fm_migrate
    
    migrations_path = os.getenv("MIGRATIONS_PATH", "migrations")
    db_path = os.getenv("DATABASE_PATH", "octosphere.db")
    
    db_file = Path(db_path)
    if not db_file.exists():
        # No database exists - create fresh versioned database
        print(f"[Octosphere] Creating new database at {db_path}")
        create_db(Path(db_path))
        # Apply migrations to set up schema
        fm_migrate(Path(db_path), Path(migrations_path))
    else:
        # Database already exists - enrollment handled by database.py
        # Don't call fm_migrate() here to avoid duplicate validation errors
        print(f"[Octosphere] Using existing database at {db_path}")


def log_db_status():
    """Log database connection status and counts on startup."""
    try:
        user_count = len(list(users()))
        pub_count = len(list(synced_publications()))
        print(f"[Octosphere] Database connected: {user_count} users, {pub_count} synced publications")
    except Exception as e:
        print(f"[Octosphere] Database connection error: {e}")


run_migrations()
log_db_status()


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


def _get_user(orcid: str) -> dict | None:
    """Get user by ORCID, returning None if not found."""
    try:
        return users[orcid]
    except NotFoundError:
        return None


def _strip_html_tags(text: str) -> str:
    """Remove HTML tags from text, returning clean plain text."""
    if not text:
        return ""
    # Remove HTML tags using regex
    clean = re.sub(r'<[^>]+>', ' ', text)
    # Collapse multiple spaces into single space
    clean = re.sub(r'\s+', ' ', clean)
    return clean.strip()


def _status_panel(message: str, status: str = "info"):
    """Return a styled status panel with proper light/dark mode support."""
    status_cls = {
        "info": "octo-status-info",
        "success": "octo-status-success",
        "error": "octo-status-error",
    }.get(status, "octo-status-info")
    return Div(
        H3("Status", style="margin-top: 0; margin-bottom: 0.5rem;"),
        P(message, style="margin-bottom: 0;"),
        cls=f"octo-status-panel {status_cls}",
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
            cls="octo-banner",
            style="padding: 0.5rem 1rem; border-radius: var(--pico-border-radius); text-align: center; margin-bottom: 1rem;",
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
            _orcid_button(text="Get started with ORCID", href="/login") if not profile else A(
                "Go to Dashboard",
                href="/dashboard",
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


def PublicationCard(record: dict, did: str, handle: str | None = None, timestamp: str | None = None, uri: str | None = None):
    """Render a publication as a social media-style card.
    
    Uses correct schema fields from social.octosphere.publication lexicon:
    - contentText: publication text content (not "description")
    - canonicalUrl: URL to Octopus publication (not "octopusUrl")
    """
    title = record.get("title") or "Untitled Publication"
    # Use contentText from schema (fall back to contentHtml stripped, then empty)
    # Strip any HTML tags from the content for clean display
    content_text = _strip_html_tags(record.get("contentText") or "")
    pub_type = record.get("publicationType") or ""
    octopus_id = record.get("octopusId") or ""
    # Use canonicalUrl from schema (correct field name)
    canonical_url = record.get("canonicalUrl") or ""
    
    # Build peer review URL: https://www.octopus.ac/create?for={octopusId}&type=PEER_REVIEW
    peer_review_url = f"https://www.octopus.ac/create?for={octopus_id}&type=PEER_REVIEW" if octopus_id else None
    
    # Build pdsls URL for AT Protocol inspection
    pdsls_url = f"https://pdsls.dev/{uri}" if uri else None
    
    # Format timestamp for display
    time_display = ""
    if timestamp:
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            time_display = dt.strftime("%b %d, %Y at %H:%M")
        except Exception:
            time_display = timestamp
    
    # Display handle or truncated DID
    author_display = f"@{handle}" if handle else f"{did[:20]}..." if did else "Unknown"
    
    # Truncate content for display (show first 300 chars)
    display_text = content_text[:300] + "..." if len(content_text) > 300 else content_text
    
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
            display_text,
            style="color: var(--pico-muted-color); margin-bottom: 1rem;",
        ) if display_text else None,
        # Footer with action links
        Footer(
            Div(
                A(
                    I(cls="fa-solid fa-eye", style="margin-right: 0.25rem;"),
                    Span("View on pdsls", cls="octo-btn-text"),
                    href=pdsls_url,
                    target="_blank",
                    role="button",
                    cls="outline secondary",
                ) if pdsls_url else None,
                A(
                    I(cls="fa-solid fa-book-open", style="margin-right: 0.25rem;"),
                    Span("View on Octopus", cls="octo-btn-text"),
                    href=canonical_url,
                    target="_blank",
                    role="button",
                    cls="outline",
                ) if canonical_url else None,
                A(
                    I(cls="fa-solid fa-comments", style="margin-right: 0.25rem;"),
                    Span("Peer Review", cls="octo-btn-text"),
                    href=peer_review_url,
                    target="_blank",
                    role="button",
                    cls="contrast",
                ) if peer_review_url else None,
                cls="octo-feed-buttons",
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


def _fetch_historic_publications(limit: int = 50) -> list[dict]:
    """Fetch historic publications from all registered users.
    
    Queries the users table to get DIDs, then fetches their publication records
    via the public AT Protocol API. Includes users with active=0 (one-time sync)
    as well as active=1 (auto-sync).
    
    Returns:
        List of dicts with: did, handle, uri, record, createdAt
    """
    atproto = _atproto_client()
    all_publications = []
    
    # Get all users with their handles and resolve to DIDs
    # Include both active=0 (one-time sync) and active=1 (auto-sync) users
    for user in users():
        
        handle = user.get("bsky_handle")
        if not handle:
            continue
        
        # Resolve handle to DID
        try:
            resolver = atproto._resolver
            did = resolver.handle.resolve(handle)
            if not did:
                continue
        except Exception:
            continue
        
        # Fetch their publication records (public API, no auth needed)
        try:
            records = atproto.list_records_public(did, limit=limit)
            for r in records:
                value = r.get("value", {})
                all_publications.append({
                    "did": did,
                    "handle": handle,
                    "uri": r.get("uri"),
                    "record": value,
                    "createdAt": value.get("createdAt") or "",
                })
        except Exception as e:
            print(f"Error fetching records for {handle}: {e}")
            continue
    
    # Sort by createdAt descending (newest first)
    all_publications.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    
    return all_publications[:limit]


@rt("/feed/history")
def feed_history():
    """Fetch and render historic publications."""
    publications = _fetch_historic_publications(limit=30)
    
    if not publications:
        return Div(
            P(
                "No publications yet. Be the first to ",
                A("sync your research", href="/login"),
                "!",
                style="text-align: center; color: var(--pico-muted-color); padding: 2rem 0;",
            ),
            id="history-container",
        )
    
    cards = [
        PublicationCard(
            p["record"],
            p["did"],
            handle=p.get("handle"),
            timestamp=p.get("createdAt"),
            uri=p.get("uri"),
        )
        for p in publications
    ]
    
    return Div(
        *cards,
        id="history-container",
    )


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
        Meta(name="color-scheme", content="light dark"),
        Favicon('/static/octosphere.ico', '/static/octosphere.ico'),
        Link(rel="stylesheet", href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css"),
        _custom_styles(),
        Script(src="https://unpkg.com/htmx-ext-sse@2.2.3/sse.js"),
        _nav(profile),
        Main(
            # Experimental banner
            Div(
                Strong("Experimental"), " — This feed shows publications synced as part of an AT Protocol experiment.",
                cls="octo-banner",
                style="padding: 0.5rem 1rem; border-radius: var(--pico-border-radius); text-align: center; margin-bottom: 1rem;",
            ),
            Header(
                H1("Research Feed"),
                P(
                    "Recent and live publications from the atmosphere",
                    style="font-size: 1.25rem; color: var(--pico-muted-color);",
                ),
                P(
                    A(
                        I(cls="fa-solid fa-satellite-dish", style="margin-right: 0.25rem;"),
                        "View live Jetstream",
                        href="https://pdsls.dev/jetstream?instance=wss%3A%2F%2Fjetstream1.us-east.bsky.network%2Fsubscribe&collections=social.octosphere.publication",
                        target="_blank",
                        title="View live Jetstream feed on pdsls.dev - see real-time AT Protocol events",
                        style="color: var(--pico-muted-color); font-size: 0.875rem; text-decoration: none;",
                    ),
                    style="margin-top: 0.5rem;",
                ),
                style="text-align: center; padding: 2rem 0;",
            ),
            # Live streaming container - new publications appear at the top (above history)
            Div(
                # This div receives live SSE updates
                id="live-container",
                hx_ext="sse",
                sse_connect="/feed/stream",
                hx_swap="afterbegin",
                sse_swap="message",
            ),
            # Separator between live and historic
            Div(
                Hr(),
                P(
                    Small("Recent publications"),
                    style="text-align: center; color: var(--pico-muted-color); margin: 0.5rem 0;",
                ),
                id="separator",
                style="display: none;",  # Hidden until we have both live and historic
            ),
            # Historic publications container - loaded via HTMX on page load
            Div(
                P(
                    Span(aria_busy="true", style="margin-right: 0.5rem;"),
                    "Loading recent publications...",
                    style="text-align: center; color: var(--pico-muted-color);",
                ),
                id="history-container",
                hx_get="/feed/history",
                hx_trigger="load",
                hx_swap="outerHTML",
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
    
    # Check if user already has auto-sync enabled (efficient lookup using try/except)
    existing = _get_user(profile.orcid)
    
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
        
        bsky_handle = existing.get("bsky_handle", "")
        last_sync = existing.get("last_sync")

        # Resolve DID for PDSLS link
        bsky_did = None
        try:
            atproto = _atproto_client()
            bsky_did = atproto._resolver.handle.resolve(bsky_handle)
        except Exception:
            pass
        
        # Format last sync time
        if last_sync:
            try:
                dt = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
                last_sync_display = dt.strftime("%b %d, %Y at %H:%M")
            except Exception:
                last_sync_display = last_sync
        else:
            last_sync_display = "Never"
        
        # Calculate sync progress percentage
        sync_pct = int((synced_count / pub_count * 100)) if pub_count > 0 else 100
        sync_complete = synced_count >= pub_count and pub_count > 0
        
        return Div(
            # Status Card
            Article(
                # Header with status badge
                Div(
                    Span(
                        I(cls="fa-solid fa-circle", style="font-size: 0.5rem; margin-right: 0.5rem;"),
                        "Auto-sync Active",
                        cls="octo-badge-success",
                    ),
                    style="margin-bottom: 1rem;",
                ),
                # Connections section
                H4(
                    I(cls="fa-solid fa-link", style="margin-right: 0.5rem; color: var(--pico-muted-color);"),
                    "Connected Accounts",
                ),
                Div(
                    Div(
                        Strong("ORCID: "),
                        A(
                            profile.orcid,
                            href=f"https://orcid.org/{profile.orcid}",
                            target="_blank",
                        ),
                        style="margin-bottom: 0.5rem;",
                    ),
                    Div(
                        Strong("Bluesky: "),
                        A(
                            f"@{bsky_handle}",
                            href=f"https://bsky.app/profile/{bsky_handle}",
                            target="_blank",
                        ),
                    ),
                    style="margin-bottom: 1.5rem;",
                ),
            ),
            # Sync Status Card
            Article(
                H4(
                    I(cls="fa-solid fa-sync", style="margin-right: 0.5rem; color: var(--pico-muted-color);"),
                    "Sync Status",
                ),
                # Progress bar
                Div(
                    Div(
                        style=f"width: {sync_pct}%; background: var(--pico-primary); height: 100%; border-radius: 0.25rem;",
                    ),
                    style="background: var(--pico-muted-border-color); height: 0.5rem; border-radius: 0.25rem; margin-bottom: 0.5rem;",
                ),
                Div(
                    Strong(f"{synced_count} of {pub_count} publications synced"),
                    " ✓" if sync_complete else "",
                    style="margin-bottom: 0.5rem;",
                    cls="octo-success-text" if sync_complete else "",
                ),
                Small(
                    I(cls="fa-regular fa-clock", style="margin-right: 0.25rem;"),
                    f"Last sync: {last_sync_display}",
                    style="color: var(--pico-muted-color);",
                ),
            ),
            # Actions Card
            Article(
                H4(
                    I(cls="fa-solid fa-bolt", style="margin-right: 0.5rem; color: var(--pico-muted-color);"),
                    "Actions",
                ),
                Div(
                    Form(
                        csrf_input(sess),  # CSRF protection
                        Button(
                            I(cls="fa-solid fa-rotate", style="margin-right: 0.5rem;"),
                            "Sync Now",
                            type="submit",
                            cls="contrast",
                            style=" margin-bottom: 0px;",
                        ),
                        Div(
                            Span("Syncing...", aria_busy="true"),
                            id="sync-loading",
                            cls="htmx-indicator",
                            style="display:none; margin-left: 0.5rem;",
                        ),
                        hx_post="/manual_sync",
                        hx_target="#sync-panel",
                        hx_swap="outerHTML",
                        hx_indicator="#sync-loading",
                        style="display: flex; align-items: center;",
                    ),
                    A(
                        I(cls="fa-solid fa-arrow-up-right-from-square", style="margin-right: 0.5rem;"),
                        "View on PDSLS",
                        href=f"https://pdsls.dev/at://{bsky_did}/social.octosphere.publication" if bsky_did else f"https://bsky.app/profile/{bsky_handle}",
                        target="_blank",
                        role="button",
                        cls="outline",
                    ),
                    style="display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1rem;",
                ),
                Hr(),
                Form(
                    csrf_input(sess),  # CSRF protection
                    Button(
                        I(cls="fa-solid fa-power-off", style="margin-right: 0.5rem;"),
                        "Disable auto-sync",
                        type="submit",
                        cls="secondary outline",
                    ),
                    hx_post="/disable_sync",
                    hx_target="#sync-panel",
                    hx_swap="outerHTML",
                ),
            ),
            # Hidden Advanced Options section
            Details(
                Summary(
                    I(cls="fa-solid fa-gear", style="margin-right: 0.5rem;"),
                    "Advanced options",
                    style="cursor: pointer; color: var(--pico-muted-color); font-size: 0.875rem;",
                ),
                Article(
                    H4(
                        I(cls="fa-solid fa-triangle-exclamation octo-danger-text", style="margin-right: 0.5rem;"),
                        "Danger Zone",
                        cls="octo-danger-text",
                    ),
                    P(
                        "Delete all your Octosphere publication records from the AT Protocol network "
                        "and disable auto-sync. This only removes ",
                        Code("social.octosphere.publication"),
                        " records — your Bluesky posts, likes, and follows are not affected.",
                        style="font-size: 0.875rem; color: var(--pico-muted-color);",
                    ),
                    Form(
                        csrf_input(sess),  # CSRF protection
                        Fieldset(
                            Label(
                                "Confirm with your Bluesky app password",
                                Input(
                                    id="confirm_password",
                                    name="confirm_password",
                                    type="password",
                                    placeholder="App password",
                                    required=True,
                                ),
                            ),
                            Small(
                                "For security, re-enter your app password to confirm this destructive action.",
                                style="color: var(--pico-muted-color);",
                            ),
                        ),
                        Button(
                            I(cls="fa-solid fa-trash", style="margin-right: 0.5rem;"),
                            "Delete All Records & Disconnect",
                            type="submit",
                            cls="octo-danger-btn",
                        ),
                        Div(
                            Span("Deleting records...", aria_busy="true"),
                            id="delete-loading",
                            cls="htmx-indicator",
                            style="display:none;",
                        ),
                        hx_post="/delete_all_records",
                        hx_target="#sync-panel",
                        hx_swap="outerHTML",
                        hx_indicator="#delete-loading",
                        hx_confirm="Are you sure? This will permanently delete all your Octosphere publication records from the AT Protocol network.",
                    ),
                    Hr(cls="octo-danger-text", style="margin: 1.5rem 0; border-color: var(--octo-danger-border);"),
                    # Delete Account section
                    H4(
                        I(cls="fa-solid fa-user-slash octo-danger-text", style="margin-right: 0.5rem;"),
                        "Delete Account",
                        cls="octo-danger-text",
                    ),
                    P(
                        "Remove your Octosphere account entirely. ",
                        Strong("Note: "),
                        "This will NOT delete your publication records from the AT Protocol network. "
                        "Use 'Delete All Records' above first if you want to remove those.",
                        style="font-size: 0.875rem; color: var(--pico-muted-color);",
                    ),
                    Form(
                        csrf_input(sess),  # CSRF protection
                        Button(
                            I(cls="fa-solid fa-user-minus", style="margin-right: 0.5rem;"),
                            "Delete My Account",
                            type="submit",
                            cls="octo-danger-btn",
                        ),
                        Div(
                            Span("Deleting account...", aria_busy="true"),
                            id="delete-account-loading",
                            cls="htmx-indicator",
                            style="display:none;",
                        ),
                        hx_post="/delete_account",
                        hx_indicator="#delete-account-loading",
                        hx_confirm="Are you sure you want to delete your Octosphere account? This will NOT delete your publication records from the AT Protocol network - use 'Delete All Records' first if you want to remove those.",
                    ),
                    cls="octo-danger-zone",
                    style="margin-top: 1rem;",
                ),
                style="margin-top: 1.5rem;",
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
                csrf_input(sess),  # CSRF protection
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
            csrf_input(sess),  # CSRF protection
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
def validate_bluesky(handle: str, app_password: str, sess, csrf_token: str | None = None):
    """Step 1: Validate Bluesky credentials and store in session."""
    # Verify CSRF token
    if not verify_csrf_token(sess, csrf_token):
        return _status_panel("Invalid request. Please try again.", "error")

    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID first.", "error")

    # Validate Bluesky credentials
    atproto = _atproto_client()
    try:
        auth = atproto.create_session(handle, app_password)
    except Exception as e:
        logger.warning(f"Bluesky auth failed: {e}")
        return _status_panel("Invalid Bluesky credentials. Please check your handle and app password.", "error")
    
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
            csrf_input(sess),  # CSRF protection
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
            csrf_input(sess),  # CSRF protection
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
def validate_octopus(octopus_url: str, sess, csrf_token: str | None = None):
    """Step 2 result: Validate Octopus URL and show publications with sync button."""
    # Verify CSRF token
    if not verify_csrf_token(sess, csrf_token):
        return _status_panel("Invalid request. Please try again.", "error")

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
        logger.warning(f"Octopus profile verification failed: {e}")
        return _status_panel("Could not verify Octopus profile. Please try again later.", "error")
    
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
                csrf_input(sess),  # CSRF protection
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
            csrf_input(sess),  # CSRF protection
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
def setup_sync(action: str, sess, csrf_token: str | None = None):
    """Handle both one-time sync and auto-sync setup."""
    # Verify CSRF token
    if not verify_csrf_token(sess, csrf_token):
        return _status_panel("Invalid request. Please try again.", "error")

    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID first.", "error")

    octopus_user_id = sess.get("octopus_user_id")
    if not octopus_user_id:
        return _status_panel("Session expired. Please start over.", "error")

    # Get Bluesky credentials from session only (never from form for security)
    bsky_handle = sess.get("bsky_handle")
    bsky_password = sess.get("bsky_app_password")

    if not bsky_handle or not bsky_password:
        return _status_panel("Bluesky credentials not found. Please start over.", "error")

    # Validate Bluesky credentials
    atproto = _atproto_client()
    try:
        auth = atproto.create_session(bsky_handle, bsky_password)
    except Exception as e:
        logger.warning(f"Bluesky auth failed for handle: {e}")
        return _status_panel("Invalid Bluesky credentials. Please check your handle and app password.", "error")
    
    # Get publication count
    octopus = _octopus_client()
    try:
        publications = octopus.get_user_publications(octopus_user_id)
        pub_count = len(publications)
    except Exception:
        pub_count = 0
    
    encrypted_pw = encrypt_password(bsky_password)
    
    if action == "auto_sync":
        # Store/update credentials for ongoing sync (use upsert in case user already exists from sync_once)
        users.upsert(
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
        
        # Return content with optional background task (FastHTML pattern: return as tuple)
        content = Article(
            Header(H3("Auto-sync enabled")),
            P(f"Your publications will be synced to @{bsky_handle}"),
            message,
            P(A("Back to home", href="/")),
            id="sync-panel",
        )
        if background:
            return content, background
        else:
            return content
    
    else:  # sync_once
        # Store user in database with active=0 so they appear in feed but don't auto-sync
        # Use upsert in case user already exists (allows re-syncing)
        users.upsert(
            orcid=profile.orcid,
            bsky_handle=bsky_handle,
            encrypted_app_password=encrypted_pw,  # Still encrypted, but won't be used for auto-sync
            octopus_user_id=octopus_user_id,
            active=0,  # Not active for auto-sync, but will appear in feed
            pk="orcid",
        )
        
        if pub_count == 0:
            return Article(
                Header(H3("Nothing to sync")),
                P("You don't have any publications on Octopus yet."),
                P("Come back after you've published!"),
                P(A("Back to home", href="/")),
                id="sync-panel",
            )
        
        # Get already synced publications to prevent duplicates
        already_synced = {
            (s.get("octopus_pub_id"), s.get("octopus_version_id"))
            for s in synced_publications()
            if s.get("orcid") == profile.orcid
        }
        
        # Set initial sync status and start background thread
        with _sync_lock:
            _sync_status[profile.orcid] = {
                "status": "syncing",
                "bsky_handle": bsky_handle,
            }
        
        # Start sync in background thread
        sync_thread = threading.Thread(
            target=_run_sync_in_background,
            args=(profile.orcid, octopus_user_id, bsky_handle, bsky_password, already_synced),
            daemon=True,
        )
        sync_thread.start()
        
        # Return polling UI that checks /sync_status/{orcid} every second
        return Article(
            P(
                Span(aria_busy="true", style="margin-right: 0.5rem;"),
                "Syncing your publications to the atmosphere...",
                style="text-align: center; padding: 1rem 0;",
            ),
            P(
                Small("This may take a moment depending on how many publications you have."),
                style="text-align: center; color: var(--pico-muted-color);",
            ),
            id="sync-panel",
            hx_get=f"/sync_status/{profile.orcid}",
            hx_trigger="every 1s",
            hx_swap="outerHTML",
        )


@rt
def manual_sync(sess, csrf_token: str | None = None):
    """Manually trigger a sync for the current user."""
    # Verify CSRF token
    if not verify_csrf_token(sess, csrf_token):
        return _status_panel("Invalid request. Please try again.", "error")

    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID first.", "error")

    # Get user data (efficient lookup using try/except)
    existing = _get_user(profile.orcid)
    if not existing or not existing.get("active"):
        return _status_panel("Auto-sync not enabled.", "error")

    # Trigger background sync
    from octosphere.tasks import task_sync_user
    task_sync_user(profile.orcid)

    # Update last_sync timestamp
    users.update({
        "orcid": profile.orcid,
        "last_sync": datetime.utcnow().isoformat() + "Z",
    })

    # Return to sync panel (it will refresh and show updated stats)
    return Div(
        Article(
            Header(H3("✅ Sync Complete")),
            P("Your publications have been synced."),
            P(A("Refresh dashboard", href="/sync_panel", hx_get="/sync_panel", hx_target="#sync-panel")),
            id="sync-panel",
        ),
        # Auto-refresh after 2 seconds
        Script("setTimeout(() => htmx.ajax('GET', '/sync_panel', '#sync-panel'), 2000);"),
    )


@rt
def disable_sync(sess, csrf_token: str | None = None):
    """Disable auto-sync for the current user."""
    # Verify CSRF token
    if not verify_csrf_token(sess, csrf_token):
        return _status_panel("Invalid request. Please try again.", "error")

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


@rt("/sync_status/{orcid}")
def sync_status(orcid: str, sess):
    """Polling endpoint for sync status - returns syncing UI or final results."""
    profile = _require_login(sess)
    if not profile or profile.orcid != orcid:
        return _status_panel("Unauthorized.", "error")
    
    with _sync_lock:
        status = _sync_status.get(orcid)
    
    if not status:
        # No status found - sync may not have started yet
        return Article(
            P(
                Span(aria_busy="true", style="margin-right: 0.5rem;"),
                "Starting sync...",
                style="text-align: center; padding: 1rem 0;",
            ),
            id="sync-panel",
            hx_get=f"/sync_status/{orcid}",
            hx_trigger="every 1s",
            hx_swap="outerHTML",
        )
    
    if status["status"] == "syncing":
        # Still syncing - show spinner and keep polling
        return Article(
            P(
                Span(aria_busy="true", style="margin-right: 0.5rem;"),
                "Syncing your publications to the atmosphere...",
                style="text-align: center; padding: 1rem 0;",
            ),
            P(
                Small("This may take a moment depending on how many publications you have."),
                style="text-align: center; color: var(--pico-muted-color);",
            ),
            id="sync-panel",
            hx_get=f"/sync_status/{orcid}",
            hx_trigger="every 1s",
            hx_swap="outerHTML",
        )
    
    if status["status"] == "error":
        # Sync failed - clean up and show error
        with _sync_lock:
            _sync_status.pop(orcid, None)
        return _status_panel(f"Sync failed: {status.get('error', 'Unknown error')}", "error")
    
    # status == "complete" - show results
    results = status.get("results", [])
    bsky_handle = status.get("bsky_handle", "")
    
    # Clean up status
    with _sync_lock:
        _sync_status.pop(orcid, None)
    
    # Get session data for auto-sync form
    bsky_password = sess.get("bsky_app_password", "")
    
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
            csrf_input(sess),  # CSRF protection
            # Note: handle and password read from session, not form (security)
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


@rt
def delete_account(sess, csrf_token: str | None = None):
    """Delete the user's Octosphere account.

    This removes the user from the database and clears synced_publications,
    but does NOT delete their AT Protocol records (publications remain on the network).
    """
    # Verify CSRF token
    if not verify_csrf_token(sess, csrf_token):
        return _status_panel("Invalid request. Please try again.", "error")

    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID first.", "error")
    
    # Delete synced_publications entries for this user
    user_synced = [s for s in synced_publications() if s.get("orcid") == profile.orcid]
    for s in user_synced:
        try:
            synced_publications.delete(s.get("id"))
        except Exception:
            pass
    
    # Delete user from users table
    try:
        users.delete(profile.orcid)
    except Exception:
        pass  # User may not exist in users table yet
    
    # Clear the session
    sess.pop("orcid", None)
    sess.pop("orcid_state", None)
    sess.pop("octopus_user_id", None)
    sess.pop("bsky_handle", None)
    sess.pop("bsky_app_password", None)
    sess.pop("bsky_authenticated", None)
    
    # Return a page that redirects to home (HTMX can't do full redirects easily)
    return Response(
        content=str(Div(
            Script("window.location.href = '/';"),
            P("Account deleted. Redirecting...", style="text-align: center;"),
        )),
        headers={"HX-Redirect": "/"},
    )


@rt
def delete_all_records(confirm_password: str, sess, csrf_token: str | None = None):
    """Delete all Octosphere publication records and disable auto-sync.

    This only deletes social.octosphere.publication records - not posts, likes, follows, etc.
    """
    # Verify CSRF token
    if not verify_csrf_token(sess, csrf_token):
        return _status_panel("Invalid request. Please try again.", "error")

    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID first.", "error")

    # Get user data
    existing = _get_user(profile.orcid)
    if not existing:
        return _status_panel("User not found.", "error")

    bsky_handle = existing.get("bsky_handle", "")
    if not bsky_handle:
        return _status_panel("Bluesky handle not found.", "error")

    # Authenticate with Bluesky using the provided password (not stored password)
    atproto = _atproto_client()
    try:
        auth = atproto.create_session(bsky_handle, confirm_password)
    except Exception as e:
        logger.warning(f"Delete records auth failed: {e}")
        return _status_panel("Invalid password. Please check and try again.", "error")

    # Get all Octosphere publication records (ONLY our lexicon collection)
    try:
        records = atproto.list_records(
            auth.did,
            collection=OCTOSPHERE_PUBLICATION_NSID,  # ONLY social.octosphere.publication
            limit=100,
        )
    except Exception as e:
        logger.error(f"Failed to list records for deletion: {e}")
        return _status_panel("Failed to list records. Please try again later.", "error")
    
    # Delete each record
    deleted_count = 0
    errors = []
    for record in records:
        uri = record.get("uri")
        if uri:
            try:
                atproto.delete_record(auth, uri)
                deleted_count += 1
            except Exception as e:
                logger.warning(f"Failed to delete record {uri}: {e}")
                errors.append(uri)  # Only store URI, not error details

    # Clear synced_publications entries for this user
    # We need to delete them one by one since fastlite doesn't have bulk delete
    user_synced = [s for s in synced_publications() if s.get("orcid") == profile.orcid]
    for s in user_synced:
        try:
            synced_publications.delete(s.get("id"))
        except Exception:
            pass  # Ignore errors when clearing local records

    # Disable auto-sync
    users.update({"orcid": profile.orcid, "active": 0})

    # Build result message
    if errors:
        error_msg = P(
            Strong(f"⚠️ {len(errors)} record(s) could not be deleted."),
            Small(" Some records may have already been removed or be inaccessible."),
            cls="octo-danger-text",
            style="font-size: 0.875rem;",
        )
    else:
        error_msg = None
    
    return Article(
        Header(
            H3("🗑️ Records Deleted"),
            style="text-align: center;",
        ),
        P(
            f"Deleted {deleted_count} publication records from the AT Protocol network.",
            style="text-align: center;",
        ),
        P(
            "Auto-sync has been disabled. Your Bluesky posts, likes, and follows were not affected.",
            style="text-align: center; color: var(--pico-muted-color);",
        ),
        error_msg,
        Hr(),
        P(
            A("Back to home", href="/", role="button", cls="contrast"),
            style="text-align: center;",
        ),
        id="sync-panel",
    )


serve()
