"""BuildDoctor - the web layer and the evidence-gathering half.

A workflow run fails, BuildDoctor pulls the evidence (the log around the
error, plus the change that triggered the run), and hands it to the graph
in graph.py. The graph classifies the failure into one of three lanes and
acts accordingly:

    informational (teal)   post the diagnosis as a comment
    safe_auto_fix (amber)  re-run the failed jobs
    needs_review  (coral)  label the PR / flag the comment for a human

Whatever the graph did is then recorded in Postgres, including which lane
actually ran.

Since Phase 6 the evidence gathering includes a lookup in BuildDoctor's own
history: if a similar failure has been diagnosed before, that diagnosis is
passed to the model as context. It is a hint, not an instruction - see
memory.py and the prompt in diagnose.py.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

# Reads .env into the environment. This has to run BEFORE db is imported:
# db builds its engine at import time and reads DATABASE_URL right then.
# (diagnose and github_client read their variables at call time, so they
# are less fussy - db is not.)
load_dotenv()

import db  # noqa: E402
import diagnose  # noqa: E402
import embeddings  # noqa: E402
import github_client  # noqa: E402
import graph  # noqa: E402
import memory  # noqa: E402
from log_excerpt import extract_error_excerpt  # noqa: E402

# Models return typographic characters (curly quotes, arrows) that the default
# Windows console codepage cannot encode. Without this, those characters come
# out as mojibake or raise UnicodeEncodeError mid-print.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup work: make sure the database exists and is reachable.

    Under docker compose the app can boot before Postgres is accepting
    connections, so this waits rather than crashing. create_all() then
    creates the table on first run and does nothing on every run after.
    """
    print(f"  database: {db.database_url()}")
    await asyncio.to_thread(db.wait_for_database)
    await asyncio.to_thread(db.init_db)
    print("  database ready")

    # Load the embedding model now rather than on the first failed build.
    # The weights are already in the image, so this is a few seconds of
    # CPU, and paying it here means a webhook never waits for it. On a
    # worker thread so the event loop can start serving immediately.
    await asyncio.to_thread(embeddings.warm)
    yield


app = FastAPI(title="BuildDoctor", version="0.6.0", lifespan=lifespan)

PROJECT_DIR = Path(__file__).parent
LOGS_DIR = PROJECT_DIR / "logs"

CONSOLE_DIFF_MAX_LINES = 200

RULE = "=" * 74
THIN = "-" * 74


@app.get("/health")
async def health():
    """Liveness check. Open this in a browser to confirm the server is up."""
    return {"status": "ok"}


def signature_is_valid(raw_body: bytes, signature_header: str) -> bool:
    """Verify GitHub's X-Hub-Signature-256 header against the raw body.

    GitHub signs the exact bytes it sent, so this must run before the body is
    parsed - re-serialising parsed JSON would change those bytes.
    """
    secret = os.environ.get("WEBHOOK_SECRET", "").strip()

    if not secret:
        print("  WARNING: WEBHOOK_SECRET is not set - request accepted unverified.")
        return True

    if not signature_header:
        return False

    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"

    # compare_digest, not ==, so the comparison time does not leak how much of
    # the signature an attacker guessed correctly.
    return hmac.compare_digest(expected, signature_header)


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    """Accept a webhook delivery, verify it, and react to failed runs."""
    raw = await request.body()
    event = request.headers.get("X-GitHub-Event", "unknown")
    delivery = request.headers.get("X-GitHub-Delivery", "unknown")
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not signature_is_valid(raw, signature):
        print(f"\n  REJECTED {event} delivery={delivery}: bad or missing signature\n")
        return JSONResponse(status_code=401, content={"detail": "invalid signature"})

    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        print(f"\n  REJECTED {event} delivery={delivery}: body was not JSON\n")
        return JSONResponse(status_code=400, content={"detail": "invalid JSON"})

    run = payload.get("workflow_run") or {}
    action = payload.get("action")
    repo = (payload.get("repository") or {}).get("full_name")

    print(f"\n  webhook: event={event} action={action} repo={repo}", end="")
    if run:
        print(f" workflow={run.get('name')!r} conclusion={run.get('conclusion')}")
    else:
        print()

    is_failed_run = (
        event == "workflow_run"
        and action == "completed"
        and run.get("conclusion") == "failure"
    )

    if is_failed_run:
        # Respond to GitHub immediately; do the slow work afterwards.
        # GitHub times a delivery out after about 10 seconds.
        background.add_task(investigate_failure, payload)
        print("  -> failed run detected, diagnosing in the background")

    return {"received": True}


def summarise_diff(diff: str) -> dict:
    """Reduce a unified diff to counts and filenames.

    The full diff is sent to the model but not kept in the record file - an
    append-only log of complete diffs becomes unusable very quickly.
    """
    files = []
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:])
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {
        "files_changed": files,
        "lines_added": added,
        "lines_removed": removed,
    }


