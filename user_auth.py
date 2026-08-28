"""User sign-in: the same GitHub App, acting on behalf of a person.

WHY THERE IS NO SECOND APP HERE

The obvious-looking move for "let people log in with GitHub" is to register
an OAuth App. That would be a second identity to configure, a second client
secret to rotate, and a second thing that can be uninstalled independently
of the App whose data the dashboard shows.

It is also unnecessary. A GitHub App already contains the whole user
authorization flow. `builddoctor-ci` has a Client ID, a client secret and a
callback URL, and those are all the web flow needs. So this module adds a
credential type, not an application.

THREE CREDENTIALS NOW, AND THEY ARE STILL NOT INTERCHANGEABLE

    the JWT                 "I am BuildDoctor."        /app/*      app_auth
    an installation token   "I am BuildDoctor, here."  /repos/*    app_auth
    a user access token     "I am this person."        /user/*     here

The third one is new. It answers a question the first two cannot: WHO IS
LOOKING AT THE DASHBOARD, and what are they entitled to see.

WHAT CHANGED FROM THE OBVIOUS ASSUMPTION

A GitHub App's user access token does NOT take scopes. There is no
`scope=read:user` to send, and the token response returns `scope` as an
empty string, always. The token carries the App's own fine-grained
permissions instead. This is why nothing in this file asks for a scope: it
would be ignored, and writing one down would imply a control that does not
exist.

THE TOKEN IS USED ONCE AND THROWN AWAY

At the end of the callback the user access token has done its entire job:
it said who the person is (`GET /user`) and which installations they may
administer (`GET /user/installations`). Both answers go into the session
cookie. The token itself does not.

That is deliberate. The session cookie is SIGNED, not encrypted - the
browser holding it can read its contents. A `ghu_` token in there would be
handed to whoever holds the cookie, in plaintext, which is strictly worse
than not having it. Keeping it server-side is not an option either: a free
instance restarts constantly, so an in-memory store would sign everyone out
on every cold start and a database store would put a live GitHub credential
in the same table the dashboard reads.

The cost of throwing it away is honest and worth naming: the installation
list in the session is a SNAPSHOT taken at login. If someone uninstalls the
App an hour later, their cookie still claims the installation. That is
handled downstream rather than here - every request intersects the session's
list against the `installations` table, which the uninstall webhook deletes
from. The snapshot can only ever be a superset of what the database will
answer for, so it cannot widen access on its own.

NOTHING HERE EVER PRINTS A TOKEN OR THE CLIENT SECRET.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from urllib.parse import urlencode

import httpx

import app_auth

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"

# Short. Every call here happens while a human is watching a spinner, and a
# long hang looks identical to a broken login.
TIMEOUT = 15.0

# GitHub caps this at 100 and the realistic answer today is 1. The loop
# below still pages, because "there is only one installation" is exactly the
# assumption this phase exists to stop making.
PER_PAGE = 100
MAX_PAGES = 10


class UserAuthError(RuntimeError):
    """Sign-in could not be completed. Never carries a token or a secret."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def client_id() -> str:
    """The App's public Client ID. Also the JWT issuer - see app_auth."""
    value = os.environ.get("GITHUB_APP_CLIENT_ID", "").strip()
    if not value:
        raise UserAuthError(
            "GITHUB_APP_CLIENT_ID is not set. It is on the GitHub App's "
            "General settings page and is not a secret."
        )
    return value


def _client_secret() -> str:
    """The App's client secret. Never returned to a caller that logs."""
    value = os.environ.get("GITHUB_APP_CLIENT_SECRET", "").strip()
    if not value:
        raise UserAuthError(
            "GITHUB_APP_CLIENT_SECRET is not set. Generate one under Client "
            "secrets on the GitHub App's General settings page - GitHub "
            "shows it exactly once."
        )
    return value


def is_configured() -> bool:
    """True when sign-in could work. Reveals nothing sensitive."""
    return bool(
        os.environ.get("GITHUB_APP_CLIENT_ID", "").strip()
        and os.environ.get("GITHUB_APP_CLIENT_SECRET", "").strip()
    )


