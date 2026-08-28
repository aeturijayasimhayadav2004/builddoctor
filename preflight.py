"""Pre-demo preflight: is every moving part of BuildDoctor actually alive?

Run this before showing the project to anybody.

    python preflight.py

BuildDoctor stands on five things it does not own - Render, Neon, Groq,
GitHub, and a cron-job.org ping that keeps the server awake. Any of them can
fail quietly on a free plan, and the failure only becomes visible when
somebody is watching you demo it. This script asks each one directly and
prints a verdict, so the answer takes thirty seconds instead of a panic.

It is read-only. It mints no tokens it does not immediately discard, writes
nothing to the database, and triggers no builds. The one thing it cannot
check by itself is an end-to-end diagnosis, because that needs a real failing
build; `--e2e` prints the command to do that by hand.

Nothing here prints a secret. Keys are reported as present/absent only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

# The service this project actually runs as. Hard-coded rather than
# discovered, because "the service is missing" is itself a finding: if this
# id stops resolving, somebody deleted or recreated the service and the
# webhook URL in the GitHub App settings is now pointing at nothing.
RENDER_SERVICE_ID = "srv-da7u2eek1f9s73bq4qpg"
BASE_URL = "https://builddoctor.onrender.com"
MODEL = "openai/gpt-oss-20b"

# Above this, the server was asleep and something is wrong with keep-warm.
# A warm free instance answers /health in well under a second; a cold one
# takes 50-120s. Three seconds is comfortably between the two.
WARM_SECONDS = 3.0

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str) -> None:
    results.append((status, name, detail))
    mark = {PASS: "  ok  ", WARN: " warn ", FAIL: " FAIL "}[status]
    print(f"[{mark}] {name}: {detail}", flush=True)


def check(name: str):
    """Wrap a check so one broken dependency cannot hide the rest.

    A check that raises is a FAIL, not a crash. The whole point is to see
    every problem in one run rather than fixing them one reboot at a time.
    """

    def wrapper(fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            record(FAIL, name, f"{type(exc).__name__}: {exc}")
        return fn

    return wrapper


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--e2e",
        action="store_true",
        help="also print the manual end-to-end test command",
    )
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()

    import os

    import httpx

    print(f"\nBuildDoctor preflight - {BASE_URL}\n" + "-" * 62)

    # ------------------------------------------------------------------
    # 1. Secrets are present. Not valid - present. Validity is checked by
    #    using them below; this catches an .env that lost a line.
    # ------------------------------------------------------------------
    required = [
        "DATABASE_URL",
        "GROQ_API_KEY",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_B64",
        "GITHUB_APP_CLIENT_ID",
        "GITHUB_APP_CLIENT_SECRET",
        "WEBHOOK_SECRET",
        "SESSION_SECRET",
    ]

    @check("environment")
    def _env():
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            record(FAIL, "environment", f"missing: {', '.join(missing)}")
        else:
            record(PASS, "environment", f"all {len(required)} keys present")

    # ------------------------------------------------------------------
    # 2. Render: is the service suspended, and is the newest deploy live?
    #    Render suspends free services, and a failed deploy leaves the OLD
    #    one serving - so "the site works" does not mean "my last push
    #    shipped". Both are worth knowing before a demo.
    # ------------------------------------------------------------------
    @check("render")
    def _render():
        key = os.environ.get("RENDER_API_KEY")
        if not key:
            record(WARN, "render", "RENDER_API_KEY not set - skipped")
            return
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        service = httpx.get(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}",
            headers=headers,
            timeout=30,
        ).json()
        suspended = service.get("suspended")
        if suspended != "not_suspended":
            record(FAIL, "render", f"service is {suspended!r}")
            return
        deploys = httpx.get(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys?limit=1",
            headers=headers,
            timeout=30,
        ).json()
        latest = (deploys[0].get("deploy") if deploys else {}) or {}
        status = latest.get("status")
        if status == "live":
            record(PASS, "render", f"not suspended; deploy {status}")
        else:
            record(FAIL, "render", f"newest deploy is {status!r}, not live")

    # ------------------------------------------------------------------
    # 3. Is it awake? This is the check that most often catches something,
    #    because keep-warm lives on a third-party cron nobody monitors.
    # ------------------------------------------------------------------
    @check("server")
    def _server():
        start = time.monotonic()
        response = httpx.get(f"{BASE_URL}/health", timeout=180)
        elapsed = time.monotonic() - start
        if response.status_code != 200:
            record(FAIL, "server", f"/health returned {response.status_code}")
        elif elapsed > WARM_SECONDS:
            record(
                WARN,
                "server",
                f"woke from sleep in {elapsed:.0f}s - keep-warm may be dead",
            )
        else:
            record(PASS, "server", f"awake, /health in {elapsed:.2f}s")

    # ------------------------------------------------------------------
    # 4. The dashboard HTML is served, and it is the CURRENT bundle. A
    #    stale bundle is served happily by a healthy server, so uptime does
    #    not prove this.
    # ------------------------------------------------------------------
    @check("dashboard")
    def _dashboard():
        response = httpx.get(f"{BASE_URL}/dashboard/", timeout=60, follow_redirects=True)
        body = response.text
        if response.status_code != 200:
            record(FAIL, "dashboard", f"returned {response.status_code}")
        elif "index-" not in body:
            record(FAIL, "dashboard", "served HTML has no JS bundle reference")
        else:
            bundle = body.split("index-")[1].split(".js")[0]
            record(PASS, "dashboard", f"serving bundle index-{bundle}.js")

    # ------------------------------------------------------------------
    # 5. Database reachable, and the gate state of every installation. This
    #    is the check that would have caught the org sitting at
    #    is_allowed=false after the Phase 14 withdrawal test.
    # ------------------------------------------------------------------
    @check("database")
    def _database():
        import db

        rows = db.list_installations()
        if not rows:
            record(WARN, "database", "reachable, but no installations")
            return
        blocked = [r for r in rows if not r.is_allowed]
        summary = ", ".join(
            f"{r.account_login}({'open' if r.is_allowed else 'BLOCKED'})" for r in rows
        )
        if blocked:
            record(WARN, "database", f"{len(rows)} installs - {summary}")
        else:
            record(PASS, "database", f"{len(rows)} installs, all open - {summary}")

    # ------------------------------------------------------------------
    # 6. Groq: key works AND the exact model still exists. Providers retire
    #    models on their own schedule; a 404 on the model id is the failure
    #    that turns every diagnosis into a fallback.
    # ------------------------------------------------------------------
    @check("groq")
    def _groq():
        key = os.environ.get("GROQ_API_KEY")
        response = httpx.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
        if response.status_code != 200:
            record(FAIL, "groq", f"models endpoint returned {response.status_code}")
            return
        ids = {m["id"] for m in response.json().get("data", [])}
        if MODEL in ids:
            record(PASS, "groq", f"key valid, {MODEL} available")
        else:
            record(FAIL, "groq", f"{MODEL} NO LONGER LISTED - diagnosis will fail")

    # ------------------------------------------------------------------
    # 7. GitHub App identity, permissions and event subscriptions. A
    #    permission silently widened or an event unticked changes behaviour
    #    without touching the code.
    # ------------------------------------------------------------------
    @check("github app")
    def _github():
        import app_auth

        response = httpx.get(
            "https://api.github.com/app", headers=app_auth.app_headers(), timeout=30
        )
        if response.status_code != 200:
            record(FAIL, "github app", f"GET /app returned {response.status_code}")
            return
        data = response.json()
        perms = data.get("permissions", {})
        events = set(data.get("events", []))
        problems = []
        if perms.get("contents") != "read":
            problems.append(f"contents is {perms.get('contents')!r}, expected read")
        for needed in ("workflow_run", "repository"):
            if needed not in events:
                problems.append(f"not subscribed to {needed}")
        detail = f"{data.get('slug')}, {data.get('installations_count')} installs"
        if problems:
            record(WARN, "github app", f"{detail} - " + "; ".join(problems))
        else:
            record(PASS, "github app", f"{detail}, perms and events correct")

    # ------------------------------------------------------------------
    # 8. Recent webhook deliveries. GitHub keeps the outcome of every one,
    #    so this answers "did anything fail to reach us lately" without
    #    needing the server logs.
    # ------------------------------------------------------------------
    @check("webhooks")
    def _webhooks():
        import app_auth

        response = httpx.get(
            "https://api.github.com/app/hook/deliveries?per_page=20",
            headers=app_auth.app_headers(),
            timeout=30,
        )
        if response.status_code != 200:
            record(WARN, "webhooks", f"deliveries returned {response.status_code}")
            return
        deliveries = response.json()
        if not deliveries:
            record(WARN, "webhooks", "no recent deliveries recorded")
            return
        bad = [d for d in deliveries if d.get("status_code", 0) >= 300]
        newest = deliveries[0].get("delivered_at")
        if bad:
            record(
                WARN,
                "webhooks",
                f"{len(bad)}/{len(deliveries)} recent deliveries failed",
            )
        else:
            record(
                PASS,
                "webhooks",
                f"last {len(deliveries)} delivered ok, newest {newest}",
            )

    # ------------------------------------------------------------------
    # 9. The gate actually refuses an anonymous caller. This is a security
    #    check, and it is here because a demo is exactly when somebody
    #    opens the API in a browser tab.
    # ------------------------------------------------------------------
    @check("auth gate")
    def _auth():
        protected = ["/api/stats", "/api/diagnoses", "/api/admin/installations"]
        leaks = []
        for path in protected:
            code = httpx.get(f"{BASE_URL}{path}", timeout=60).status_code
            if code != 401:
                leaks.append(f"{path}->{code}")
        if leaks:
            record(FAIL, "auth gate", "NOT protected: " + ", ".join(leaks))
        else:
            record(PASS, "auth gate", f"all {len(protected)} routes return 401")

    # ------------------------------------------------------------------
    # 10. Local git hygiene. Unpushed commits mean the repo an interviewer
    #     reads is not the code you are describing.
    # ------------------------------------------------------------------
    @check("git")
    def _git():
        out = subprocess.run(
            ["git", "log", "--oneline", "origin/master..master"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        unpushed = [line for line in out.stdout.splitlines() if line.strip()]
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
        notes = []
        if unpushed:
            notes.append(f"{len(unpushed)} unpushed commit(s)")
        if dirty:
            notes.append(f"{len(dirty.splitlines())} uncommitted file(s)")
        if notes:
            record(WARN, "git", "; ".join(notes))
        else:
            record(PASS, "git", "clean and pushed")

    # ------------------------------------------------------------------
    print("-" * 62)
    failed = [r for r in results if r[0] == FAIL]
    warned = [r for r in results if r[0] == WARN]
    print(
        f"{len(results) - len(failed) - len(warned)} pass, "
        f"{len(warned)} warn, {len(failed)} fail"
    )

    if args.e2e:
        print(
            "\nEnd-to-end test (needs a real failing build):\n"
            "  gh api -X PUT repos/builddoctor-testing/ci-test/contents/trigger.txt \\\n"
            "    -f message='preflight' -f content=\"$(date | base64)\" \\\n"
            "    -f sha=\"$(gh api repos/builddoctor-testing/ci-test/contents/trigger.txt "
            "--jq .sha 2>/dev/null)\"\n"
            "  then watch the commit for a builddoctor-ci[bot] comment (~30s)."
        )

    if failed:
        print("\nDO NOT DEMO until the FAIL lines above are resolved.")
        return 1
    if warned:
        print("\nUsable, but read the warn lines - they are the ones that bite.")
        return 0
    print("\nAll green. Safe to demo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
