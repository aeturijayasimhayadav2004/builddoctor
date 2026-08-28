"""Approving installations (Phase 13).

WHY THIS IS NOT IN dashboard.py

dashboard.py opens by promising that every route in it reads, and that
nothing there writes, deletes, re-runs or posts. That promise is worth
something: it is why the module can be reviewed by reading its docstring
rather than its routes. This file contains the one route in the whole
service that a browser can use to change a row, so it goes somewhere the
promise is not being made.

WHAT PHASE 13 ACTUALLY CHANGED, AND WHAT IT DELIBERATELY DID NOT

The gate itself is untouched. installation_is_allowed() in db.py still
decides, still fails closed, and is still consulted on every delivery. The
pipeline still skips an unapproved installation in the same place for the
same reason. None of the enforcement moved.

What moved is how a row gets INTO is_allowed = true. That used to be a
comma-separated environment variable read at install time, which meant
approving somebody required an edit, a commit and a redeploy - and on this
free tier a redeploy is a cold start, so granting access briefly took the
service down. Now it is a button, and the row it writes takes effect on the
next webhook with nothing restarted.

WHO IS ALLOWED TO PRESS IT

The account that registered the App, established by asking GitHub - the
`owner` of GET /app, compared against the signed-in user's id. This is the
same rule the rest of the project already follows: the pipeline re-reads
run_attempt from the API instead of counting attempts itself, and sign-in
reads GET /user/installations instead of inferring administrator status
from webhook payloads. Ask the system that owns the fact.

The alternative was an ADMIN_ACCOUNTS environment variable, and it was
rejected for the same reasons the allowlist variable it would have replaced
was rejected. A login in a config file goes stale the moment the account is
renamed; it has to be kept identical in .env and in the Render dashboard,
which are two places that can disagree without anyone noticing; and it is
one typo away from either locking the owner out or letting somebody else
in. GET /app cannot drift, because it is not a copy.

THE ONE LIMITATION, STATED RATHER THAN DESIGNED AROUND

GET /app returns an organisation as the owner if an App is transferred to
one. No individual user id would then match, and nobody could approve
anything through this view. That is not reachable without a manual transfer
on GitHub, and the recovery is an UPDATE against the installations table.
Building an escape hatch for a situation that requires a deliberate human
action elsewhere would be speculative, so this is documented instead.

CROSS-SITE REQUESTS

There is no CSRF token here, and the session cookie is the reason why. It
is SameSite=Lax, which means the browser will not attach it to a POST that
originates from another site - the request arrives with no session at all
and is rejected as unauthenticated before it reaches any of this. A CSRF
token would be a second lock on the same door.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import dashboard
import db

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_app_owner(request: Request) -> dashboard.Viewer:
    """FastAPI dependency. Only the account that registered the App.

    Two different failures, two different statuses, and the distinction is
    not pedantry: 401 means "you have not said who you are, go and log in",
    which the frontend turns into a sign-in button. 403 means "we know
    exactly who you are and it is not you", where logging in again would
    achieve nothing. Collapsing them would send an ordinary installer round
    a sign-in loop that could never succeed.
    """
    viewer = dashboard.current_viewer(request)
    if viewer is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "not_signed_in",
                "message": "Sign in with GitHub first.",
                "login_url": "/login",
            },
        )
    if not viewer.is_app_owner:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "not_app_owner",
                "message": "Only the account that registered BuildDoctor "
                "can approve installations.",
            },
        )
    return viewer


class ApprovalRequest(BaseModel):
    """The body of an approve/revoke call.

    `allowed` is explicit rather than the route being POST /approve, because
    an approve button with no way back is a worse product than one with two
    directions: the realistic mistake is approving the wrong row, and the
    fix for it should not be a psql session. Both directions call the same
    db function that already existed, so revoking adds no new machinery.
    """

    allowed: bool


def _serialise(row: db.Installation, diagnosis_count: int) -> dict:
    return {
        "installation_id": row.installation_id,
        "account_login": row.account_login,
        # "User" or "Organization". Null on rows recorded before this was
        # captured, passed through rather than guessed at.
        "account_type": row.account_type,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "is_allowed": row.is_allowed,
        # How much work has actually been done for this installation. A
        # pending row with diagnoses behind it means it was approved once
        # and revoked, which is worth seeing before approving it again.
        "diagnoses": diagnosis_count,
    }


@router.get("/installations")
def installations(_: dashboard.Viewer = Depends(require_app_owner)) -> dict:
    """Every installation on record, pending ones first.

    Not filtered to is_allowed = false, even though pending rows are the
    only ones with a button. A list showing only what needs a decision hides
    the consequences of past decisions - there would be no way to see what
    you have already approved, and no way to revoke it. Sorting puts the
    pending ones at the top instead.

    Plain `def`, not `async def`: db.py is synchronous, so an async route
    would block the event loop for the length of the query. FastAPI runs a
    sync route on a worker thread.
    """
    rows = db.list_installations()
    counts = db.diagnosis_counts_by_installation()
    items = [_serialise(row, counts.get(row.installation_id, 0)) for row in rows]

    # Newest first, then pending first. Two passes rather than one compound
    # key because Python's sort is stable and a date cannot be reversed
    # inside a tuple key without turning it into a number first.
    items.sort(key=lambda item: item["created_at"] or "", reverse=True)
    items.sort(key=lambda item: item["is_allowed"])

    return {
        "count": len(items),
        "pending": sum(1 for item in items if not item["is_allowed"]),
        "installations": items,
    }


@router.post("/installations/{installation_id}/allowed")
def set_allowed(
    installation_id: int,
    body: ApprovalRequest,
    viewer: dashboard.Viewer = Depends(require_app_owner),
) -> dict:
    """Flip one installation's gate. Takes effect on the next webhook.

    404 rather than a silent success when the row is gone, because the
    realistic cause is that the App was uninstalled while this page was
    open - remove_installation deletes the row outright - and reporting
    "approved" for something that no longer exists would be a lie the UI
    then displays.
    """
    changed = db.set_installation_allowed(installation_id, body.allowed)
    if not changed:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no_such_installation",
                "message": f"Installation {installation_id} is not on record. "
                "It may have been uninstalled since this page loaded.",
            },
        )

    row = db.get_installation(installation_id)
    verb = "APPROVED" if body.allowed else "REVOKED"
    print(
        f"  {verb}: installation {installation_id} "
        f"({row.account_login if row else '?'}) by {viewer.login}",
        flush=True,
    )

    counts = db.diagnosis_counts_by_installation()
    return {
        "ok": True,
        "installation": _serialise(row, counts.get(installation_id, 0)),
    }
