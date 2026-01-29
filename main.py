"""FastHTML UI for Octosphere bridge."""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from fasthtml.common import *
from starlette.responses import RedirectResponse

from atproto.client import AtprotoClient
from bridge import sync_publications
from octopus.client import OctopusClient
from orcid import OrcidClient, OrcidProfile
from settings import Settings


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


@dataclass
class Credentials:
    handle: str
    app_password: str


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


def _octopus_client(profile: OrcidProfile) -> OctopusClient:
    if settings is None:
        raise RuntimeError("Settings not configured")
    return OctopusClient(
        api_url=settings.octopus_api_url,
        web_url=settings.octopus_web_url,
        access_token=profile.access_token,
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
        login_cta = P(f"Signed in as {profile.orcid}")
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
    return Article(
        Header(H3("Sync LIVE publications")),
        Form(
            Fieldset(
                Label("ATProto handle", Input(id="handle", required=True)),
                Label("App password", Input(id="app_password", type="password", required=True)),
            ),
            Button("Sync all live publications", type="submit", cls="contrast"),
            Div(
                Span("Syncing publications...", aria_busy="true"),
                id="loading",
                cls="htmx-indicator",
                style="display:none;",
            ),
            hx_post=sync_now,
            hx_target="#sync-panel",
            hx_swap="outerHTML",
            hx_indicator="#loading",
        ),
        P(A("Logout", href=logout)),
        id="sync-panel",
    )


@rt
def sync_now(creds: Credentials, sess):
    profile = _require_login(sess)
    if not profile:
        return _status_panel("Login with ORCID to sync publications.", "error")
    octopus = _octopus_client(profile)
    atproto = _atproto_client()
    auth = atproto.create_session(creds.handle, creds.app_password)
    results = sync_publications(octopus, atproto, auth, profile.orcid)
    rows = [
        Tr(
            Td(r.publication_id),
            Td(r.version_id),
            Td(A("record", href=r.uri)),
        )
        for r in results
    ]
    table = Table(
        Thead(Tr(Th("Publication"), Th("Version"), Th("AT URI"))),
        Tbody(*rows),
    )
    return Article(
        Header(H3("Sync complete")),
        P(f"Created {len(results)} AT Proto records."),
        table,
        P(A("Sync another", href=index)),
        id="sync-panel",
    )


@rt
def home():
    return RedirectResponse(url=index.to(), status_code=303)


# For package usage, run with: uvicorn octosphere.app:app --port 5001
# serve() is designed for single-file apps - use uvicorn directly for packages
