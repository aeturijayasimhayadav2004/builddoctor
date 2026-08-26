"""Postgres storage for diagnoses.

Everything that touches SQL lives here, the same way everything that touches
the GitHub API lives in github_client.py. The rest of the app calls
save_diagnosis() and never sees a session.

Deliberately synchronous. This is one INSERT per failed build, so an async
driver would add a whole second connection stack to avoid blocking for a
couple of milliseconds. The pipeline is async, so it wraps the call in
asyncio.to_thread instead - the event loop stays free and these same plain
functions are reusable from the migration script and from a shell.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

# Dimensions of the vector all-MiniLM-L6-v2 produces. Declared here because
# it is a property of the COLUMN - Postgres needs a fixed width - and
# embeddings.py asserts against it so a model swap fails loudly instead of
# writing vectors the column silently rejects.
EMBEDDING_DIM = 384

# Inside Docker the host is the compose SERVICE NAME ("postgres"), not
# localhost - see docker-compose.yml. The default here is the value that
# works when running uvicorn directly on the machine, against the postgres
# container's published port.
# Port 5433, not 5432: docker-compose.yml publishes Postgres there to
# avoid colliding with a natively installed PostgreSQL service.
DEFAULT_URL = (
    "postgresql+psycopg://builddoctor:builddoctor@localhost:5433/builddoctor"
)


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip() or DEFAULT_URL


# github_client speaks in terms of what it posted TO ("pull_request" /
# "commit"); the column records what KIND of comment was left. One mapping,
# in the module that owns the column, so main.py and the migration script
# cannot drift apart.
POSTED_TO = {
    "pull_request": "pr_comment",
    "commit": "commit_comment",
}


def posted_to_column(target: str | None) -> str | None:
    """Translate a github_client target into a posted_to value."""
    if not target:
        return None
    return POSTED_TO.get(target, target)


class Base(DeclarativeBase):
    pass


class Diagnosis(Base):
    """One diagnosis of one failed workflow run."""

    __tablename__ = "diagnoses"

    # Surrogate key. run_id is not the primary key because re-running a
    # failed workflow produces a second diagnosis under the same run id.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # BigInteger is mandatory, not defensive: real run ids are around
    # 3.3e10, and a 32-bit INTEGER stops at 2147483647.
    run_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)

    repo: Mapped[str] = mapped_column(String(255), index=True, nullable=False)

    # TIMESTAMPTZ. Stores an absolute instant rather than ambiguous
    # wall-clock digits. Defaulted by the database so every row shares one
    # clock, whichever machine inserted it.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
        nullable=False,
    )

    log_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Already a dict: files_changed / lines_added / lines_removed. JSONB
    # keeps it queryable instead of forcing a re-parse on every read.
    diff_summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    diagnosis_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # "pr_comment" | "commit_comment" | NULL. Nullable because posting can
    # fail after the diagnosis succeeded, and NULL is the honest value for
    # "diagnosed, never delivered".
    posted_to: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Phase 4 populates this. Declared now so Phase 4 does not have to
    # alter a table that already holds data.
    lane: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)

    # The log excerpt, turned into 384 numbers by embeddings.py. This is
    # what makes the row findable by MEANING rather than by keyword: two
    # failures worded differently but describing the same thing end up
    # close together in this space.
    #
    # Nullable, and that is load-bearing. Rows 1-11 predate Phase 6 and
    # start NULL until backfill_embeddings.py fills them in, and a future
    # row could fail to embed. Every search filters on IS NOT NULL, so a
    # missing embedding means "not searchable yet", never a broken query.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=True
    )

    # Everything the pipeline records that has no column of its own:
    # run_url, posted_url, failed_jobs, workflow, model, diff_source,
    # diff_ref, failed_step. Kept verbatim so nothing is lost; a field can
    # be promoted to a real column later, when something needs to query it.
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    def __repr__(self) -> str:
        return (
            f"<Diagnosis id={self.id} run_id={self.run_id} "
            f"repo={self.repo!r} posted_to={self.posted_to!r}>"
        )


# pool_pre_ping issues a cheap SELECT 1 before handing out a pooled
# connection. Without it, a connection that died while the app sat idle
# (container restart, postgres restart) surfaces as a failed INSERT on the
# next real build instead of being quietly replaced.
_engine = create_engine(database_url(), pool_pre_ping=True, future=True)
Session = sessionmaker(bind=_engine, expire_on_commit=False)


def wait_for_database(attempts: int = 30, delay: float = 1.0) -> None:
    """Block until Postgres accepts a connection.

    Under docker compose the app container can start before Postgres is
    ready to serve. Compose's healthcheck covers the normal case; this
    covers the rest, and turns a crash loop into a few seconds of waiting.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with _engine.connect() as conn:
                conn.execute(select(1))
            return
        except OperationalError as exc:
            last = exc
            print(f"  database not ready ({attempt}/{attempts}), retrying ...")
            time.sleep(delay)
    raise RuntimeError(f"database unreachable after {attempts} attempts: {last}")


