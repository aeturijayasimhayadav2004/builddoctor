"""One-time backfill: diagnoses.jsonl -> Postgres.

Run once, after the database exists:

    python migrate_jsonl.py

Safe to run again. Rows are matched on (run_id, created_at), so a second
run inserts nothing rather than duplicating the history.

The .jsonl file is never modified or deleted. Delete it yourself once you
have seen the rows in Postgres - a one-time script that destroys its own
input leaves nothing to retry with.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402  (must come after load_dotenv: db reads DATABASE_URL)

from sqlalchemy import select  # noqa: E402

JSONL_PATH = Path(__file__).parent / "diagnoses.jsonl"

# Fields that have a column of their own. Everything else in the record
# goes into the raw JSONB column instead of being dropped.
PROMOTED = {
    "run_id",
    "repo",
    "timestamp",
    "log_excerpt",
    "diff_summary",
    "diagnosis",
    "posted_to",
}


def parse_timestamp(value: str | None) -> datetime:
    """Read the ISO timestamp the old recorder wrote, always as UTC-aware."""
    if not value:
        return datetime.now(timezone.utc)
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def already_present(session, run_id: int, created_at: datetime) -> bool:
    stmt = select(db.Diagnosis.id).where(
        db.Diagnosis.run_id == run_id,
        db.Diagnosis.created_at == created_at,
    )
    return session.scalar(stmt) is not None


def main() -> int:
    if not JSONL_PATH.exists():
        print(f"  nothing to migrate: {JSONL_PATH.name} does not exist")
        return 0

    print(f"  database: {db.database_url()}")
    db.wait_for_database()
    db.init_db()

    inserted = skipped = bad = 0

    with db.Session() as session:
        for number, line in enumerate(
            JSONL_PATH.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"  line {number}: not valid JSON, skipped ({exc})")
                bad += 1
                continue

            run_id = record.get("run_id")
            repo = record.get("repo")
            if run_id is None or not repo:
                print(f"  line {number}: missing run_id or repo, skipped")
                bad += 1
                continue

            created_at = parse_timestamp(record.get("timestamp"))

            if already_present(session, int(run_id), created_at):
                print(f"  line {number}: run {run_id} already in the table, skipped")
                skipped += 1
                continue

            row = db.Diagnosis(
                run_id=int(run_id),
                repo=repo,
                created_at=created_at,
                log_excerpt=record.get("log_excerpt") or "",
                diff_summary=record.get("diff_summary") or {},
                # The old file called this "diagnosis"; the column is
                # "diagnosis_text" so it does not collide with the model
                # class name in queries.
                diagnosis_text=record.get("diagnosis") or "",
                posted_to=db.posted_to_column(record.get("posted_to")),
                lane=None,  # Phase 4 fills this in; history predates it.
                raw={k: v for k, v in record.items() if k not in PROMOTED},
            )
            session.add(row)
            session.flush()  # assigns row.id without ending the transaction
            print(
                f"  line {number}: inserted id={row.id} run={run_id} "
                f"posted_to={row.posted_to}"
            )
            inserted += 1

        session.commit()

    total = inserted + skipped + bad
    print(f"\n  {total} record(s) read: {inserted} inserted, {skipped} already there, {bad} unusable")

    for row in reversed(db.recent(limit=10)):
        print(
            f"    id={row.id:<3} {row.created_at:%Y-%m-%d %H:%M} "
            f"run={row.run_id} {row.posted_to or '(not posted)':<15} "
            f"lane={row.lane}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
