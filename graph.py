"""The decision half of the pipeline, as a LangGraph state machine.

main.py gathers the evidence (which jobs failed, the log excerpt, the diff)
and hands it to this graph. The graph decides what to do about it and does
it. Recording the result is main.py's job again afterwards.

Shape of the graph:

    START -> classify -> (conditional) -> informational -> END
                                       -> safe_auto_fix  -> END
                                       -> needs_review   -> END

The three lanes, in the colour language used throughout the project:

    informational = TEAL   just explain it: post the diagnosis as a comment
    safe_auto_fix = AMBER  looks flaky: re-run the failed jobs
    needs_review  = CORAL  a human must look: label the PR and flag the
                           comment

Why a graph and not an if/elif: the routing becomes data - it can be
inspected, drawn, and each node kept independent - instead of being
control flow buried inside one long function.

Since Phase 5 the action nodes do not call GitHub themselves. They call
mcp_client, which speaks MCP over HTTP to a separate server process.
Failures still arrive as github_client.GitHubError, so the error handling
in these nodes is unchanged from Phase 4.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

import diagnose
import github_client
import mcp_client

# The label put on a pull request by the coral lane.
NEEDS_REVIEW_LABEL = "needs-review"

# Prefix used when the coral lane has no pull request to label. A commit
# comment cannot carry a label, so the distinction has to be visible in the
# text itself.
CORAL_PREFIX = "\U0001f6a9 NEEDS REVIEW - "

MAX_EXCERPT_IN_COMMENT = 5000


class BuildState(TypedDict, total=False):
    """Everything the run knows, and everything the nodes add to it.

    total=False because nodes fill this in progressively: the classify node
    is the first thing that knows a category, and only one of the three
    action nodes ever runs, so most keys are absent most of the time.

    A node returns a PARTIAL dict of just the keys it changed; LangGraph
    merges that into the state rather than the node mutating anything.
    """

    # --- provided by main.py before the graph starts ---
    payload: dict           # the raw webhook body
    repo: str               # owner/name
    run: dict               # payload["workflow_run"]
    run_id: int
    run_attempt: int        # GitHub's own counter: 1 = first try
    job_names: list
    first_step: str
    log_excerpt: str        # the trimmed log, the SYMPTOM
    diff: str               # the change that triggered the run, the CAUSE
    past_summary: str       # a similar past failure, or "" - a HINT only

    # --- added by the classify node ---
    diagnosis: str          # what the model said broke
    category: str           # what the model CHOSE
    reason: str             # why the model chose it
    lane: str               # what will actually RUN (guards may downgrade)
    guard_note: str         # set only when lane differs from category

    # --- added by whichever action node ran ---
    action: str             # short description of what was done
    posted: dict            # {"target", "ref", "url"} or empty
    labels: list            # labels added, if any
    rerun_requested: bool


# --------------------------------------------------------------------------
# Comment formatting
# --------------------------------------------------------------------------


def build_comment(state: BuildState, prefix: str = "") -> str:
    """Format the GitHub comment.

    The diagnosis goes first because that is what lands in a notification
    email. The evidence is collapsed underneath for whoever wants to check
    it.
    """
    run = state.get("run") or {}
    failed = ", ".join(f"`{name}`" for name in state.get("job_names") or []) or "unknown"
    excerpt = (state.get("log_excerpt") or "")[:MAX_EXCERPT_IN_COMMENT]
    fence = "```"
    return (
        f"## {prefix}BuildDoctor: `{run.get('name')}` failed\n\n"
        f"{state.get('diagnosis', '')}\n\n"
        f"---\n"
        f"**Failed job(s):** {failed}  \n"
        f"**Run:** [{run.get('id')}]({run.get('html_url')})  \n"
        f"**Lane:** `{state.get('lane')}` - {state.get('reason', '')}\n\n"
        f"<details>\n<summary>Log excerpt around the error</summary>\n\n"
        f"{fence}\n{excerpt}\n{fence}\n\n</details>\n\n"
        f"<sub>Posted automatically by BuildDoctor "
        f"(model: `{diagnose.MODEL}`). Not a human review.</sub>"
    )


async def _post(state: BuildState, prefix: str = "") -> dict:
    """Post the diagnosis where the person who broke the build is looking."""
    try:
        posted = await mcp_client.post_diagnosis(
            state["payload"], build_comment(state, prefix)
        )
        print(f"       posted as {posted['target']} comment on {posted['ref']}")
        print(f"       {posted['url']}")
        return posted
    except github_client.GitHubError as exc:
        print(f"       ERROR posting comment: {exc}")
        return {"target": None, "ref": None, "url": None}


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


async def classify(state: BuildState) -> dict:
    """Ask the model what broke and which lane this belongs in.

    Also applies the rerun guard, so that by the time the router runs, the
    lane in the state is one that is actually safe to execute.
    """
    print("\n  [classify] asking the model ...")
    triage = await diagnose.diagnose_failure(
        log_excerpt=state.get("log_excerpt", ""),
        diff=state.get("diff", ""),
        repo=state.get("repo", "unknown"),
        job_name=", ".join(state.get("job_names") or []) or "unknown",
        step_name=state.get("first_step", "unknown"),
        # Phase 6. Empty string when memory found nothing close enough, in
        # which case the prompt is byte-for-byte the Phase 4 prompt. This
        # is context for the DIAGNOSIS only - the categorising steps and
        # the guard below are unchanged and read nothing from it.
        past_summary=state.get("past_summary", ""),
    )

    lane = triage.category
    guard_note = ""

    # THE RERUN GUARD.
    #
    # run_attempt is GitHub's own counter for this run id: 1 on the first
    # try, 2 after any re-run, whoever triggered it. If it is already above
    # 1, this failure IS the result of a re-run, and re-running again would
    # start a loop that never ends: fail -> flaky -> rerun -> fail -> ...
    # with the same evidence producing the same verdict every time.
    #
    # Reading GitHub's counter beats keeping our own, because ours would
    # have to survive restarts, rebuilds and a wiped database - and it
    # would be blind to a human pressing "Re-run failed jobs" themselves.
    # This one is simply observed from the payload we already have.
    attempt = state.get("run_attempt") or 1
    if lane == diagnose.SAFE_AUTO_FIX and attempt > 1:
        guard_note = (
            f"classified {diagnose.SAFE_AUTO_FIX} but run_attempt={attempt}, "
            f"so this failure is already a re-run; downgraded to "
            f"{diagnose.INFORMATIONAL} to avoid a retry loop"
        )
        lane = diagnose.INFORMATIONAL
        print(f"       GUARD: {guard_note}")

    print(f"       category={triage.category}  lane={lane}")
    print(f"       reason: {triage.reason}")

    return {
        "diagnosis": triage.diagnosis,
        "category": triage.category,
        "reason": triage.reason,
        "lane": lane,
        "guard_note": guard_note,
    }


async def informational(state: BuildState) -> dict:
    """TEAL. Explain it, and nothing else. Unchanged from Phase 3."""
    print("  [teal] informational: posting the diagnosis")
    return {"action": "commented", "posted": await _post(state)}


async def safe_auto_fix(state: BuildState) -> dict:
    """AMBER. Looks flaky, so re-run the failed jobs.

    The guard that protects this lane lives in classify(), which downgrades
    to informational before routing ever reaches here. The check is
    repeated as an assertion rather than trusted, because the cost of being
    wrong is an infinite loop of CI runs.
    """
    attempt = state.get("run_attempt") or 1
    if attempt > 1:
        # Should be unreachable: classify() downgrades this case.
        print(f"  [amber] BLOCKED: run_attempt={attempt}, refusing to re-run")
        return {"action": "rerun blocked by guard", "posted": await _post(state)}

    print("  [amber] safe_auto_fix: re-running the failed jobs")
    try:
        await mcp_client.rerun_failed_jobs(state["repo"], state["run_id"])
        print("       re-run requested; it will appear as run_attempt 2")
        return {"action": "rerun requested", "rerun_requested": True}
    except github_client.GitHubError as exc:
        # Most likely the token lacks Actions: Read and write. Fall back to
        # saying something rather than silently doing nothing.
        print(f"       ERROR requesting re-run: {exc}")
        return {
            "action": f"rerun failed ({exc})",
            "rerun_requested": False,
            "posted": await _post(state),
        }


async def needs_review(state: BuildState) -> dict:
    """CORAL. A human has to look at this.

    On a pull request: add a label AND comment. A label is what shows up in
    a list of PRs, so it is the part a reviewer actually sees.

    On a plain push there is no PR and no label to add - a commit comment
    cannot carry one - so the distinction has to live in the text, hence
    the flag prefix.
    """
    number = github_client.pull_request_number(state["payload"])

    if number is None:
        print("  [coral] needs_review: no PR, posting a flagged commit comment")
        return {
            "action": "flagged commit comment",
            "posted": await _post(state, prefix=CORAL_PREFIX),
        }

    print(f"  [coral] needs_review: labelling PR #{number} and commenting")
    labels: list = []
    try:
        labels = await mcp_client.add_labels(
            state["repo"], number, [NEEDS_REVIEW_LABEL]
        )
        print(f"       labels now: {', '.join(labels)}")
    except github_client.GitHubError as exc:
        print(f"       ERROR adding label: {exc}")

    return {
        "action": "labelled and commented",
        "labels": labels,
        "posted": await _post(state),
    }


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def route(state: BuildState) -> str:
    """Conditional edge: return the NAME of the node to run next.

    Pure and boring on purpose. Every real decision - including the rerun
    guard - has already been made in classify() and written into
    state["lane"], so this cannot disagree with what was recorded.
    """
    lane = state.get("lane") or diagnose.FALLBACK_CATEGORY
    if lane not in diagnose.CATEGORIES:
        # Unreachable via the schema, but routing to a node that does not
        # exist is a crash, while an extra comment is not.
        return diagnose.FALLBACK_CATEGORY
    return lane


def build_graph() -> Any:
    """Assemble and compile the state machine.

    compile() validates the wiring - every named node exists, every path
    reaches END - and returns something with .ainvoke(state).
    """
    builder = StateGraph(BuildState)

    builder.add_node("classify", classify)
    builder.add_node(diagnose.INFORMATIONAL, informational)
    builder.add_node(diagnose.SAFE_AUTO_FIX, safe_auto_fix)
    builder.add_node(diagnose.NEEDS_REVIEW, needs_review)

    builder.add_edge(START, "classify")

    # The fan-out. route() names the next node; the third argument is the
    # set of nodes it is allowed to name, which is what lets compile()
    # catch a typo instead of failing at runtime.
    builder.add_conditional_edges(
        "classify",
        route,
        {
            diagnose.INFORMATIONAL: diagnose.INFORMATIONAL,
            diagnose.SAFE_AUTO_FIX: diagnose.SAFE_AUTO_FIX,
            diagnose.NEEDS_REVIEW: diagnose.NEEDS_REVIEW,
        },
    )

    for lane in diagnose.CATEGORIES:
        builder.add_edge(lane, END)

    return builder.compile()


# Compiled once at import time. The graph is stateless between runs - all
# the state travels in the dict passed to ainvoke - so one instance is
# safe to share across concurrent webhooks.
GRAPH = build_graph()
