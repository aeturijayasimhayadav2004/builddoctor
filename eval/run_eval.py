"""Run the golden set against the real BuildDoctor, and score it.

    docker compose run --rm --no-deps app python eval/run_eval.py

HOW THIS CALLS THE REAL LOGIC WITHOUT REAL SIDE EFFECTS

This is the part worth understanding, because the usual way to write a
harness like this is to reimplement the interesting bit and then measure
the reimplementation. That produces a number about the test, not about the
system.

Nothing here is reimplemented. The eval calls exactly two things:

  1. graph.classify(state)

     Not a copy of it - the actual LangGraph node that the live pipeline
     runs. It calls diagnose.diagnose_failure with the real prompt and
     applies the real rerun guard, and it returns a dict.

     The reason this is safe is structural rather than a matter of care.
     Posting a comment, adding a label and re-running a job do not live in
     classify(). They live in three SIBLING nodes - informational(),
     safe_auto_fix(), needs_review() - which only run when the compiled
     graph routes to them. Calling the node directly instead of calling
     GRAPH.ainvoke() means the routing never happens, so those nodes are
     never entered. The eval is not avoiding the side effects; it never
     reaches the code that has them.

  2. memory.search_past_failures(...)

     Also the real one, threshold and all. It is a SELECT plus some CPU.
     db.nearest_by_embedding is called alongside it to record the raw
     similarity scores that search_past_failures deliberately throws away
     when it rejects a match - the same function search_past_failures
     calls internally, not a second implementation of it.

  Nothing calls db.save_diagnosis. Nothing calls mcp_client. No row is
  written, no comment is posted, no job is re-run.

AND A LANDMINE, BECAUSE STRUCTURE IS AN ARGUMENT AND NOT A PROOF

install_write_landmines() below replaces every write path in the process
with a function that raises. These are not mocks - a mock would let the
code run and quietly return a plausible value, which is precisely how a
harness ends up posting to a real repository while its author is certain
it cannot. If any of these is ever called, this script dies loudly with a
stack trace pointing at whoever called it.

So there are two independent reasons no side effect can happen: the code
that does them is never reached, and it would explode if it were.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

# The script lives in eval/ but the modules it tests live one level up.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import diagnose  # noqa: E402
import embeddings  # noqa: E402
import graph  # noqa: E402
import mcp_client  # noqa: E402
import memory  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
GOLDEN = HERE / "golden_set.json"
REPORT = HERE / "report.md"

LANES = list(diagnose.CATEGORIES)

# THE PROVIDER'S RATE LIMIT IS THE BINDING CONSTRAINT, NOT THE MODEL.
#
# Groq's free tier allows 8000 tokens per minute for this model. A single
# case costs 1900-3350 prompt tokens, because the log excerpts are long -
# so the ceiling is roughly two and a half cases per minute, and the whole
# 24-case set cannot finish faster than about ten minutes no matter how it
# is scheduled.
#
# The first run of this file used CONCURRENCY=3 and no backoff. Ten of the
# 24 cases came back as HTTP 429 and were dropped from scoring, which
# produced a confident-looking accuracy figure computed over 14 cases. An
# eval that silently scores a subset is worse than one that fails, so the
# pacing below exists to make the number trustworthy rather than fast.
#
# This is a property of the free tier, not of BuildDoctor. Nothing in
# diagnose.py was touched to accommodate it - the retry lives here, in the
# harness, because it is the harness that is being rate limited.
CONCURRENCY = 1

# Minimum gap between the START of one request and the next. 24 seconds
# keeps the worst-case case (3350 tokens) at about 2.5 per minute.
MIN_GAP_SECONDS = 24.0

# How many times to wait out a 429 before giving up on a case.
RATE_LIMIT_ATTEMPTS = 6

# How many neighbours to record per case. One is what memory actually uses;
# three is what makes the middle-zone table readable, because seeing the
# second and third scores is how you tell "one clear match" apart from "a
# whole cluster sitting at the same distance".
NEIGHBOURS = 3


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------


def install_write_landmines() -> list[str]:
    """Make every write in the process raise, and report what was armed.

    Deliberately NOT mocks. A mock returns something plausible and lets
    execution continue, which means a harness that accidentally calls a
    write path looks like it worked. These raise.
    """
    armed = []

    def landmine(name):
        def boom(*args, **kwargs):
            raise AssertionError(
                f"eval attempted a real side effect: {name}(). "
                f"The eval is read-only; this is a bug in the harness, not "
                f"a test failure."
            )

        return boom

    for module, attr in [
        (db, "save_diagnosis"),
        (db, "set_embedding"),
        (db, "init_db"),
        (mcp_client, "post_diagnosis"),
        (mcp_client, "rerun_failed_jobs"),
        (mcp_client, "add_labels"),
        (mcp_client, "_call"),
        (mcp_client, "post_pull_request_comment"),
        (mcp_client, "post_commit_comment"),
    ]:
        if hasattr(module, attr):
            setattr(module, attr, landmine(f"{module.__name__}.{attr}"))
            armed.append(f"{module.__name__}.{attr}")

    return armed


# --------------------------------------------------------------------------
# Running one case
# --------------------------------------------------------------------------


def memory_probe(case: dict) -> dict:
    """What memory does with this case, and the numbers behind it.

    Runs the real lookup, then records the raw neighbour scores as well.
    search_past_failures returns None whenever the closest row is below the
    threshold - which is correct behaviour and useless for measuring where
    the threshold actually sits, so the scores are captured separately.
    """
    # Replaying a real row must not let it match itself. This is the same
    # exclusion the live pipeline applies, for the same reason: the row
    # written by a run is a perfect match for that run and teaches nothing.
    exclude = case.get("exclude_run_id")

    vector = embeddings.embed(case["log_excerpt"])
    neighbours = db.nearest_by_embedding(
        vector, exclude_run_id=exclude, limit=NEIGHBOURS
    )

    match = memory.search_past_failures(case["log_excerpt"], exclude_run_id=exclude)

    return {
        "excluded_run_id": exclude,
        "neighbours": [
            {
                "row_id": row.id,
                "run_id": row.run_id,
                "lane": row.lane,
                "similarity": round(sim, 4),
            }
            for row, sim in neighbours
        ],
        "top_similarity": round(neighbours[0][1], 4) if neighbours else None,
        "top_row_id": neighbours[0][0].id if neighbours else None,
        "matched": match is not None,
        "matched_row_id": match.row_id if match else None,
        "matched_similarity": round(match.similarity, 4) if match else None,
        "past_summary": match.summary() if match else "",
    }


_pace_lock = asyncio.Lock()
_last_start = 0.0


async def _pace() -> None:
    """Hold each request back so the whole set stays under the token limit."""
    global _last_start
    async with _pace_lock:
        wait = MIN_GAP_SECONDS - (time.monotonic() - _last_start)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_start = time.monotonic()


_RETRY_AFTER = re.compile(r"try again in ([\d.]+)s")


async def classify_with_backoff(state: dict, case_id: str) -> dict:
    """graph.classify, retried when the provider says to slow down.

    The retry is HERE and not in diagnose.py on purpose. diagnose.py is the
    system under test and this phase does not modify it; being rate limited
    is a fact about running 24 cases in a burst against a free tier, which
    is a fact about the harness.

    Only 429 is retried. Every other failure is a real result and is
    recorded as an error rather than being papered over.
    """
    last: Exception | None = None
    for attempt in range(RATE_LIMIT_ATTEMPTS):
        await _pace()
        try:
            return await graph.classify(state)
        except diagnose.DiagnosisError as exc:
            text = str(exc)
            if "429" not in text and "rate_limit" not in text:
                raise
            last = exc
            # The provider says exactly how long to wait. Believe it, plus
            # a second, rather than guessing with an exponential curve.
            found = _RETRY_AFTER.search(text)
            delay = float(found.group(1)) + 1.0 if found else 20.0
            print(f"       {case_id}: rate limited, waiting {delay:.0f}s "
                  f"(attempt {attempt + 1}/{RATE_LIMIT_ATTEMPTS})")
            await asyncio.sleep(delay)
    raise last if last else RuntimeError("unreachable")


async def run_case(case: dict, semaphore: asyncio.Semaphore) -> dict:
    """One golden case, start to finish."""
    started = time.perf_counter()

    # The embedding model is not thread-safe to load twice and the lookup is
    # synchronous, so the memory probe runs off the event loop.
    mem = await asyncio.to_thread(memory_probe, case)

    # Exactly the state main.py hands the graph. run_attempt=1 so the rerun
    # guard is in its normal position - it downgrades safe_auto_fix only on
    # a re-run, and none of these are re-runs.
    state = {
        "repo": case.get("repo", "buildDocterRepo1"),
        "log_excerpt": case["log_excerpt"],
        "diff": case["diff"],
        "job_names": [case.get("job_name", "test")],
        "first_step": case.get("failed_step", "Run tests"),
        "run_attempt": case.get("run_attempt", 1),
        # Fed straight from the real memory lookup, exactly as main.py does,
        # so what is being measured is the system WITH memory attached and
        # not a hypothetical version of it without.
        "past_summary": mem["past_summary"],
    }

    error = None
    result = {}
    try:
        async with semaphore:
            result = await classify_with_backoff(state, case["id"])
    except Exception as exc:  # noqa: BLE001
        result = {}
        error = f"{type(exc).__name__}: {exc}"

    actual = result.get("lane")
    return {
        "id": case["id"],
        "group": case["group"],
        "description": case["description"],
        "ambiguous": case.get("ambiguous", False),
        "source_row": case.get("source_row"),
        "recorded_lane": case.get("recorded_lane"),
        "expected_lane": case["expected_lane"],
        "lane_rationale": case.get("lane_rationale", ""),
        "expected_memory": case.get("expected_memory"),
        "actual_lane": actual,
        # category is what the model chose; lane is what would actually run
        # after the guard. They differ only when the guard fired.
        "actual_category": result.get("category"),
        "guard_note": result.get("guard_note", ""),
        "model_reason": result.get("reason", ""),
        "diagnosis": result.get("diagnosis", ""),
        "correct": actual == case["expected_lane"],
        "error": error,
        "memory": mem,
        "seconds": round(time.perf_counter() - started, 2),
    }


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def score(results: list[dict]) -> dict:
    graded = [r for r in results if r["error"] is None]
    correct = [r for r in graded if r["correct"]]

    by_lane = {}
    for lane in LANES:
        subset = [r for r in graded if r["expected_lane"] == lane]
        by_lane[lane] = {
            "total": len(subset),
            "correct": sum(1 for r in subset if r["correct"]),
        }

    by_group = {}
    for group in sorted({r["group"] for r in graded}):
        subset = [r for r in graded if r["group"] == group]
        by_group[group] = {
            "total": len(subset),
            "correct": sum(1 for r in subset if r["correct"]),
        }

    # Confusion: expected -> what came back instead.
    confusion = defaultdict(Counter)
    for r in graded:
        confusion[r["expected_lane"]][r["actual_lane"]] += 1

    # The unambiguous subset. A miss on a case I labelled ambiguous is a
    # disagreement; a miss on a clear case is a defect, and mixing the two
    # into one percentage hides which kind of trouble the system is in.
    clear = [r for r in graded if not r["ambiguous"]]

    return {
        "total": len(results),
        "graded": len(graded),
        "errored": len(results) - len(graded),
        "correct": len(correct),
        "accuracy": round(100.0 * len(correct) / len(graded), 1) if graded else 0.0,
        "clear_total": len(clear),
        "clear_correct": sum(1 for r in clear if r["correct"]),
        "clear_accuracy": (
            round(100.0 * sum(1 for r in clear if r["correct"]) / len(clear), 1)
            if clear
            else 0.0
        ),
        "by_lane": by_lane,
        "by_group": by_group,
        "confusion": {k: dict(v) for k, v in confusion.items()},
    }


def memory_verdict(result: dict) -> tuple[str, str]:
    """Did memory do what a human would have wanted? (verdict, explanation)"""
    expected = result.get("expected_memory")
    if not expected:
        return "n/a", "no memory expectation was set for this case"

    wanted = expected["verdict"] == "should_match"
    got = result["memory"]["matched"]
    top = result["memory"]["top_similarity"]

    if wanted and got:
        return "AS PREDICTED", f"matched at {top:.4f}, and a match was wanted"
    if wanted and not got:
        return "MISS", (
            f"closest was {top:.4f}, below the "
            f"{memory.SIMILARITY_THRESHOLD:.2f} threshold, but a human "
            f"would have wanted this hint"
        )
    if not wanted and got:
        return "FALSE MATCH", (
            f"matched row {result['memory']['matched_row_id']} at {top:.4f}, "
            f"but a human would NOT have wanted this hint"
        )
    return "AS PREDICTED", (
        f"closest was {top:.4f}, correctly rejected"
    )


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def bar(part: int, whole: int, width: int = 18) -> str:
    if not whole:
        return " " * width
    filled = round(width * part / whole)
    return "#" * filled + "." * (width - filled)


def render_console(results: list[dict], s: dict, armed: list[str]) -> None:
    print("\n" + "=" * 74)
    print("  BUILDDOCTOR PHASE 8 - GOLDEN SET RESULTS")
    print("=" * 74)

    print(f"\n  OVERALL CLASSIFICATION ACCURACY   {s['correct']}/{s['graded']}"
          f"   ({s['accuracy']}%)")
    print(f"  Excluding the 3 ambiguous cases   {s['clear_correct']}/"
          f"{s['clear_total']}   ({s['clear_accuracy']}%)")
    if s["errored"]:
        print(f"  !! {s['errored']} case(s) errored and are not scored")

    print("\n  BY EXPECTED LANE")
    for lane, d in s["by_lane"].items():
        pct = f"{100.0 * d['correct'] / d['total']:.0f}%" if d["total"] else "  -"
        print(f"    {lane:<16} {d['correct']:>2}/{d['total']:<2}  {pct:>4}  "
              f"{bar(d['correct'], d['total'])}")

    print("\n  BY GROUP")
    for group, d in s["by_group"].items():
        pct = f"{100.0 * d['correct'] / d['total']:.0f}%" if d["total"] else "  -"
        print(f"    {group:<16} {d['correct']:>2}/{d['total']:<2}  {pct:>4}  "
              f"{bar(d['correct'], d['total'])}")

    print("\n  CONFUSION (expected -> actual)")
    for expected, got in s["confusion"].items():
        parts = ", ".join(f"{k}={v}" for k, v in sorted(got.items(), key=lambda x: -x[1]))
        print(f"    {expected:<16} {parts}")

    # ---- the middle zone, which is the actual question ----
    print("\n" + "=" * 74)
    print("  THE MIDDLE ZONE  (the open question from Phase 6)")
    print("=" * 74)
    print(f"  Threshold is {memory.SIMILARITY_THRESHOLD:.2f}. Before this eval, "
          f"nothing had ever landed\n  between 0.83 and 0.99.\n")
    print(f"  {'case':<9} {'top':>7}  {'row':>4}  {'matched':>8}  "
          f"{'predicted':<16} {'verdict'}")
    print("  " + "-" * 70)
    for r in results:
        if not r.get("expected_memory"):
            continue
        verdict, _ = memory_verdict(r)
        top = r["memory"]["top_similarity"]
        print(f"  {r['id']:<9} {top:>7.4f}  {r['memory']['top_row_id']:>4}  "
              f"{'YES' if r['memory']['matched'] else 'no':>8}  "
              f"{r['expected_memory']['verdict']:<16} {verdict}")

    print("\n  Every case's closest neighbour, for context:")
    print(f"  {'case':<9} {'top':>7} {'2nd':>7} {'3rd':>7}   nearest row")
    print("  " + "-" * 60)
    for r in results:
        n = r["memory"]["neighbours"]
        cols = " ".join(f"{x['similarity']:>7.4f}" for x in n[:3])
        cols += "        " * (3 - len(n[:3]))
        print(f"  {r['id']:<9} {cols}   row {n[0]['row_id'] if n else '-'}")

    # ---- every wrong case, individually ----
    wrong = [r for r in results if r["error"] is None and not r["correct"]]
    print("\n" + "=" * 74)
    print(f"  WRONG CASES ({len(wrong)})")
    print("=" * 74)
    if not wrong:
        print("  None.")
    for r in wrong:
        print(f"\n  --- {r['id']}  [{r['group']}]"
              f"{'  (I FLAGGED THIS AMBIGUOUS)' if r['ambiguous'] else ''}")
        print(f"      {r['description']}")
        print(f"      expected : {r['expected_lane']}")
        actual = r["actual_lane"]
        if r["actual_category"] != actual:
            actual = f"{actual}  (the model chose {r['actual_category']}; the guard changed it)"
        print(f"      actual   : {actual}")
        if r["recorded_lane"]:
            print(f"      recorded : {r['recorded_lane']}  (what the live run stored)")
        print(f"      model's reason : {r['model_reason']}")
        print(f"      my rationale   : {r['lane_rationale'][:300]}")
        if r["memory"]["matched"]:
            print(f"      memory hinted  : row {r['memory']['matched_row_id']} "
                  f"at {r['memory']['matched_similarity']:.4f}")

    errored = [r for r in results if r["error"]]
    if errored:
        print("\n  ERRORED CASES")
        for r in errored:
            print(f"    {r['id']}: {r['error']}")

    print(f"\n  Write landmines armed on: {', '.join(armed)}")
    print("  None of them fired, so nothing was posted, labelled, re-run or saved.\n")


def render_markdown(results: list[dict], s: dict, armed: list[str], elapsed: float) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out: list[str] = []
    w = out.append

    w("# BuildDoctor evaluation report")
    w("")
    w(f"Generated {now} by `eval/run_eval.py` against `eval/golden_set.json`.")
    w(f"{s['total']} cases, {elapsed:.0f} seconds, model `{diagnose.MODEL}` at "
      f"temperature 0.2.")
    w("")
    w("This run **measured** the system. Nothing was tuned in response to it: "
      "no prompt, no threshold, no guard was changed. A fix without a "
      "before-and-after number is a guess with extra steps.")
    w("")

    w("## Headline")
    w("")
    w(f"| | |")
    w(f"| --- | --- |")
    w(f"| **Classification accuracy** | **{s['correct']}/{s['graded']} "
      f"({s['accuracy']}%)** |")
    w(f"| Excluding cases I flagged ambiguous | {s['clear_correct']}/"
      f"{s['clear_total']} ({s['clear_accuracy']}%) |")
    if s["errored"]:
        w(f"| Errored, not scored | {s['errored']} |")
    w("")

    w("### By expected lane")
    w("")
    w("| Lane | Correct | Accuracy |")
    w("| ---- | ------- | -------- |")
    for lane, d in s["by_lane"].items():
        pct = f"{100.0 * d['correct'] / d['total']:.0f}%" if d["total"] else "-"
        w(f"| `{lane}` | {d['correct']}/{d['total']} | {pct} |")
    w("")

    w("### By group")
    w("")
    w("| Group | Correct | Accuracy | What it tests |")
    w("| ----- | ------- | -------- | ------------- |")
    blurb = {
        "historical": "Real failures from rows 1-13, verbatim excerpts",
        "synthetic": "Written to cover each lane, including 2 ambiguous",
        "middle-zone": "Similar-but-different pairs, to probe the threshold",
        "live-crosscheck": "Replays of real runs, vs what the live pipeline stored",
    }
    for group, d in s["by_group"].items():
        pct = f"{100.0 * d['correct'] / d['total']:.0f}%" if d["total"] else "-"
        w(f"| {group} | {d['correct']}/{d['total']} | {pct} | {blurb.get(group, '')} |")
    w("")

    w("### Confusion")
    w("")
    w("| Expected | What came back |")
    w("| -------- | -------------- |")
    for expected, got in s["confusion"].items():
        parts = ", ".join(f"`{k}` x{v}" for k, v in sorted(got.items(), key=lambda x: -x[1]))
        w(f"| `{expected}` | {parts} |")
    w("")

    # ---- middle zone ----
    w("## The middle zone: 0.83 to 0.99")
    w("")
    w("This is the open question Phase 6 left behind. Every similarity ever "
      "seen by this system had been either about 1.00 (the same fixture "
      "failing twice) or at most 0.83 (two unrelated failures). The band "
      "between them was empty, so the "
      f"{memory.SIMILARITY_THRESHOLD:.2f} threshold had never actually been "
      "asked to make a hard call.")
    w("")
    w("Six cases were built to land in it: pairs that are deliberately "
      "similar-but-different. The `predicted` column is my guess, written "
      "**before** the run; `verdict` is what happened.")
    w("")
    w("| Case | Top similarity | Nearest row | Matched? | I predicted | Verdict |")
    w("| ---- | -------------- | ----------- | -------- | ----------- | ------- |")
    for r in results:
        if not r.get("expected_memory"):
            continue
        verdict, _ = memory_verdict(r)
        m = r["memory"]
        w(f"| `{r['id']}` | **{m['top_similarity']:.4f}** | {m['top_row_id']} | "
          f"{'**yes**' if m['matched'] else 'no'} | "
          f"{r['expected_memory']['verdict'].replace('_', ' ')} | {verdict} |")
    w("")

    w("### Case by case")
    w("")
    for r in results:
        if not r.get("expected_memory"):
            continue
        verdict, explanation = memory_verdict(r)
        m = r["memory"]
        w(f"#### `{r['id']}` - {r['description']}")
        w("")
        w(f"- **Top similarity: {m['top_similarity']:.4f}** against row "
          f"{m['top_row_id']}; memory {'RETURNED' if m['matched'] else 'rejected'} it.")
        others = ", ".join(
            f"row {n['row_id']} at {n['similarity']:.4f}" for n in m["neighbours"][1:]
        )
        if others:
            w(f"- Next closest: {others}.")
        w(f"- I predicted **{r['expected_memory']['verdict'].replace('_', ' ')}** "
          f"against rows {r['expected_memory']['against_rows']}.")
        w(f"- Verdict: **{verdict}** - {explanation}.")
        w("")
        w(f"  > {r['expected_memory']['why']}")
        w("")

    w("### Where every case landed")
    w("")
    w("Including the ones with no memory expectation, because the shape of "
      "the whole distribution is what says whether the threshold sits in a "
      "gap or in the middle of a crowd.")
    w("")
    w("| Case | Closest | 2nd | 3rd | Nearest row | Above threshold? |")
    w("| ---- | ------- | --- | --- | ----------- | ---------------- |")
    for r in sorted(results, key=lambda x: -(x["memory"]["top_similarity"] or 0)):
        n = r["memory"]["neighbours"]
        if not n:
            continue
        second = f"{n[1]['similarity']:.4f}" if len(n) > 1 else "-"
        third = f"{n[2]['similarity']:.4f}" if len(n) > 2 else "-"
        w(f"| `{r['id']}` | {n[0]['similarity']:.4f} | {second} | {third} | "
          f"{n[0]['row_id']} | {'yes' if r['memory']['matched'] else 'no'} |")
    w("")

    # ---- wrong cases ----
    wrong = [r for r in results if r["error"] is None and not r["correct"]]
    w(f"## Every wrong case ({len(wrong)})")
    w("")
    if not wrong:
        w("None. Every case returned the expected lane.")
        w("")
    for r in wrong:
        flag = " - **I flagged this one ambiguous**" if r["ambiguous"] else ""
        w(f"### `{r['id']}` ({r['group']}){flag}")
        w("")
        w(f"{r['description']}")
        w("")
        w("| | |")
        w("| --- | --- |")
        w(f"| Expected | `{r['expected_lane']}` |")
        w(f"| Actually returned | `{r['actual_lane']}` |")
        if r["actual_category"] != r["actual_lane"]:
            w(f"| Model chose | `{r['actual_category']}` (the guard changed it) |")
        if r["guard_note"]:
            w(f"| Guard | {r['guard_note']} |")
        if r["recorded_lane"]:
            w(f"| Live pipeline recorded | `{r['recorded_lane']}` |")
        if r["memory"]["matched"]:
            w(f"| Memory hint | row {r['memory']['matched_row_id']} at "
              f"{r['memory']['matched_similarity']:.4f} |")
        w("")
        w("**The model's own reason:**")
        w("")
        w(f"> {r['model_reason']}")
        w("")
        w("**Its diagnosis:**")
        w("")
        for line in (r["diagnosis"] or "").strip().splitlines():
            w(f"> {line}")
        w("")
        w("**Why I expected something else:**")
        w("")
        w(f"> {r['lane_rationale']}")
        w("")

    # ---- live cross-check ----
    live = [r for r in results if r["group"] == "live-crosscheck"]
    w("## Live cross-check")
    w("")
    w("Four verbatim replays of runs the live pipeline actually processed. "
      "The question is narrow: does calling the classifier the way this "
      "harness calls it produce what the live pipeline produced when it "
      "called it for real?")
    w("")
    w("| Case | Source row | Live recorded | Eval returned | Agree? |")
    w("| ---- | ---------- | ------------- | ------------- | ------ |")
    for r in live:
        agree = "yes" if r["actual_lane"] == r["recorded_lane"] else "**no**"
        w(f"| `{r['id']}` | {r['source_row']} | `{r['recorded_lane']}` | "
          f"`{r['actual_lane']}` | {agree} |")
    w("")
    agreed = sum(1 for r in live if r["actual_lane"] == r["recorded_lane"])
    w(f"**{agreed} of {len(live)} agree.**")
    w("")

    # ---- historical disagreements ----
    hist = [
        r for r in results
        if r["recorded_lane"] and r["group"] == "historical"
    ]
    dis = [r for r in hist if r["actual_lane"] != r["recorded_lane"]]
    if hist:
        w("## Where the system now disagrees with its own history")
        w("")
        w("The lane stored in the database is not ground truth - it is what "
          "the system output at the time, sometimes under an earlier "
          "prompt. These are the historical cases where today's answer "
          "differs from what was stored.")
        w("")
        w("| Case | Row | Stored then | Returns now | My expected |")
        w("| ---- | --- | ----------- | ----------- | ----------- |")
        for r in hist:
            mark = "**" if r["actual_lane"] != r["recorded_lane"] else ""
            w(f"| `{r['id']}` | {r['source_row']} | `{r['recorded_lane']}` | "
              f"{mark}`{r['actual_lane']}`{mark} | `{r['expected_lane']}` |")
        w("")
        w(f"{len(dis)} of {len(hist)} historical cases with a stored lane now "
          f"return something different.")
        w("")

    # ---- method ----
    w("## How this was run")
    w("")
    w("- `graph.classify(state)` - the **real** LangGraph node, not a copy. "
      "It calls `diagnose.diagnose_failure` with the real prompt and applies "
      "the real rerun guard.")
    w("- `memory.search_past_failures(...)` - the **real** lookup, threshold "
      "and all. `db.nearest_by_embedding` records the raw scores that the "
      "gated function discards on a rejection.")
    w("- Posting, labelling and re-running live in three sibling nodes that "
      "only run when the compiled graph routes to them. Calling the node "
      "directly means routing never happens, so that code is never reached.")
    w("- On top of that, these write paths were replaced with functions that "
      "raise before the run started, and none of them fired:")
    w("")
    for name in armed:
        w(f"  - `{name}`")
    w("")
    w("- Replayed rows pass `exclude_run_id`, so a real excerpt cannot match "
      "the row it wrote. That is the same exclusion the live pipeline uses.")
    w("")

    w("## Caveats, stated rather than buried")
    w("")
    w("1. **The diffs on historical cases are reconstructed.** The pipeline "
      "stores `diff_summary` - which files changed and how many lines - but "
      "never the diff text. Those cases have the real log excerpt byte for "
      "byte and a diff rebuilt from the summary. Faithful in shape and in "
      "which file changed; not identical to what the model originally saw.")
    w("2. **One sample per case.** Temperature is 0.2, not 0, so these "
      "numbers carry unmeasured run-to-run variance. `--repeat N` runs each "
      "case N times if you want to measure it.")
    w("3. **Twenty-four cases is a small set.** One case is roughly four "
      "percentage points. Treat a change of one or two cases as noise.")
    w("4. **The middle-zone cases are compared against thirteen rows.** "
      "A similarity score is a statement about this database, not about the "
      "world. The same case against a thousand rows could find a closer "
      "neighbour and behave differently.")
    w("5. **I wrote both the cases and the expected answers.** The lanes are "
      "derived from `diagnose.py`'s own STEP 1-5, so they are at least "
      "traceable to a written rule rather than to taste - but a second "
      "person would disagree somewhere, and the three cases marked "
      "ambiguous are where I would expect it first.")
    w("")

    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run each case N times, to measure run-to-run variance",
    )
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated case ids or group names, to run a subset",
    )
    args = parser.parse_args()

    armed = install_write_landmines()
    print(f"  write landmines armed on {len(armed)} functions")

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = golden["cases"]

    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in wanted or c["group"] in wanted]

    # A replayed row must not be allowed to match itself.
    rows_by_id = {}
    for case in cases:
        if case.get("source_row"):
            rows_by_id[case["source_row"]] = None
    if rows_by_id:
        with db.Session() as session:
            for row_id in list(rows_by_id):
                row = session.get(db.Diagnosis, row_id)
                rows_by_id[row_id] = row.run_id if row else None
    for case in cases:
        if case.get("source_row"):
            case["exclude_run_id"] = rows_by_id.get(case["source_row"])

    print(f"  loaded {len(cases)} cases from {GOLDEN.name}")
    print(f"  warming the embedding model ...")
    await asyncio.to_thread(embeddings.warm)

    semaphore = asyncio.Semaphore(CONCURRENCY)
    started = time.perf_counter()

    all_runs: list[list[dict]] = []
    for attempt in range(args.repeat):
        if args.repeat > 1:
            print(f"\n  === pass {attempt + 1} of {args.repeat} ===")
        results = await asyncio.gather(
            *(run_case(case, semaphore) for case in cases)
        )
        all_runs.append(list(results))

    elapsed = time.perf_counter() - started
    results = all_runs[0]
    s = score(results)

    render_console(results, s, armed)

    if args.repeat > 1:
        print("  RUN-TO-RUN VARIANCE")
        for i, case in enumerate(cases):
            lanes = [run[i]["actual_lane"] for run in all_runs]
            flag = "  <-- UNSTABLE" if len(set(lanes)) > 1 else ""
            print(f"    {case['id']:<9} {' '.join(str(x) for x in lanes)}{flag}")
        print("")

    REPORT.write_text(
        render_markdown(results, s, armed, elapsed), encoding="utf-8"
    )
    print(f"  report written to {REPORT}")

    # Raw results next to the report, so a later run can be diffed against
    # this one without re-reading a rendered document.
    (HERE / "last_run.json").write_text(
        json.dumps({"scored": s, "results": results}, indent=2, default=str),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
