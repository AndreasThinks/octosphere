"""FastHTML UI for Octosphere bridge."""
import secrets

from fasthtml.common import *
from starlette.responses import RedirectResponse
from starlette.background import BackgroundTask

from octosphere.atproto.client import AtprotoClient
from octosphere.bridge import sync_publications
from octosphere.database import db, encrypt_password
from octosphere.octopus.client import OctopusClient
from octosphere.orcid import OrcidClient, OrcidProfile
from octosphere.settings import Settings
from octosphere.tasks import task_sync_user


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


@rt
def index(sess):
    if settings_error:
        return Titled(
            "Octosphere Bridge",
            P("Missing configuration"),
            Pre(settings_error),
            P("Set the required environment variables and restart."),
        )
    profile = _profile_from_session(sess)
    login_cta = None
    if not profile:
        login_cta = A("Login with ORCID", href=login, role="button")
    else:
        login_cta = P(f"Signed in as {profile.name or profile.orcid}")
    return Titled(
        "Octosphere Bridge",
        P("Sync Octopus LIVE publications to AT Proto (Bluesky)."),
        login_cta,
        Div(id="sync-panel", hx_get=sync_panel, hx_trigger="load"),
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
    return RedirectResponse(url=index.to(), status_code=303)


@rt
def logout(sess):
    sess.pop("orcid", None)
    sess.pop("orcid_state", None)
    sess.pop("octopus_user_id", None)
    return RedirectResponse(url=index.to(), status_code=303)


@rt
def sync_panel(sess):
    profile = _profile_from_session(sess)
    if not profile:
        return _status_panel("Login with ORCID to continue.", "error")
    
    # Check if user already has auto-sync enabled
    users = db.t.users
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
            synced = db.t.synced_publications
            synced_count = len([s for s in synced() if s.get("orcid") == profile.orcid])
        
        return Article(
            Header(H3("✓ Auto-sync enabled")),
            P(f"Your publications are being synced to @{existing['bsky_handle']}"),
            P(f"📚 Total publications: {pub_count}"),
            P(f"✓ Already synced: {synced_count}"),
            P(f"Last sync: {existing.get('last_sync') or 'Never'}"),
            Form(
                Button("Disable auto-sync", type="submit", cls="secondary"),
                hx_post=disable_sync,
                hx_target="#sync-panel",
                hx_swap="outerHTML",
            ),
            P(A("Logout", href=logout)),
            id="sync-panel",
        )
    
    # Step 1: Ask for Octopus author URL
    return Article(
        Header(H3("Step 1: Connect your Octopus profile")),
        P("First, let's find your Octopus LIVE publications."),
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
            hx_post=validate_octopus,
            hx_target="#sync-panel",
            hx_swap="outerHTML",
            hx_indicator="#loading",
        ),
        P(A("Logout", href=logout)),
        id="sync-panel",
    )


@rt
def validate_octopus(octopus_url: str, sess):
    """Step 1 result: Validate Octopus URL and show publications."""
    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID first.", "error")
    
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
        # Handle different response structures
        version = pub.get("latestLiveVersion") or pub.get("latestVersion") or pub
        title = version.get("title") or pub.get("title") or "Untitled"
        pub_type = version.get("publication", {}).get("type") or pub.get("type") or ""
        pub_items.append(Li(f"{pub_type}: {title[:60]}{'...' if len(title) > 60 else ''}"))
    
    if pub_count > 5:
        pub_items.append(Li(f"...and {pub_count - 5} more"))
    
    # Step 2: Show publications and ask for Bluesky credentials
    return Article(
        Header(H3(f"📚 Found {pub_count} publications!")),
        Ul(*pub_items) if pub_items else P("No publications yet - you can still set up sync for future publications."),
        Hr(),
        H4("Step 2: Connect to Bluesky"),
        P("Enter your Bluesky credentials to sync these publications:"),
        Form(
            Fieldset(
                Label("Bluesky handle", Input(id="handle", placeholder="user.bsky.social", required=True)),
                Label("App password", Input(id="app_password", type="password", required=True)),
                Small("Create an app password at bsky.app → Settings → App Passwords"),
            ),
            Div(
                Button("Sync now (one-time)", type="submit", name="action", value="sync_once", cls="secondary"),
                Button("Enable auto-sync", type="submit", name="action", value="auto_sync", cls="contrast"),
                style="display: flex; gap: 1rem;",
            ),
            Div(
                Span("Syncing...", aria_busy="true"),
                id="loading2",
                cls="htmx-indicator",
                style="display:none;",
            ),
            hx_post=setup_sync,
            hx_target="#sync-panel",
            hx_swap="outerHTML",
            hx_indicator="#loading2",
        ),
        P(Small("One-time sync: Sync existing publications now, no future auto-sync.")),
        P(Small("Auto-sync: Sync now + automatically sync new publications every 7 days.")),
        P(A("← Back", href=sync_panel, hx_get=sync_panel, hx_target="#sync-panel")),
        id="sync-panel",
    )