def public_base_url() -> str:
    """Where this service is reachable from a browser, without a trailing /.

    An environment variable rather than something derived from the request's
    Host header, and that is a security choice rather than a convenience
    one. The Host header is attacker-controlled; deriving the redirect_uri
    from it would let a request with a forged Host send the authorization
    code somewhere else. This value is fixed at deploy time and cannot be
    moved by anything arriving over the network.
    """
    return (
        os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
        .strip()
        .rstrip("/")
    )


def redirect_uri() -> str:
    """The one callback URL. Must match the App's setting BYTE FOR BYTE.

    Wildcard matching is deliberately off in the App's settings, so a
    trailing slash or an http/https mismatch here produces a redirect_uri
    error from GitHub rather than a quiet fallback to something else.
    """
    return f"{public_base_url()}/auth/callback"


# --------------------------------------------------------------------------
# CSRF state, and PKCE
# --------------------------------------------------------------------------


def new_state() -> str:
    """An unguessable value tying a callback to the browser that started it.

    Without it, anyone can send a victim a crafted /auth/callback link
    carrying THEIR authorization code, and the victim's browser silently
    ends up logged into the attacker's account - reading the attacker's
    data, or writing into it. The state is stored in the session cookie
    before the redirect and compared on the way back.
    """
    return secrets.token_urlsafe(32)


def new_pkce_pair() -> tuple[str, str]:
    """(verifier, challenge). GitHub strongly recommends PKCE for this flow.

    Belt and braces for a confidential client: the code alone is useless
    without the verifier, so an intercepted redirect cannot be redeemed by
    whoever intercepted it. The verifier rides in the session cookie, which
    never leaves this origin.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def authorize_url(*, state: str, code_challenge: str) -> str:
    """The github.com URL to send the browser to.

    No `scope` parameter, on purpose - see the module docstring. A GitHub
    App's user token carries the App's fine-grained permissions, and GitHub
    returns an empty scope string regardless of what is asked for.
    """
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# --------------------------------------------------------------------------
# Trading the code for a user access token
# --------------------------------------------------------------------------


async def exchange_code(*, code: str, code_verifier: str) -> str:
    """Swap the one-time code for a `ghu_` user access token.

    Accept: application/json is required. Without it GitHub answers with a
    form-encoded body, which json() cannot parse - a classic five minutes
    lost to a response that looks empty.
    """
    payload = {
        "client_id": client_id(),
        "client_secret": _client_secret(),
        "code": code,
        "redirect_uri": redirect_uri(),
        "code_verifier": code_verifier,
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                ACCESS_TOKEN_URL,
                data=payload,
                headers={"Accept": "application/json"},
            )
    except httpx.RequestError as exc:
        raise UserAuthError(
            f"could not reach GitHub to exchange the code: "
            f"{type(exc).__name__}"
        ) from exc

    if response.status_code >= 400:
        # The status only. An error body can echo the request that caused
        # it, and that request contained the client secret.
        raise UserAuthError(
            f"GitHub refused the code exchange with HTTP "
            f"{response.status_code}."
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise UserAuthError(
            "GitHub's token response was not JSON. The Accept header is "
            "probably wrong."
        ) from exc

    # GitHub reports failure INSIDE a 200 here. Checking the status alone
    # would let an expired code look like a successful login holding a token
    # of None.
    if body.get("error"):
        raise UserAuthError(
            f"GitHub rejected the sign-in: {body.get('error')} - "
            f"{body.get('error_description', 'no description')}"
        )

    token = body.get("access_token")
    if not token:
        raise UserAuthError("GitHub's token response contained no access_token.")
    return str(token)


# --------------------------------------------------------------------------
# Asking GitHub who this is, and what they administer
# --------------------------------------------------------------------------


def _user_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": app_auth.API_VERSION,
    }


async def _get(client: httpx.AsyncClient, url: str, token: str) -> dict:
    try:
        response = await client.get(url, headers=_user_headers(token))
    except httpx.RequestError as exc:
        raise UserAuthError(
            f"could not reach GitHub: {type(exc).__name__}"
        ) from exc

    if response.status_code == 401:
        raise UserAuthError("GitHub rejected the user access token.")
    if response.status_code >= 400:
        raise UserAuthError(
            f"HTTP {response.status_code} from {url.replace(GITHUB_API, '')}"
        )
    return response.json() or {}


async def fetch_user(token: str) -> dict:
    """`GET /user` - the person who just signed in.

    The numeric id is what gets stored, not the login. A login can be
    changed, and can be claimed by somebody else once an account is deleted;
    the id cannot.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        body = await _get(client, f"{GITHUB_API}/user", token)

    user_id = body.get("id")
    login = body.get("login")
    if user_id is None or not login:
        raise UserAuthError("GitHub's /user response had no id or login.")

    return {
        "id": int(user_id),
        "login": str(login),
        "avatar_url": body.get("avatar_url") or None,
    }