async def record_diagnosis(
    *,
    run_id: int,
    repo: str,
    log_excerpt: str,
    diff_summary: dict,
    diagnosis_text: str,
    posted_target: str | None,
    lane: str | None,
    extra: dict,
    embedding: list | None,
) -> int:
    """Store one diagnosis in Postgres. Returns the new row id.

    save_diagnosis is synchronous, so it runs on a worker thread and the
    event loop stays free to answer the next webhook.
    """
    return await asyncio.to_thread(
        db.save_diagnosis,
        run_id=run_id,
        repo=repo,
        log_excerpt=log_excerpt,
        diff_summary=diff_summary,
        diagnosis_text=diagnosis_text,
        posted_to=db.posted_to_column(posted_target),
        lane=lane,
        raw=extra,
        embedding=embedding,
    )


async def investigate_failure(payload: dict) -> None:
    """Run the pipeline, and never let an exception escape.

    This runs as a background task. An exception escaping here is
    reported by the server as "Exception in ASGI application" long after
    the 200 was sent, so nobody is watching - the failure simply looks
    like BuildDoctor ignoring the build.
    """
    run_id = (payload.get('workflow_run') or {}).get('id')
    try:
        await _investigate(payload)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  PIPELINE FAILED for run {run_id}: "
              f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        print(RULE + "\n", flush=True)


