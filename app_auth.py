"""GitHub App identity: a private key in, short-lived installation tokens out.

WHY THIS REPLACES A STATIC TOKEN

Until Phase 11 every GitHub call used one fine-grained personal access token
belonging to one human, hard-coded into the environment. That works for
exactly one repository owned by exactly one person. It cannot be installed
by anyone else, cannot be revoked by the person whose repository it touches,
never expires on its own, and its blast radius is whatever that human
happened to grant it.

A GitHub App inverts all four. The App proves WHO IT IS by signing a JWT
with its private key; that JWT is worth nothing on its own - it cannot read
a repository or post a comment. It can only be traded for an INSTALLATION
ACCESS TOKEN, which is scoped to one installation, carries only the
permissions that installation accepted, and expires in an hour whether we
remember to rotate it or not.

So there are two credentials in this file and they are not interchangeable:

    the JWT                 "I am BuildDoctor."       Talks only to /app/*
    an installation token   "I am BuildDoctor, here." Talks to /repos/*

THE CACHING DECISION

Installation tokens last one hour (GitHub's documented figure). Minting one
costs a round trip and a signature, and a busy repository can produce several
webhooks in a minute, so re-minting per call would waste rate limit for
nothing.

They are cached IN MEMORY, per installation, and never in Postgres. That is
deliberate: a token is a live credential, and the database is read by a
dashboard that currently has no authentication in front of it (Phase 12's
job). Writing an hour-long GitHub credential into a table reachable that way
trades one saved HTTP request for a much worse problem. Losing the cache on
restart costs exactly one extra API call.

Refresh happens REFRESH_MARGIN_SECONDS BEFORE expiry rather than at it, so a
token cannot expire midway through a call that already passed the check.

The lock matters. Several webhooks can arrive at once, and without it each
would see an empty cache and mint its own token. The double check - once
before taking the lock, once after - means the common case (a valid cached
token) never waits, and the cold case mints exactly one.

NOTHING HERE EVER PRINTS A KEY OR A TOKEN. Errors name what failed and what
to check, never the value that failed.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import time
from datetime import datetime, timezone

import httpx
import jwt

GITHUB_API = "https://api.github.com"

# 2022-11-28 is still supported - GitHub's own version table gives it an
# end-of-support date of March 2028 - and it is what the rest of the codebase
# pins, so this pins the same one rather than running two versions in one
# process.
API_VERSION = "2022-11-28"
JSON_ACCEPT = "application/vnd.github+json"

# GitHub rejects a JWT whose exp is more than 10 minutes ahead. Nine minutes
# takes nearly all of that while leaving headroom for a server clock that runs
# slightly fast - which would otherwise be rejected as "in the future".
JWT_TTL_SECONDS = 9 * 60

# GitHub's own guidance, verbatim: "To protect against clock drift, we
# recommend that you set this 60 seconds in the past".
CLOCK_SKEW_SECONDS = 60

# Documented lifetime of an installation access token. Only a fallback - the
# real expiry is read from the response.
TOKEN_TTL_SECONDS = 3600

# Refresh this long before expiry, so a token that passes the check cannot
# expire while the call it was fetched for is still in flight.
REFRESH_MARGIN_SECONDS = 300

TIMEOUT = httpx.Timeout(30.0)


class AppAuthError(RuntimeError):
    """Raised when the App identity is missing, malformed, or refused."""


# --------------------------------------------------------------------------
# Configuration, read at call time
#
# Read per call rather than at import, matching github_client and diagnose.
# db.py is the one module that reads its variable at import time, and that has
# already cost a debugging session - see the load_dotenv note in main.py.
# --------------------------------------------------------------------------


def _private_key() -> str:
    """The App's PEM private key, from the environment.

    Stored base64-encoded because a PEM is multi-line and environment
    variables - especially in a hosting dashboard's single-line input - are
    not. Base64 turns it into one long line that survives copy-paste with no
    newline mangling.

    A raw PEM is accepted too, for the case where it was pasted into a .env
    that quotes it properly. Both paths end at the same string.
    """
    raw = os.environ.get("GITHUB_APP_PRIVATE_KEY_B64", "").strip()
    if not raw:
        raise AppAuthError(
            "GITHUB_APP_PRIVATE_KEY_B64 is not set. It holds the App's .pem "
            "private key, base64-encoded. See the Secrets section of README.md."
        )

    if raw.startswith("-----BEGIN"):
        return raw

    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AppAuthError(
            "GITHUB_APP_PRIVATE_KEY_B64 is not valid base64. Re-encode the "
            ".pem file; do not paste its contents in directly."
        ) from exc

    text = decoded.decode("utf-8", errors="replace")
    if not text.startswith("-----BEGIN"):
        raise AppAuthError(
            "GITHUB_APP_PRIVATE_KEY_B64 decoded to something that is not a PEM "
            "key. Check that the whole .pem file was encoded, header included."
        )
    return text


def _issuer() -> str:
    """The `iss` claim: who is asking.

    GitHub's current documentation says "Use of the client ID is recommended".
    The App ID still works, but the client ID is where the docs have moved, so
    it wins when both are present.
    """
    client_id = os.environ.get("GITHUB_APP_CLIENT_ID", "").strip()
    if client_id:
        return client_id

    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    if app_id:
        return app_id

    raise AppAuthError(
        "Neither GITHUB_APP_CLIENT_ID nor GITHUB_APP_ID is set. One of them "
        "identifies this App to GitHub; the client ID is preferred."
    )


def is_configured() -> bool:
    """True when the App identity could be used. Reveals nothing sensitive."""
    return bool(
        os.environ.get("GITHUB_APP_PRIVATE_KEY_B64", "").strip()
        and (
            os.environ.get("GITHUB_APP_CLIENT_ID", "").strip()
            or os.environ.get("GITHUB_APP_ID", "").strip()
        )
    )


# --------------------------------------------------------------------------
# The JWT: proving the App is the App
# --------------------------------------------------------------------------


def create_jwt() -> str:
    """Sign a short-lived JWT as the App itself.

    RS256 is not a choice - GitHub requires it, which is why the private key
    is an RSA key and why PyJWT needs its `crypto` extra installed.
    """
    now = int(time.time())
    payload = {
        "iat": now - CLOCK_SKEW_SECONDS,
        "exp": now + JWT_TTL_SECONDS,
        "iss": _issuer(),
    }
    try:
        return jwt.encode(payload, _private_key(), algorithm="RS256")
    except AppAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        # Only the exception TYPE is reported. A key-parsing failure's message
        # is not worth the risk of it quoting key material.
        raise AppAuthError(
            f"could not sign the App JWT ({type(exc).__name__}). The private "
            f"key is probably not a valid RSA PEM."
        ) from exc


def app_headers(accept: str = JSON_ACCEPT) -> dict:
    """Headers for the few endpoints the App calls AS ITSELF.

    Only /app/* endpoints accept this. A repository endpoint refuses it, which
    is the entire point of the two-credential split.
    """
    return {
        "Authorization": f"Bearer {create_jwt()}",
        "Accept": accept,
        "X-GitHub-Api-Version": API_VERSION,
    }


# --------------------------------------------------------------------------
# Installation tokens: the credential that can actually touch a repository
# --------------------------------------------------------------------------

# installation_id -> (token, unix timestamp it expires at)
_tokens: dict[int, tuple[str, float]] = {}

# One lock per installation, plus a lock guarding the dict of locks. Without
# the outer one, two coroutines racing to create the FIRST lock for an
# installation would each make their own and neither would exclude the other.
_locks: dict[int, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _lock_for(installation_id: int) -> asyncio.Lock:
    async with _locks_guard:
        lock = _locks.get(installation_id)
        if lock is None:
            lock = asyncio.Lock()
            _locks[installation_id] = lock
        return lock


def _fresh(installation_id: int) -> str | None:
    """A cached token with enough life left, or None."""
    cached = _tokens.get(installation_id)
    if cached and cached[1] - time.time() > REFRESH_MARGIN_SECONDS:
        return cached[0]
    return None


def _parse_expiry(value: str | None) -> float:
    """GitHub's ISO-8601 expires_at as a unix timestamp.

    Falls back to the documented hour if the field is missing or oddly shaped.
    A token refreshed slightly early is harmless; a crash here would take out
    the whole pipeline.
    """
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            pass
    return time.time() + TOKEN_TTL_SECONDS


async def _mint(installation_id: int) -> tuple[str, float]:
    """Trade the App JWT for an installation access token."""
    url = f"{GITHUB_API}/app/installations/{installation_id}/access_tokens"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(url, headers=app_headers())
    except httpx.RequestError as exc:
        raise AppAuthError(
            f"could not reach GitHub to mint a token for installation "
            f"{installation_id}: {type(exc).__name__}: {exc}"
        ) from exc

    if response.status_code == 401:
        raise AppAuthError(
            f"401 minting a token for installation {installation_id}. GitHub "
            f"rejected the JWT: wrong private key, wrong issuer "
            f"(GITHUB_APP_CLIENT_ID / GITHUB_APP_ID), or this server's clock is "
            f"more than a few minutes off."
        )
    if response.status_code == 404:
        raise AppAuthError(
            f"404 minting a token for installation {installation_id}. That "
            f"installation does not exist - the App was probably uninstalled."
        )
    if response.status_code >= 400:
        raise AppAuthError(
            f"HTTP {response.status_code} minting a token for installation "
            f"{installation_id} - {response.text[:300]}"
        )

    data = response.json()
    token = data.get("token")
    if not token:
        raise AppAuthError(
            f"GitHub returned no token for installation {installation_id}."
        )

    expires_at = _parse_expiry(data.get("expires_at"))
    # The expiry is printable. The token is not.
    print(
        f"  [app_auth] minted installation token for {installation_id}, "
        f"expires {data.get('expires_at')}",
        flush=True,
    )
    return token, expires_at


async def installation_token(installation_id: int) -> str:
    """A valid token for this installation, cached until close to expiry."""
    token = _fresh(installation_id)
    if token:
        return token

    lock = await _lock_for(installation_id)
    async with lock:
        # Checked again inside the lock: while this coroutine waited, whoever
        # held the lock may already have minted the token it needs.
        token = _fresh(installation_id)
        if token:
            return token

        token, expires_at = await _mint(installation_id)
        _tokens[installation_id] = (token, expires_at)
        return token


def invalidate(installation_id: int) -> None:
    """Forget a cached token.

    Called when GitHub rejects one we believed was still valid - a revoked
    installation, a permission change, a suspension. Without this, the same
    dead token would be handed out until its clock ran down.
    """
    _tokens.pop(installation_id, None)


async def installation_headers(
    installation_id: int, accept: str = JSON_ACCEPT
) -> dict:
    """Headers for any /repos/* call, authenticated as one installation."""
    return {
        "Authorization": f"Bearer {await installation_token(installation_id)}",
        "Accept": accept,
        "X-GitHub-Api-Version": API_VERSION,
    }


# --------------------------------------------------------------------------
# Finding the installation an event belongs to
# --------------------------------------------------------------------------


def installation_id_from_payload(payload: dict) -> int | None:
    """The installation id carried by a webhook delivered to the App.

    GitHub's wording: "Webhook payloads contain the installation property when
    the event is configured for and sent to a GitHub App." Every delivery we
    care about is exactly that, so this is the normal path - but it is
    documented as CONDITIONAL, not guaranteed, so callers treat None as a real
    possibility rather than an impossible one.
    """
    value = (payload.get("installation") or {}).get("id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def lookup_installation_id(repo: str) -> int | None:
    """Ask GitHub which installation covers `repo`. The fallback path.

    Authenticated as the App, not as an installation - the only thing that
    could work, since finding the installation is the whole question.
    """
    url = f"{GITHUB_API}/repos/{repo}/installation"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(url, headers=app_headers())
    except httpx.RequestError as exc:
        raise AppAuthError(
            f"could not reach GitHub to look up the installation for {repo}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if response.status_code == 404:
        # Not an error: the App simply is not installed there.
        return None
    if response.status_code >= 400:
        raise AppAuthError(
            f"HTTP {response.status_code} looking up the installation for "
            f"{repo} - {response.text[:300]}"
        )

    value = (response.json() or {}).get("id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def resolve_installation_id(payload: dict) -> int | None:
    """The installation for this event: from the payload, or looked up."""
    found = installation_id_from_payload(payload)
    if found is not None:
        return found

    repo = (payload.get("repository") or {}).get("full_name")
    if not repo:
        return None

    print(
        f"  [app_auth] payload carried no installation id; looking it up for "
        f"{repo}",
        flush=True,
    )
    return await lookup_installation_id(repo)
