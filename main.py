"""BuildDoctor - Phase 2.

End to end: a workflow run fails, BuildDoctor pulls the evidence (the log
around the error, plus the change that triggered the run), asks a model what
went wrong, and posts the answer back where the person who broke it is
looking.

Every diagnosis is also appended to diagnoses.jsonl so the history is not
thrown away. That file is a stand-in for real storage, not the real thing.
"""

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

import diagnose
import github_client
from log_excerpt import extract_error_excerpt

# Reads .env into the environment. Called once, at import time, so every
# os.environ lookup later in the process sees the values.
load_dotenv()

# Models return typographic characters (curly quotes, arrows) that the default
# Windows console codepage cannot encode. Without this, those characters come
# out as mojibake or raise UnicodeEncodeError mid-print.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

app = FastAPI(title="BuildDoctor", version="0.3.0")

PROJECT_DIR = Path(__file__).parent
LOGS_DIR = PROJECT_DIR / "logs"
DIAGNOSES_FILE = PROJECT_DIR / "diagnoses.jsonl"

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


def build_comment(diagnosis: str, run: dict, job_names: list, excerpt: str) -> str:
    """Format the GitHub comment.

    The diagnosis goes first because that is what lands in a notification
    email. The evidence is collapsed underneath for whoever wants to check it.
    """
    failed = ", ".join(f"`{name}`" for name in job_names) or "unknown"
    fence = "```"
    return (
        f"## BuildDoctor: `{run.get('name')}` failed\n\n"
        f"{diagnosis}\n\n"
        f"---\n"
        f"**Failed job(s):** {failed}  \n"
        f"**Run:** [{run.get('id')}]({run.get('html_url')})\n\n"
        f"<details>\n<summary>Log excerpt around the error</summary>\n\n"
        f"{fence}\n{excerpt[:5000]}\n{fence}\n\n</details>\n\n"
        f"<sub>Posted automatically by BuildDoctor "
        f"(model: `{diagnose.MODEL}`). Not a human review.</sub>"
    )


def record_diagnosis(entry: dict) -> None:
    """Append one JSON object as a line to diagnoses.jsonl."""
    with DIAGNOSES_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def investigate_failure(payload: dict) -> None:
    """Gather evidence, diagnose it, post the answer, and record it."""
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
        except github_client.GitHubError as exc:
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

    # --- 4. ask the model ----------------------------------------------
    print("\n  asking the model ...")
    try:
        diagnosis = await diagnose.diagnose_failure(
            log_excerpt=combined_excerpt,
            diff=diff_text,
            repo=repo,
            job_name=", ".join(job_names),
            step_name=first_step,
        )
    except diagnose.DiagnosisError as exc:
        print(f"  ERROR diagnosing: {exc}")
        return

    print("\n  DIAGNOSIS")
    print(THIN)
    for line in diagnosis.splitlines():
        print(f"  | {line}")
    print(THIN)

    # --- 5. post it back to GitHub --------------------------------------
    comment = build_comment(diagnosis, run, job_names, combined_excerpt)
    posted = {"target": None, "ref": None, "url": None}
    try:
        posted = await github_client.post_diagnosis(payload, comment)
        print(f"\n  posted as {posted['target']} comment on {posted['ref']}")
        print(f"  {posted['url']}")
    except github_client.GitHubError as exc:
        print(f"  ERROR posting comment: {exc}")

    # --- 6. keep the record ---------------------------------------------
    record_diagnosis(
        {
            "run_id": run_id,
            "repo": repo,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workflow": run.get("name"),
            "run_url": run.get("html_url"),
            "failed_jobs": job_names,
            "failed_step": first_step,
            "log_excerpt": combined_excerpt,
            "diff_source": diff_info["source"],
            "diff_ref": diff_info["ref"],
            "diff_summary": summarise_diff(diff_text),
            "diagnosis": diagnosis,
            "model": diagnose.MODEL,
            "posted_to": posted["target"],
            "posted_url": posted["url"],
        }
    )
    print(f"  recorded -> {DIAGNOSES_FILE.name}")
    print(RULE + "\n", flush=True)