async def _investigate(payload: dict) -> None:
    """Gather evidence, classify it, act on the lane, and record it."""
    repo = payload["repository"]["full_name"]
    run = payload["workflow_run"]
    run_id = run["id"]

    print("\n" + RULE)
    print("  BUILD FAILED - collecting evidence")
    print(f"  repo={repo}  run_id={run_id}  workflow={run.get('name')!r}")
    print(f"  {run.get('html_url')}")
    print(RULE)

    # --- 1. which jobs failed, and what did they say -------------------
    try:
        failed_jobs = await github_client.list_failed_jobs(repo, run_id)
    except github_client.GitHubError as exc:
        print(f"  ERROR listing jobs: {exc}")
        return

    if not failed_jobs:
        print("  No failed jobs returned. The run may have failed before any")
        print("  job started (for example, invalid workflow YAML).")
        return

    log_sections = []
    excerpts = []
    job_names = []
    first_step = "unknown"

    for index, job in enumerate(failed_jobs, start=1):
        job_id = job.get("id")
        name = job.get("name")
        job_names.append(name)
        failed_steps = [
            step.get("name")
            for step in (job.get("steps") or [])
            if step.get("conclusion") == "failure"
        ]
        if failed_steps and first_step == "unknown":
            first_step = failed_steps[0]

        print(f"\n  [{index}/{len(failed_jobs)}] FAILED JOB: {name}  (id={job_id})")
        if failed_steps:
            print(f"       failed step(s): {', '.join(failed_steps)}")

        try:
            text = await github_client.download_job_log(repo, job_id)
        except Exception as exc:  # noqa: BLE001
            # Deliberately broad. One job's log going missing is not a
            # reason to abandon the diagnosis - the other jobs, and the
            # diff, may still explain the failure perfectly well.
            text = f"<log unavailable: {exc}>"
            print(f"       ERROR downloading log: {exc}")

        excerpt = extract_error_excerpt(text)
        excerpts.append(f"### job: {name}\n{excerpt}")

        print(
            f"       log {len(text.splitlines())} lines "
            f"-> excerpt {len(excerpt.splitlines())} lines"
        )
        print(THIN)
        for line in excerpt.splitlines():
            print(f"  | {line}")
        print(THIN)

        log_sections.append(
            f"{RULE}\nJOB: {name}  (id={job_id})\n"
            f"failed step(s): {', '.join(failed_steps) or 'unknown'}\n"
            f"{RULE}\n{text}\n"
        )

    combined_excerpt = "\n\n".join(excerpts)

    # --- 2. save the untouched logs for inspection ---------------------
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"run_{run_id}.txt"
    header = (
        f"repo: {repo}\nrun_id: {run_id}\n"
        f"workflow: {run.get('name')}\nurl: {run.get('html_url')}\n\n"
    )
    log_path.write_text(header + "\n".join(log_sections), encoding="utf-8")
    print(f"\n  full logs saved -> {log_path}")

    # --- 3. the change that triggered the run --------------------------
    try:
        diff_info = await github_client.get_diff_for_run(payload)
    except github_client.GitHubError as exc:
        print(f"  ERROR fetching diff: {exc}")
        return

    diff_text = diff_info["diff"]
    print(f"\n  DIFF SOURCE: {diff_info['source']} - {diff_info['description']}")
    print(THIN)
    diff_lines = diff_text.splitlines()
    for line in diff_lines[:CONSOLE_DIFF_MAX_LINES]:
        print(f"  | {line}")
    if len(diff_lines) > CONSOLE_DIFF_MAX_LINES:
        print(f"  | ... {len(diff_lines) - CONSOLE_DIFF_MAX_LINES} more lines")
    print(THIN)

    # --- 3.5 has anything like this failed before? ---------------------
    #
    # Runs BEFORE the model is asked anything, because its result is an
    # INPUT to the question. An ordinary function call, not an MCP tool:
    # this is a read, and Phase 5 kept reads out of MCP - see memory.py.
    #
    # Wrapped in its own try/except because memory is an enhancement, not
    # a dependency. If the model fails to load or the query errors, the
    # right outcome is a Phase 5 diagnosis, not no diagnosis at all.
    past = None
    past_summary = ""
    try:
        past = await asyncio.to_thread(
            memory.search_past_failures,
            combined_excerpt,
            # Redelivering a webhook re-processes this same run. Without
            # this the new row would match the row the last delivery just
            # wrote, at ~1.00, and "learn" only that it equals itself.
            exclude_run_id=run_id,
        )
        if past:
            past_summary = past.summary()
            print(THIN)
            for line in past_summary.splitlines():
                print(f"  | {line}")
            print(THIN)
    except Exception as exc:  # noqa: BLE001
        print(f"  memory lookup failed, continuing without it: "
              f"{type(exc).__name__}: {exc}")

    # --- 4. classify, and act on the classification --------------------
    #
    # Everything from here is the graph's job: decide the lane, then run
    # the one action node that lane maps to. main.py deliberately does not
    # know which of the three ran.
    run_attempt = run.get("run_attempt") or 1
    print(f"\n  run_attempt={run_attempt} (1 = first try; >1 blocks a re-run)")

    initial: graph.BuildState = {
        "payload": payload,
        "repo": repo,
        "run": run,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "job_names": job_names,
        "first_step": first_step,
        "log_excerpt": combined_excerpt,
        "diff": diff_text,
        "past_summary": past_summary,
    }

    try:
        final = await graph.GRAPH.ainvoke(initial)
    except diagnose.DiagnosisError as exc:
        print(f"  ERROR diagnosing: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR running the graph: {exc}")
        return

    lane = final.get("lane", diagnose.FALLBACK_CATEGORY)
    diagnosis = final.get("diagnosis", "")
    posted = final.get("posted") or {"target": None, "url": None}

    print("\n  DIAGNOSIS")
    print(THIN)
    for line in diagnosis.splitlines():
        print(f"  | {line}")
    print(THIN)
    print(f"  LANE: {lane}  ACTION: {final.get('action')}")

    # --- 5. keep the record ---------------------------------------------
    #
    # Embed the excerpt so THIS failure is findable by the next one. Done
    # for all three lanes: an amber re-run and a coral escalation are just
    # as worth remembering as a teal explanation, and a memory that only
    # held one lane would quietly bias every future lookup towards it.
    #
    # embeddings.embed is cached, so when memory already embedded this same
    # text a moment ago this is a dictionary lookup, not a second run of
    # the model. None on failure - the row is still stored, just not
    # searchable, which is the same state rows 1-11 started in.
    try:
        vector = await asyncio.to_thread(embeddings.embed, combined_excerpt)
    except Exception as exc:  # noqa: BLE001
        vector = None
        print(f"  ERROR embedding for memory: {type(exc).__name__}: {exc}")

    try:
        row_id = await record_diagnosis(
            run_id=run_id,
            repo=repo,
            log_excerpt=combined_excerpt,
            diff_summary=summarise_diff(diff_text),
            diagnosis_text=diagnosis,
            posted_target=posted.get("target"),
            # The lane that ACTUALLY RAN, which is the operational fact
            # worth querying. When the rerun guard downgrades amber to
            # teal, this says teal - and category_from_model below keeps
            # what the classifier originally chose, so nothing is lost.
            lane=lane,
            extra={
                "workflow": run.get("name"),
                "run_url": run.get("html_url"),
                "failed_jobs": job_names,
                "failed_step": first_step,
                "diff_source": diff_info["source"],
                "diff_ref": diff_info["ref"],
                "model": diagnose.MODEL,
                "posted_url": posted.get("url"),
                "run_attempt": run_attempt,
                "category_from_model": final.get("category"),
                "category_reason": final.get("reason"),
                "guard_note": final.get("guard_note") or None,
                "action": final.get("action"),
                "labels": final.get("labels") or [],
                "rerun_requested": bool(final.get("rerun_requested")),
                # Kept so a stored row shows whether memory influenced it.
                "memory_match": (
                    {
                        "row_id": past.row_id,
                        "run_id": past.run_id,
                        "similarity": round(past.similarity, 4),
                        "lane": past.lane,
                    }
                    if past_summary and past
                    else None
                ),
            },
            embedding=vector,
        )
        print(
            f"  recorded -> postgres diagnoses.id={row_id} lane={lane} "
            f"embedded={vector is not None}"
        )
    except Exception as exc:  # noqa: BLE001
        # Whatever the lane did has already happened at this point, so a
        # storage failure must not look like a total failure.
        print(f"  ERROR writing to the database: {exc}")

    print(RULE + "\n", flush=True)
