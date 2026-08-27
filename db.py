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
    Boolean,
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
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    defer,
    mapped_column,
    sessionmaker,
)

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

    # Which GitHub App installation this diagnosis was produced for.
    #
    # NULLABLE, permanently. Rows 1-5 were made by the old static personal
    # access token, which had no installation behind it at all, and NULL is
    # the honest value for "predates the App" - not zero, and not a guess.
    # Every row written from Phase 11 onward carries a real id.
    #
    # Phase 9 flagged this column as the thing multi-tenant filtering will
    # need. Phase 11 only POPULATES it: nothing reads it as a filter yet, so
    # /api/diagnoses still returns every row to everyone. Making the data
    # correct before anything depends on it means the filter, when it lands,
    # is a WHERE clause rather than a backfill.
    #
    # BigInteger for the same reason run_id is: these ids are already
    # eight digits and there is no reason to bet on the ceiling.
    installation_id: Mapped[int | None] = mapped_column(
        BigInteger, index=True, nullable=True
    )

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


class Installation(Base):
    """One place the GitHub App has been installed.

    WHY is_allowed IS A COLUMN AND NOT AN ENV VAR

    The obvious cheap version of a gate is a comma-separated env var read at
    request time - and ALLOWED_ACCOUNTS below is exactly that, so the cheap
    version does exist here. It is just not the thing the gate reads.

    The difference is what each one can answer. An env var can answer "should
    this account be let in by default", once, at install time. It cannot
    answer "is THIS installation allowed right now", because:

      * Changing it requires an edit and a redeploy. On Render's free tier a
        redeploy is a cold start, so revoking access to a misbehaving install
        would take the whole service down for a minute - the exact failure
        Phase 10 spent its time removing.
      * It cannot be changed from anywhere but a deploy pipeline, so nothing
        in the product can ever grant or revoke access. Phase 13 opening this
        up means an approval flow, and an approval flow needs somewhere to
        write "yes" that is not a source file.
      * It has no memory. A row records when an installation appeared, who
        owns it, and whether it was ever allowed, so a request that got
        skipped can be explained afterwards. An env var explains nothing.
      * Two accounts can legitimately have the same login at different times
        (an account gets deleted and the name is reused). An installation id
        does not move.

    So the env var seeds the decision and the column HOLDS it. Flipping one
    boolean in Postgres changes behaviour on the next webhook with no deploy,
    which is precisely what Phase 13 will need and what proving the gate
    works needs today.
    """

    __tablename__ = "installations"

    # GitHub's own id, used directly as the primary key rather than a
    # surrogate. It is globally unique, permanent for the life of the
    # installation, and it is the value that arrives in every webhook - so a
    # surrogate key would add a lookup and buy nothing.
    installation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # The user or organisation the App is installed on.
    #
    # A COPY, not the source of truth: an account can be renamed and this
    # column will then be stale until the next installation event refreshes
    # it. It is stored anyway because it is what a human reads when deciding
    # whether to allow something, and because the allowlist is expressed in
    # logins. The gate itself keys on installation_id, which cannot go stale.
    account_login: Mapped[str] = mapped_column(String(255), index=True, nullable=False)

    # "User" or "Organization", straight from the payload. Kept because the
    # two behave differently once multi-tenancy is real - an org install can
    # be administered by people other than whoever clicked install.
    account_type: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # THE GATE. False by default, and that default is the point: an
    # installation that nobody has explicitly approved does nothing.
    #
    # server_default is spelled as SQL "false" rather than a Python default
    # so that a row inserted by hand - psql, a migration, anything that is
    # not this file - also lands closed. A default that only exists in
    # Python is a default that can be bypassed.
    is_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )

    def __repr__(self) -> str:
        return (
            f"<Installation id={self.installation_id} "
            f"account={self.account_login!r} allowed={self.is_allowed}>"
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
    # Phase 11. Same additive shape as the Phase 6 column above, and additive
    # for the same reason: the five existing rows predate the App entirely, so
    # the honest value for them is NULL and no backfill is possible or wanted.
    #
    # The `installations` TABLE needs no statement here - it does not exist
    # yet, so create_all() builds it. Only a change to a table that already
    # holds data needs an ALTER, which is the distinction the note above
    # explains.
    (
        "add diagnoses.installation_id",
        "ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS "
        "installation_id BIGINT",
    ),
    (
        "index diagnoses.installation_id",
        "CREATE INDEX IF NOT EXISTS ix_diagnoses_installation_id "
        "ON diagnoses (installation_id)",
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
    installation_id: int | None = None,
) -> int:
    """Insert one diagnosis. Returns its new primary key.

    Keyword-only: every argument here is a string or a dict, and positional
    calls would be silently reorderable.

    installation_id defaults to None so the migration script and any older
    caller keep working unchanged; the pipeline always passes a real one.
    """
    row = Diagnosis(
        run_id=run_id,
        repo=repo,
        installation_id=installation_id,
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


# ---------------------------------------------------------------------------
# Installations and the allowlist (Phase 11)
# ---------------------------------------------------------------------------

# Comma-separated GitHub logins that are approved AT INSTALL TIME.
#
# This seeds is_allowed on a brand new row and is never consulted again - see
# the long note on the Installation model for why the column, not this
# variable, is what the gate actually reads.
#
# Empty means empty. There is no "allow everything" value on purpose: a
# misspelled or unset variable must fail CLOSED, and a wildcard is one typo
# away from opening the App to all of GitHub.
ALLOWED_ACCOUNTS_VAR = "ALLOWED_ACCOUNTS"


def allowed_accounts() -> set[str]:
    """Logins pre-approved by configuration, casefolded for comparison.

    GitHub treats logins case-insensitively, so "JaySmith" and "jaysmith" are
    the same account and the allowlist must not care which one was typed.
    """
    raw = os.environ.get(ALLOWED_ACCOUNTS_VAR, "")
    return {part.strip().casefold() for part in raw.split(",") if part.strip()}


def account_is_preapproved(login: str | None) -> bool:
    """Whether this account appears in ALLOWED_ACCOUNTS."""
    if not login:
        return False
    return login.strip().casefold() in allowed_accounts()


def get_installation(installation_id: int) -> Installation | None:
    with Session() as session:
        return session.get(Installation, installation_id)


def installation_is_allowed(installation_id: int | None) -> bool:
    """THE GATE. False for anything not explicitly recorded as allowed.

    Three cases collapse to False here, and collapsing them is deliberate:
    an installation that was never recorded, one recorded but not approved,
    and no installation id at all. The pipeline needs a yes/no, and every
    one of those three is a no. The caller logs WHICH it was; this decides.
    """
    if installation_id is None:
        return False
    row = get_installation(installation_id)
    return bool(row and row.is_allowed)


def upsert_installation(
    *,
    installation_id: int,
    account_login: str,
    account_type: str | None = None,
) -> tuple[Installation, bool]:
    """Record an installation. Returns the row and whether it was created.

    Not insert-only. GitHub sends `installation` events for permission
    changes and for suspend/unsuspend as well as for a fresh install, and a
    plain INSERT would raise a duplicate-key error on every one of those. It
    also re-sends `created` on a redelivery.

    IS_ALLOWED IS ONLY EVER SET ON CREATION. An existing row keeps whatever
    it holds, because that value may have been set by hand - which is exactly
    what Phase 13's approval flow will do, and exactly what proving the gate
    works does today. A later event overwriting it from the env var would
    silently undo a deliberate revocation.
    """
    with Session() as session:
        row = session.get(Installation, installation_id)
        created = row is None

        if created:
            row = Installation(
                installation_id=installation_id,
                account_login=account_login,
                account_type=account_type,
                is_allowed=account_is_preapproved(account_login),
            )
            session.add(row)
        else:
            # Refresh the descriptive fields - an account can be renamed -
            # and touch nothing else.
            row.account_login = account_login
            if account_type:
                row.account_type = account_type

        session.commit()
        session.refresh(row)
        return row, created


def remove_installation(installation_id: int) -> bool:
    """Delete an installation's row. Returns whether there was one.

    A hard delete, not a flag, and the reason is that the row cannot ever be
    useful again: GitHub issues a NEW installation id when an App is
    reinstalled, so this id is dead permanently. Keeping it would leave the
    allowlist full of entries that can never match anything.

    Nothing is lost by deleting it. There is deliberately NO foreign key from
    diagnoses.installation_id to this table - diagnoses must outlive the
    installation that produced them - so the history stays intact and still
    carries the id.
    """
    with Session() as session:
        row = session.get(Installation, installation_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True


def set_installation_allowed(installation_id: int, allowed: bool) -> bool:
    """Flip the gate for one installation. Returns whether the row existed.

    The write half of the reason is_allowed is a column: this takes effect on
    the very next webhook, with no deploy and no restart.
    """
    with Session() as session:
        row = session.get(Installation, installation_id)
        if row is None:
            return False
        row.is_allowed = allowed
        session.commit()
        return True


def list_installations() -> list[Installation]:
    """Every recorded installation, oldest first."""
    with Session() as session:
        return list(
            session.scalars(
                select(Installation).order_by(Installation.created_at)
            )
        )


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


def all_rows_for_embedding() -> list[Diagnosis]:
    """Every row, for a FULL re-embed.

    Added in Phase 8.5. rows_missing_embeddings() answers "what has never
    been embedded", which is the right question after a schema change and
    the wrong one after a CLEANING change: when embeddings.clean() changes,
    every stored vector is stale even though none of them is NULL.

    Ordered by id so a long run is readable and interruptible in a
    predictable place.
    """
    with Session() as session:
        return list(session.scalars(select(Diagnosis).order_by(Diagnosis.id)))


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


# ---------------------------------------------------------------------------
# Read-only queries for the dashboard (Phase 7).
#
# In this file, not in dashboard.py, for the reason at the top of the
# module: everything that touches SQL lives here. dashboard.py turns these
# results into JSON and never sees a SELECT.
#
# Every function below only reads. Phase 7 adds no way to change anything
# through the web layer, which is why none of them take a session or a
# commit.
# ---------------------------------------------------------------------------

# The three lanes, in the order the dashboard shows them. Hard-coded rather
# than discovered from the data, because a lane with zero rows still has to
# appear. "No failure has ever been auto-fixed" is a real fact about this
# project, and a GROUP BY would simply leave it out.
LANE_ORDER = ("informational", "safe_auto_fix", "needs_review")

# Three states, not two, and the difference matters.
#
# Rows 1-11 were written before Phase 6 existed, so `raw` has no
# memory_match KEY AT ALL. Every row written since has the key, set to null
# when nothing was close enough. Those are different facts - "memory was
# never asked" versus "memory was asked and said no" - and collapsing them
# would report a hit rate of 1-in-13 for a feature that has only ever run
# twice.
_MEMORY_ASKED = Diagnosis.raw.has_key("memory_match")

# jsonb_typeof returns the string 'object' for a real match and 'null' for
# a JSON null, which is how a hit is told apart from a miss. Comparing to
# NULL with = would not work: in SQL, null = null is not true.
_MEMORY_HIT = func.jsonb_typeof(Diagnosis.raw["memory_match"]) == "object"


def _percent(part: int, whole: int) -> float:
    """Share of the whole, rounded to one place. 0 when there is nothing."""
    if not whole:
        return 0.0
    return round(100.0 * part / whole, 1)


def dashboard_stats() -> dict:
    """Counts for the stat cards. One row of aggregates, plus the lanes."""
    with Session() as session:
        # count(*) FILTER (WHERE ...) - one pass over the table producing
        # every counter at once, instead of four separate queries that
        # could each see a slightly different moment.
        totals = session.execute(
            select(
                func.count().label("total"),
                func.count().filter(_MEMORY_ASKED).label("memory_asked"),
                func.count().filter(_MEMORY_HIT).label("memory_hits"),
                func.count()
                .filter(Diagnosis.embedding.isnot(None))
                .label("searchable"),
                func.count(func.distinct(Diagnosis.repo)).label("repos"),
                func.max(Diagnosis.created_at).label("latest_at"),
            )
        ).one()

        by_lane = dict(
            session.execute(
                select(Diagnosis.lane, func.count()).group_by(Diagnosis.lane)
            ).all()
        )

    total = int(totals.total or 0)

    # The known lanes first, in a fixed order, then whatever is left over.
    lanes = [
        {
            "lane": name,
            "count": int(by_lane.get(name, 0)),
            "percent": _percent(int(by_lane.get(name, 0)), total),
        }
        for name in LANE_ORDER
    ]

    # Rows 1-4 predate Phase 4 and have lane = NULL. They are real
    # diagnoses and they are counted, but they were never classified, so
    # they get their own bucket rather than being quietly dropped or
    # dumped into one of the three real lanes.
    unclassified = int(by_lane.get(None, 0))
    if unclassified:
        lanes.append(
            {
                "lane": None,
                "count": unclassified,
                "percent": _percent(unclassified, total),
            }
        )

    asked = int(totals.memory_asked or 0)
    hits = int(totals.memory_hits or 0)

    return {
        "total": total,
        "repos": int(totals.repos or 0),
        "latest_at": totals.latest_at.isoformat() if totals.latest_at else None,
        "lanes": lanes,
        "memory": {
            # Denominator, deliberately exposed. The rate alone would look
            # like a claim about all thirteen rows; shipping `asked` next
            # to it means the dashboard can say "1 of 2" and be honest.
            "asked": asked,
            "hits": hits,
            "rate": _percent(hits, asked),
            # How many rows memory can currently find, which is a different
            # question from how often it found one.
            "searchable": int(totals.searchable or 0),
        },
    }


# A sane ceiling, not pagination. There are thirteen rows, so paging would
# be machinery guarding against a problem that does not exist yet.
#
# LEFT HERE ON PURPOSE, AND IT WILL NEED REVISITING: once this table holds
# a few thousand rows, this endpoint starts shipping every log excerpt in
# the database on every page load. The fix at that point is a cursor on
# created_at plus a truncated excerpt in the list view, with the full text
# fetched only when a row is expanded.
DASHBOARD_DEFAULT_LIMIT = 100
DASHBOARD_MAX_LIMIT = 500


def list_diagnoses(limit: int = DASHBOARD_DEFAULT_LIMIT) -> list[tuple[Diagnosis, bool]]:
    """Newest diagnoses first, each paired with "does it have an embedding".

    created_at DESC, then id DESC: two diagnoses of the same run can land
    in the same second, and without the tiebreak their order on screen
    would be whatever Postgres felt like that day.

    The embedding column is DEFERRED - 384 floats per row that the browser
    has no use for - and a plain boolean is selected alongside instead. The
    boolean has to be selected explicitly, because asking a deferred
    attribute for its value would fetch the whole vector back, once per
    row, which is the thing being avoided.
    """
    with Session() as session:
        stmt = (
            select(Diagnosis, Diagnosis.embedding.isnot(None).label("embedded"))
            .options(defer(Diagnosis.embedding))
            .order_by(Diagnosis.created_at.desc(), Diagnosis.id.desc())
            .limit(limit)
        )
        return [(row, bool(embedded)) for row, embedded in session.execute(stmt)]