async def fetch_installations(token: str) -> list[dict]:
    """`GET /user/installations` - the authoritative access answer.

    THIS IS THE POINT OF THE WHOLE MODULE, and it is the same rule the
    pipeline already follows when it re-reads run_attempt from GitHub rather
    than tracking it locally: ask the system that owns the fact.

    GitHub lists only installations the user has explicit :read, :write or
    :admin access to, worked out from repository ownership, collaborator
    status and organisation membership. Reproducing that here would mean
    reimplementing GitHub's permission model from webhook payloads, and
    being subtly wrong about it is a data leak, not a bug.

    Filtered to THIS App: the endpoint returns installations of every App the
    user can administer, and BuildDoctor has no business knowing which other
    Apps somebody has installed - let alone scoping its own data by them.
    """
    app_id_raw = os.environ.get("GITHUB_APP_ID", "").strip()
    our_app_id = int(app_id_raw) if app_id_raw.isdigit() else None

    found: list[dict] = []
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for page in range(1, MAX_PAGES + 1):
            url = (
                f"{GITHUB_API}/user/installations"
                f"?per_page={PER_PAGE}&page={page}"
            )
            body = await _get(client, url, token)
            batch = body.get("installations") or []
            if not batch:
                break

            for item in batch:
                if our_app_id is not None and item.get("app_id") != our_app_id:
                    continue
                account = item.get("account") or {}
                found.append(
                    {
                        "id": int(item["id"]),
                        "account_login": account.get("login") or "",
                        "account_type": account.get("type") or None,
                    }
                )

            if len(batch) < PER_PAGE:
                break

    return found


# --------------------------------------------------------------------------
# Who owns the App itself
# --------------------------------------------------------------------------

# Cached for the life of the process. The App's owner does not change while
# the service is running, and this sits on the login path, which a human is
# waiting on.
_owner_cache: dict | None = None


async def app_owner() -> dict | None:
    """`GET /app` - the account that registered BuildDoctor.

    Used for exactly one thing: deciding who may see the legacy diagnoses
    that predate installation ids. Authenticated as the App, so it is
    GitHub's answer rather than a login copied into an environment variable
    that could drift out of date.

    Returns None rather than raising. A failure here must not be able to
    block sign-in; it only ever means the legacy rows stay hidden, which is
    the safe direction to fail.
    """
    global _owner_cache
    if _owner_cache is not None:
        return _owner_cache

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(
                f"{GITHUB_API}/app", headers=app_auth.app_headers()
            )
        if response.status_code >= 400:
            print(
                f"  [user_auth] GET /app returned {response.status_code}; "
                f"legacy rows will stay hidden",
                flush=True,
            )
            return None
        owner = (response.json() or {}).get("owner") or {}
    except (httpx.RequestError, app_auth.AppAuthError) as exc:
        print(
            f"  [user_auth] could not read the App owner "
            f"({type(exc).__name__}); legacy rows will stay hidden",
            flush=True,
        )
        return None

    if owner.get("id") is None:
        return None

    _owner_cache = {"id": int(owner["id"]), "login": owner.get("login") or ""}
    return _owner_cache