# Hand-written migration statements, run in order on every startup.
#
# Phase 3 said create_all only ever CREATEs, never ALTERs - and that is
# exactly why it cannot deliver Phase 6. The diagnoses table already exists
# with eleven rows in it, so create_all looks at it, sees a table by that
# name, and moves on. The new column would never appear.
#
# So: one explicit statement per change, each written to be safe to run
# again. IF NOT EXISTS on both means the second boot and the two-hundredth
# boot do nothing at all.
#
# Order matters. The `vector` TYPE does not exist until the extension
# creating it has been installed, so the ALTER cannot come first.
#
# This works because Phase 6's change is purely ADDITIVE - a new nullable
# column. The day a column has to be renamed, retyped, or backfilled with
# a real default while holding data, this style stops being enough, and
# that is the day Alembic earns its place.
MIGRATIONS = (
    ("enable pgvector", "CREATE EXTENSION IF NOT EXISTS vector"),
    (
        "add diagnoses.embedding",
        f"ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS "
        f"embedding vector({EMBEDDING_DIM})",
    ),
)


def init_db() -> None:
    """Create the tables if they are missing, then apply the migrations.

    Three steps, in this order:
      1. the extension, because create_all cannot build a vector column
         without the vector type existing,
      2. create_all, which builds the whole table on a brand new database,
      3. the ALTER, which is what actually adds the column to a database
         that already had the table. A no-op on a fresh one.
    """
    with _engine.begin() as conn:
        conn.execute(text(MIGRATIONS[0][1]))

    Base.metadata.create_all(_engine)

    with _engine.begin() as conn:
        for label, statement in MIGRATIONS[1:]:
            conn.execute(text(statement))
            print(f"  migration ok: {label}")


def save_diagnosis(
    *,
    run_id: int,
    repo: str,
    log_excerpt: str,
    diff_summary: dict,
    diagnosis_text: str,
    posted_to: str | None,
    lane: str | None = None,
    created_at: datetime | None = None,
    raw: dict | None = None,
    embedding: list[float] | None = None,
) -> int:
    """Insert one diagnosis. Returns its new primary key.

    Keyword-only: every argument here is a string or a dict, and positional
    calls would be silently reorderable.
    """
    row = Diagnosis(
        run_id=run_id,
        repo=repo,
        log_excerpt=log_excerpt or "",
        diff_summary=diff_summary or {},
        diagnosis_text=diagnosis_text or "",
        posted_to=posted_to,
        lane=lane,
        raw=raw or {},
        # Stored for EVERY lane, not only the ones that comment. Memory is
        # about what failures look like, and an amber re-run is exactly as
        # worth remembering as a teal explanation.
        embedding=embedding,
    )
    # Only set when backfilling history; otherwise the server default wins.
    if created_at is not None:
        row.created_at = created_at

    with Session() as session:
        session.add(row)
        session.commit()
        return row.id


def nearest_by_embedding(
    vector: list[float],
    *,
    exclude_run_id: int | None = None,
    limit: int = 1,
) -> list[tuple[Diagnosis, float]]:
    """Rows closest to `vector`, nearest first, with a similarity score.

    Applies no threshold. This is the SQL half of the lookup and nothing
    else - deciding whether a match is GOOD ENOUGH is a judgement call, and
    it lives in memory.py where it can be explained and tuned.

    Cosine distance (pgvector's `<=>`) measures the ANGLE between two
    vectors and ignores their length, which is what we want: a 400-line log
    and a 40-line log describing the same failure point the same direction.
    Similarity is 1 - distance, so 1.0 is identical and 0.0 is unrelated.

    exclude_run_id keeps a run from matching itself. Redelivering a webhook
    re-processes the same run and would otherwise score ~1.00 against the
    row it just wrote - a perfect match that teaches nothing. Memory should
    be about OTHER builds.
    """
    distance = Diagnosis.embedding.cosine_distance(vector)
    stmt = (
        select(Diagnosis, distance.label("distance"))
        # Rows that predate Phase 6, or whose embedding failed, are simply
        # not searchable. They are skipped, never treated as distance 0.
        .where(Diagnosis.embedding.isnot(None))
        .order_by(distance)
        .limit(limit)
    )
    if exclude_run_id is not None:
        stmt = stmt.where(Diagnosis.run_id != exclude_run_id)

    with Session() as session:
        return [(row, 1.0 - float(dist)) for row, dist in session.execute(stmt)]


def rows_missing_embeddings() -> list[Diagnosis]:
    """Rows that have text but no vector. The backfill script's input."""
    with Session() as session:
        stmt = (
            select(Diagnosis)
            .where(Diagnosis.embedding.is_(None))
            .order_by(Diagnosis.id)
        )
        return list(session.scalars(stmt))


def set_embedding(row_id: int, vector: list[float]) -> None:
    """Attach a vector to an existing row. Touches nothing else.

    Deliberately narrow: it reads `id` and writes `embedding`, so a row
    with odd data in an unrelated column - a NULL posted_url, say - is
    backfilled exactly like any other.
    """
    with Session() as session:
        row = session.get(Diagnosis, row_id)
        if row is None:
            return
        row.embedding = vector
        session.commit()


def recent(limit: int = 20) -> list[Diagnosis]:
    """Newest diagnoses first. Used for eyeballing the table from a shell."""
    with Session() as session:
        stmt = select(Diagnosis).order_by(Diagnosis.created_at.desc()).limit(limit)
        return list(session.scalars(stmt))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
