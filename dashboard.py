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
deletes, re-runs, or posts anything, which is what makes it safe to leave
open to a browser with no authentication in front of it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

import db

router = APIRouter(prefix="/api", tags=["dashboard"])

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


# Plain `def`, not `async def`, and on purpose. db.py is synchronous, so an
# async route would block the event loop for the length of the query.
# FastAPI runs a sync route on a worker thread instead, which is the same
# thing main.py does by hand with asyncio.to_thread.
@router.get("/stats")
def stats() -> dict:
    """Totals for the cards at the top of the page."""
    return db.dashboard_stats()


@router.get("/diagnoses")
def diagnoses(
    limit: int = Query(
        db.DASHBOARD_DEFAULT_LIMIT,
        ge=1,
        le=db.DASHBOARD_MAX_LIMIT,
        description="Most recent N diagnoses. See the note in db.py: this "
        "is a ceiling, not pagination, and it will need to become "
        "pagination once the table is large.",
    ),
) -> dict:
    """Every diagnosis, newest first."""
    rows = db.list_diagnoses(limit=limit)
    return {
        "count": len(rows),
        "limit": limit,
        "diagnoses": [_serialise(row, embedded) for row, embedded in rows],
    }