@rt
def setup_sync(handle: str, app_password: str, action: str, sess):
    """Handle both one-time sync and auto-sync setup."""
    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID first.", "error")
    
    octopus_user_id = sess.get("octopus_user_id")
    if not octopus_user_id:
        return _status_panel("Session expired. Please start over.", "error")
    
    # Validate Bluesky credentials
    atproto = _atproto_client()
    try:
        auth = atproto.create_session(handle, app_password)
    except Exception as e:
        return _status_panel(f"Invalid Bluesky credentials: {e}", "error")
    
    # Get publication count
    octopus = _octopus_client()
    try:
        publications = octopus.get_user_publications(octopus_user_id)
        pub_count = len(publications)
    except Exception:
        pub_count = 0
    
    users = db.t.users
    encrypted_pw = encrypt_password(app_password)
    
    if action == "auto_sync":
        # Store credentials for ongoing sync
        users.insert(
            orcid=profile.orcid,
            bsky_handle=handle,
            encrypted_app_password=encrypted_pw,
            octopus_user_id=octopus_user_id,
            active=1,
            pk="orcid",
        )
        
        if pub_count > 0:
            message = P(f"✓ Auto-sync enabled! Syncing {pub_count} publications in the background...")
            background = BackgroundTask(task_sync_user, orcid=profile.orcid)
        else:
            message = P("✓ Auto-sync enabled! We'll sync your publications when you publish on Octopus LIVE.")
            background = None
        
        return Response(
            content=Article(
                Header(H3("Auto-sync enabled!")),
                P(f"Your publications will be synced to @{handle}"),
                message,
                P(A("Back to home", href=index)),
                id="sync-panel",
            ),
            background=background,
        )
    
    else:  # sync_once
        # Don't store credentials permanently, just sync now
        if pub_count == 0:
            return Article(
                Header(H3("Nothing to sync")),
                P("You don't have any publications on Octopus LIVE yet."),
                P("Come back after you've published!"),
                P(A("Back to home", href=index)),
                id="sync-panel",
            )
        
        # Sync publications synchronously and show results
        try:
            results = sync_publications(octopus, atproto, auth, octopus_user_id)
            
            # Record synced publications
            synced = db.t.synced_publications
            for r in results:
                synced.insert(
                    orcid=profile.orcid,
                    octopus_pub_id=r.publication_id,
                    octopus_version_id=r.version_id,
                    at_uri=r.uri,
                )
            
            rows = [
                Tr(
                    Td(r.publication_id[:12] + "..."),
                    Td(A("View on Bluesky", href=r.uri.replace("at://", "https://bsky.app/profile/").replace("/com.octopus.publication/", "/post/") if r.uri else "#")),
                )
                for r in results[:10]
            ]
            
            return Article(
                Header(H3(f"✓ Synced {len(results)} publications!")),
                P(f"Your publications are now on @{handle}"),
                Table(
                    Thead(Tr(Th("Publication"), Th("Link"))),
                    Tbody(*rows),
                ) if rows else None,
                Hr(),
                P("Want to automatically sync future publications?"),
                Form(
                    Input(type="hidden", name="handle", value=handle),
                    Input(type="hidden", name="app_password", value=app_password),
                    Input(type="hidden", name="action", value="auto_sync"),
                    Button("Enable auto-sync", type="submit", cls="contrast"),
                    hx_post=setup_sync,
                    hx_target="#sync-panel",
                    hx_swap="outerHTML",
                ),
                P(A("No thanks, back to home", href=index)),
                id="sync-panel",
            )
        except Exception as e:
            return _status_panel(f"Sync failed: {e}", "error")


@rt
def enable_sync(handle: str, app_password: str, octopus_url: str, sess):
    """Legacy endpoint - redirect to new flow."""
    return RedirectResponse(url=sync_panel.to(), status_code=303)


@rt
def disable_sync(sess):
    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID first.", "error")
    
    users = db.t.users
    users.update({"orcid": profile.orcid, "active": 0})
    
    return Article(
        Header(H3("Auto-sync disabled")),
        P("Your publications will no longer be synced automatically."),
        P(A("Back to home", href=index)),
        id="sync-panel",
    )


@rt
def home():
    return RedirectResponse(url=index.to(), status_code=303)
