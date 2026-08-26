"""BuildDoctor's memory: has something like this failed before?

Called before the model is asked anything. If a genuinely similar past
failure exists, its diagnosis is handed to the model as CONTEXT - a hint,
not an instruction.

WHY THIS IS NOT AN MCP TOOL

Phase 5 exposed four capabilities over MCP and deliberately exposed no
others. The dividing line was reads versus writes:

    writes  post a comment, add a label, re-run a job. These change the
            outside world, they are what an AI client would want to be
            able to do on our behalf, and each one needs its "do not
            retry" rule published where a client can see it.

    reads   list the failed jobs, download a log, fetch a diff. These are
            BuildDoctor's own evidence gathering. Nobody else needs them,
            they change nothing, and their retry rule lives in exactly one
            place - the @_reads decorator in github_client.

This lookup is a read. It changes nothing, it exists so that BuildDoctor
can gather better evidence about a failure it is already investigating,
and it has no side effect worth publishing a hint about. Putting it behind
MCP would add a network hop, a serialisation format, and a second thing
that can be down, and would buy nothing - so it stays an ordinary function
call, on the same reasoning that kept the reads out in Phase 5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import db
import embeddings

# HOW SIMILAR IS SIMILAR ENOUGH.
#
# Cosine similarity runs from 1.0 (identical meaning) down towards 0.0
# (unrelated). This number was measured, not guessed. The eleven rows
# already in the database fall into four groups that are known to be the
# same underlying failure - a failed assertion, a missing test directory,
# an import of a module that does not exist, and a pinned package version
# that does not exist - so every pair has a right answer. Measured:
#
#     pairs that SHOULD match     0.994 .. 1.000
#     pairs that should NOT       0.321 .. 0.811
#
# 0.811 is the important number. It is a failed assertion compared against
# a failed import: two completely different problems that happen to be the
# same shape of pytest output. Anything at or below 0.85 would let that
# through, which is why the first draft of this file - 0.80 - was wrong.
#
# 0.90 sits near the middle of the empty band between the two ranges, so
# neither side is favoured, and it is far from both edges rather than
# tuned to just clear one of them.
#
# One honest limitation: every true pair above is the SAME fixture failing
# twice, so 0.994 is the floor for identical failures, not for merely
# similar ones. There is no example yet of "similar but not identical", so
# the low side of the band is where the real uncertainty lives.
#
# That uncertainty is deliberately resolved towards silence, because the
# costs are not symmetric. A decent-but-not-great match must return
# NOTHING. A weak hint does not arrive labelled as weak - it arrives in
# the prompt looking exactly as authoritative as a strong one, and it can
# pull a correct diagnosis towards a failure that never happened. Missing
# a match costs one ordinary Phase 5 diagnosis. A false match costs a
# wrong one.
SIMILARITY_THRESHOLD = 0.90

# How much of a past diagnosis to quote into the prompt. Enough to be
# useful, short enough that the actual evidence stays the biggest thing in
# the message.
MAX_SUMMARY_CHARS = 700


@dataclass
class PastFailure:
    """A previous diagnosis that resembles the one being investigated."""

    row_id: int
    run_id: int
    repo: str
    created_at: datetime
    lane: str | None
    diagnosis_text: str
    similarity: float

    def summary(self) -> str:
        """The few lines that go into the prompt."""
        when = self.created_at.strftime("%Y-%m-%d") if self.created_at else "unknown"
        text = (self.diagnosis_text or "").strip()[:MAX_SUMMARY_CHARS]
        return (
            f"Date: {when}\n"
            f"Repository: {self.repo}\n"
            f"Workflow run: {self.run_id}\n"
            f"How it was handled: {self.lane or 'unrecorded'}\n"
            f"Similarity to the current failure: {self.similarity:.2f}\n"
            f"What was concluded then:\n{text}"
        )


def search_past_failures(
    log_excerpt: str,
    *,
    exclude_run_id: int | None = None,
    threshold: float = SIMILARITY_THRESHOLD,
) -> PastFailure | None:
    """The closest past diagnosis, or None if nothing is close enough.

    Synchronous, like everything in db.py, and for the same reason: it is
    one query plus a few milliseconds of CPU. The async pipeline calls it
    through asyncio.to_thread.

    Returns None - not a best guess - whenever the nearest match falls
    below the threshold. That is the whole point of having a threshold.
    """
    vector = embeddings.embed(log_excerpt)
    matches = db.nearest_by_embedding(vector, exclude_run_id=exclude_run_id, limit=1)
    if not matches:
        print("  memory: nothing stored to compare against yet")
        return None

    row, similarity = matches[0]

    if similarity < threshold:
        # Say the number out loud. When a match is rejected, the useful
        # question is always "by how much", and printing it is what makes
        # the threshold tunable from evidence instead of from opinion.
        print(
            f"  memory: closest is row {row.id} at {similarity:.2f}, "
            f"below the {threshold:.2f} threshold - treating as no match"
        )
        return None

    print(
        f"  memory: matched row {row.id} (run {row.run_id}, "
        f"lane={row.lane}) at similarity {similarity:.2f}"
    )
    return PastFailure(
        row_id=row.id,
        run_id=row.run_id,
        repo=row.repo,
        created_at=row.created_at,
        lane=row.lane,
        diagnosis_text=row.diagnosis_text,
        similarity=similarity,
    )
