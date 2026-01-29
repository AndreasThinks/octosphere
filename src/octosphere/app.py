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
    # Find migrations relative to this file or use env var
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
        if existing.get("octopus_user_id"):
            octopus = _octopus_client()
            try:
                publications = octopus.get_user_publications(existing["octopus_user_id"])
                pub_count = len(publications)
            except Exception:
                pass
        
        return Article(
            Header(H3("Auto-sync enabled")),
            P(f"Your publications are being synced to @{existing['bsky_handle']}"),
            P(f"Publications found: {pub_count}"),
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
    
    # Show setup form - need Octopus author URL and Bluesky credentials
    return Article(
        Header(H3("Set up auto-sync")),
        P("To sync your Octopus LIVE publications to Bluesky, we need:"),
        Form(
            Fieldset(
                Legend("Octopus Profile"),
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
            Fieldset(
                Legend("Bluesky Account"),
                Label("Bluesky handle", Input(id="handle", placeholder="user.bsky.social", required=True)),
                Label("App password", Input(id="app_password", type="password", required=True)),
                Small("Create an app password at bsky.app → Settings → App Passwords"),
            ),
            Button("Enable auto-sync", type="submit", cls="contrast"),
            Div(
                Span("Setting up sync...", aria_busy="true"),
                id="loading",
                cls="htmx-indicator",
                style="display:none;",
            ),
            hx_post=enable_sync,
            hx_target="#sync-panel",
            hx_swap="outerHTML",
            hx_indicator="#loading",
        ),
        P(A("Logout", href=logout)),
        id="sync-panel",
    )


@rt
def enable_sync(handle: str, app_password: str, octopus_url: str, sess):
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
    
    # Verify the Octopus user exists
    octopus = _octopus_client()
    try:
        user_info = octopus.get_user_info(octopus_user_id)
        if not user_info:
            return _status_panel("Octopus user not found. Check your author URL.", "error")
    except Exception as e:
        return _status_panel(f"Could not verify Octopus profile: {e}", "error")
    
    # Validate Bluesky credentials
    atproto = _atproto_client()
    try:
        auth = atproto.create_session(handle, app_password)
    except Exception as e:
        return _status_panel(f"Invalid Bluesky credentials: {e}", "error")
    
    # Check how many publications exist
    try:
        publications = octopus.get_user_publications(octopus_user_id)
        pub_count = len(publications)
    except Exception:
        pub_count = 0
    
    # Store encrypted credentials
    users = db.t.users
    encrypted_pw = encrypt_password(app_password)
    
    users.insert(
        orcid=profile.orcid,
        bsky_handle=handle,
        encrypted_app_password=encrypted_pw,
        octopus_user_id=octopus_user_id,
        active=1,
        pk="orcid",
    )
    
    # Build response message
    if pub_count > 0:
        message = P(f"Found {pub_count} publications. Initial sync is running in the background...")
        background = BackgroundTask(task_sync_user, orcid=profile.orcid)
    else:
        message = P("No publications found yet. We'll sync them when you publish on Octopus LIVE.")
        background = None
    
    # Trigger background sync if there are publications
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


@rt
def disable_sync(sess):
    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID first.", "error")
    
    users = db.t.users
    users.update({"orcid": profile.orcid, "active": 0})
    
    return Article(
        Header(H3("Auto-sync disabled")),
        P("Your publications will no longer be synced."),
        P(A("Back to home", href=index)),
        id="sync-panel",
    )


@rt
def home():
    return RedirectResponse(url=index.to(), status_code=303)
