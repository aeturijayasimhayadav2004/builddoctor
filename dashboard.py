"""Read-only HTTP routes behind the dashboard (Phase 7).

WHY THIS IS NOT A SEPARATE SERVICE

The obvious-looking move is a small backend of its own that serves the
dashboard. It would need its own image, its own connection pool to the
same database, its own place in docker-compose, and its own way of going
down - and in return it would be able to do exactly nothing the app cannot
already do. db.py is already imported here, already holds the pool, and
already knows the schema.

That is Phase 5's rule applied one layer up. Phase 5 exposed four MCP tools
and stopped, because a capability is only worth a new surface if something
actually needs it to be separate. Reading rows the app already owns does
not qualify.

Everything in this module reads. There is no route here that writes,
deletes, re-runs, or posts anything.

THAT IS NOT THE SAME AS BEING SAFE TO LEAVE OPEN, WHICH IS WHAT PHASE 7
CLAIMED HERE AND PHASE 12 CORRECTS.

Read-only means these routes cannot damage anything. It says nothing about
disclosure, and disclosure is the actual risk: a diagnosis carries a repo
name, a workflow name, the files a commit touched, and a raw excerpt of a
build log. Build logs leak. They contain internal hostnames, package
registry URLs, occasionally a path with somebody's name in it. Handing all
of that to an unauthenticated stranger is a real harm even though no button
here writes a row.

So every route below now requires a signed-in GitHub user, and returns only
the diagnoses belonging to installations that GitHub itself says that user
may administer. The filtering is not done here - it is a WHERE clause in
db.py that fails closed - and this module's job is to work out WHO is
asking and hand that answer down.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import db

router = APIRouter(prefix="/api", tags=["dashboard"])

# The key the signed session cookie stores the viewer under. One key holding
# one dict, rather than several loose keys, so signing out is a single pop
# and cannot half-succeed.
SESSION_KEY = "viewer"

# The first line of a diagnosis, cut to something a table cell can hold.
# The full text is in the same response, so nothing is lost - this is only
# what to show before a row is expanded.
SUMMARY_MAX_CHARS = 240


def _first_line(text: str) -> str:
    """The first line with anything on it, shortened."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            if len(stripped) > SUMMARY_MAX_CHARS:
                return stripped[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
            return stripped
    return ""


def _blank_to_none(value: Any) -> Any:
    """Turn "" into None so the UI has one empty case to handle, not two."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _serialise(row: db.Diagnosis, embedded: bool) -> dict:
    """One database row as the flat object the frontend expects.

    Half of what the dashboard shows lives in the `raw` JSONB column rather
    than in a column of its own - posted_url, run_url, workflow,
    memory_match. Flattening it here means the frontend never has to know
    which fields were promoted to columns and which were not, and a field
    that gets promoted later changes nothing on the other side of the wire.
    """
    raw = row.raw or {}
    match = raw.get("memory_match")

    return {
        "id": row.id,
        "run_id": row.run_id,
        "repo": row.repo,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        # NULL for rows 1-4, which were diagnosed before Phase 4 invented
        # lanes. Passed through as null rather than guessed at.
        "lane": row.lane,
        "summary": _first_line(row.diagnosis_text),
        "diagnosis_text": row.diagnosis_text or "",
        "log_excerpt": row.log_excerpt or "",
        "posted_to": _blank_to_none(row.posted_to),
        # Null on row 10 (a Phase 5 bug) and null for every amber run,
        # which never posts a comment at all. Two different reasons, the
        # same honest value.
        "posted_url": _blank_to_none(raw.get("posted_url")),
        "run_url": _blank_to_none(raw.get("run_url")),
        "workflow": _blank_to_none(raw.get("workflow")),
        "failed_step": _blank_to_none(raw.get("failed_step")),
        "run_attempt": raw.get("run_attempt"),
        "files_changed": (row.diff_summary or {}).get("files_changed") or [],
        # Whether memory ran at all, which is not the same as whether it
        # found something - see the comment on _MEMORY_ASKED in db.py.
        "memory_checked": "memory_match" in raw,
        "memory_match": match if isinstance(match, dict) else None,
        "embedded": embedded,
    }


# --------------------------------------------------------------------------
# Who is asking
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Viewer:
    """One signed-in person, as recovered from the session cookie.

    Frozen because nothing downstream has any business editing it. Note what
    is NOT in here: the user access token. It did its job during the callback
    and was discarded - see the module docstring in user_auth.py.
    """

    user_id: int
    login: str
    avatar_url: str | None
    # A SNAPSHOT of what GitHub said at login. Never trusted on its own -
    # every request intersects it against the installations table.
    installation_ids: list[int]
    accounts: list[str]
    # Whether this is the account that registered the App, which is the only
    # thing that unlocks the pre-Phase-11 rows.
    is_app_owner: bool


def sign_in(
    request: Request,
    *,
    user: dict,
    installations: list[dict],
    is_app_owner: bool,
) -> None:
    """Write the session. Called by the OAuth callback in main.py."""
    request.session[SESSION_KEY] = {
        "user_id": user["id"],
        "login": user["login"],
        "avatar_url": user.get("avatar_url"),
        "installation_ids": [item["id"] for item in installations],
        "accounts": [item["account_login"] for item in installations],
        "is_app_owner": bool(is_app_owner),
    }


def sign_out(request: Request) -> None:
    """Forget the session. There is no server-side record to also delete."""
    request.session.pop(SESSION_KEY, None)


def current_viewer(request: Request) -> Viewer | None:
    """The signed-in person, or None. Never raises on a malformed cookie.

    A cookie whose shape does not match is treated as absent rather than as
    an error. The realistic cause is a deploy that changed this structure,
    and the right behaviour then is "you are logged out", not a 500 on every
    page load until the cookie expires.
    """
    raw = request.session.get(SESSION_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return Viewer(
            user_id=int(raw["user_id"]),
            login=str(raw["login"]),
            avatar_url=raw.get("avatar_url"),
            installation_ids=[
                int(value) for value in raw.get("installation_ids", [])
            ],
            accounts=[str(value) for value in raw.get("accounts", [])],
            is_app_owner=bool(raw.get("is_app_owner")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def require_viewer(request: Request) -> Viewer:
    """FastAPI dependency. 401 for anyone not signed in.

    401 rather than 403: the caller has not identified themselves at all, and
    the fix is to log in. The body names where, so the frontend does not have
    to hardcode the route.
    """
    viewer = current_viewer(request)
    if viewer is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "not_signed_in",
                "message": "Sign in with GitHub to see diagnoses.",
                "login_url": "/login",
            },
        )
    return viewer


def _scope(viewer: Viewer) -> tuple[list[int], bool]:
    """(installation ids to show, whether the legacy NULL rows are included).

    The intersection against the database is the freshness check on a cookie
    that could be eight hours old - see existing_installation_ids in db.py.

    THE LEGACY-ROW POLICY, stated once, here. Five diagnoses predate Phase 11
    and carry no installation id. They are shown only to the account that
    registered the App. The alternatives were both worse: hiding them from
    everyone silently removes five real diagnoses from every total, and
    showing them to everyone hands buildDocterRepo1 log excerpts to the next
    stranger who installs BuildDoctor. There is no way to attribute them
    after the fact, so they belong to the one account that can be proven to
    own them.
    """
    return (
        db.existing_installation_ids(viewer.installation_ids),
        viewer.is_app_owner,
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("/me")
def me(request: Request) -> dict:
    """Who the browser is signed in as. The one route that allows everybody.

    Deliberately NOT behind require_viewer: the frontend calls this first to
    decide whether to render a login button or a dashboard, and a 401 there
    would be the normal case rather than an error. It discloses nothing to a
    signed-out caller beyond signed_in: false.
    """
    viewer = current_viewer(request)
    if viewer is None:
        return {"signed_in": False}

    installation_ids, include_legacy = _scope(viewer)

    # The approval state of each one, so the frontend can tell an installer
    # who is waiting on a human apart from one whose dashboard is simply
    # empty. Before Phase 13 both looked identical from the browser - an
    # empty table - which is the "silent skipping" this route now fixes.
    rows = db.installations_for(installation_ids)
    installations = [
        {
            "installation_id": row.installation_id,
            "account_login": row.account_login,
            "account_type": row.account_type,
            "is_allowed": row.is_allowed,
        }
        for row in rows
    ]

    return {
        "signed_in": True,
        "login": viewer.login,
        "avatar_url": viewer.avatar_url,
        "installations": installations,
        # What GitHub said at login, against what is still true now. When
        # these differ, an installation went away mid-session, or its
        # `created` webhook never landed - worth being able to see rather
        # than silently reconciling.
        "installations_at_login": len(viewer.installation_ids),
        "is_app_owner": viewer.is_app_owner,
        "includes_legacy_rows": include_legacy,
        # Waiting on somebody, as opposed to having nothing yet. False for a
        # viewer with no installations at all: they are not pending, they
        # have not installed anything, and telling them to wait would be
        # wrong. Note this is a PRESENTATION fact, not an access control -
        # is_allowed gates whether builds get diagnosed, and deliberately
        # does not hide diagnoses that already exist.
        "pending_approval": bool(installations)
        and not any(item["is_allowed"] for item in installations),
    }


# Plain `def`, not `async def`, and on purpose. db.py is synchronous, so an
# async route would block the event loop for the length of the query.
# FastAPI runs a sync route on a worker thread instead, which is the same
# thing main.py does by hand with asyncio.to_thread.
@router.get("/stats")
def stats(viewer: Viewer = Depends(require_viewer)) -> dict:
    """Totals for the cards at the top of the page, for this viewer only."""
    installation_ids, include_legacy = _scope(viewer)
    return db.dashboard_stats(installation_ids, include_legacy=include_legacy)


@router.get("/diagnoses")
def diagnoses(
    viewer: Viewer = Depends(require_viewer),
    limit: int = Query(
        db.DASHBOARD_DEFAULT_LIMIT,
        ge=1,
        le=db.DASHBOARD_MAX_LIMIT,
        description="Most recent N diagnoses. See the note in db.py: this "
        "is a ceiling, not pagination, and it will need to become "
        "pagination once the table is large.",
    ),
) -> dict:
    """This viewer's diagnoses, newest first."""
    installation_ids, include_legacy = _scope(viewer)
    rows = db.list_diagnoses(
        installation_ids, include_legacy=include_legacy, limit=limit
    )
    return {
        "count": len(rows),
        "limit": limit,
        "diagnoses": [_serialise(row, embedded) for row, embedded in rows],
    }
