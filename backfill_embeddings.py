"""One-time: embed the rows that were stored before Phase 6 existed.

Rows 1-11 were written by Phases 3-5, which had no embedding column, so
they arrive with embedding = NULL and are invisible to memory. Without
this, BuildDoctor would only ever remember failures from today onwards and
the whole demo history would be missing from its memory.

Safe to run repeatedly. By default it selects only rows WHERE embedding IS
NULL, so a second run finds nothing and does nothing. Same idempotency
rule as migrate_jsonl.py in Phase 3.

    --all  RE-EMBED EVERY ROW, including rows that already have a vector.

Needed whenever embeddings.clean() changes, which Phase 8.5 did. A vector
computed from differently-cleaned text is STALE even though it is not
NULL, and a table holding a mixture of old and new vectors produces
similarity scores that mean nothing - every row has to be computed the
same way, or none of them can be compared.

--all OVERWRITES existing data. It is recoverable, because the log
excerpts it reads from are never touched and it can simply be run again -
but it is not a no-op, which is why it is not the default.

Narrow on purpose: it reads `id` and `log_excerpt`, and writes `embedding`.
Nothing else is looked at, which is why row 10 - whose posted_url is NULL
because of the Phase 5 structured-output bug - is backfilled exactly like
every other row. That NULL is a record of a bug in a different column and
has nothing to do with what the log said.

Run it inside the app container, where the model weights live:

    docker compose exec app python backfill_embeddings.py
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402
import embeddings  # noqa: E402


def main() -> int:
    db.wait_for_database()
    db.init_db()

    full = "--all" in sys.argv

    if full:
        rows = db.all_rows_for_embedding()
        if not rows:
            print("The table is empty - nothing to embed.")
            return 0
        already = sum(1 for r in rows if r.embedding is not None)
        print(
            f"FULL RE-EMBED: {len(rows)} row(s), of which {already} already "
            f"have a vector that will be OVERWRITTEN.\n"
        )
    else:
        rows = db.rows_missing_embeddings()
        if not rows:
            print(
                "Nothing to backfill - every row already has an embedding.\n"
                "If embeddings.clean() has changed, those vectors are stale "
                "rather than missing; re-run with --all."
            )
            return 0
        print(f"Backfilling {len(rows)} row(s) with no embedding.\n")

    embeddings.warm()

    done = 0
    skipped = 0
    failed = 0

    for row in rows:
        text = (row.log_excerpt or "").strip()
        if not text:
            # An empty excerpt embeds to a meaningless vector that would
            # then sit in the table looking like a real memory. Leaving it
            # NULL keeps it honestly unsearchable.
            print(f"  id={row.id:<3} SKIP  - no log excerpt to embed")
            skipped += 1
            continue

        try:
            vector = embeddings.embed(text)
            db.set_embedding(row.id, vector)
        except Exception as exc:  # noqa: BLE001
            # One bad row must not abandon the other ten.
            print(f"  id={row.id:<3} FAIL  - {type(exc).__name__}: {exc}")
            failed += 1
            continue

        print(
            f"  id={row.id:<3} ok    run={row.run_id} "
            f"lane={row.lane or '-':<13} chars={len(text)}"
        )
        done += 1

    print(f"\nEmbedded {done}, skipped {skipped}, failed {failed}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
