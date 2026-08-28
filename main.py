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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# Reads .env into the environment. This has to run BEFORE db is imported:
# db builds its engine at import time and reads DATABASE_URL right then.
# (diagnose and github_client read their variables at call time, so they
# are less fussy - db is not.)
load_dotenv()

import app_auth  # noqa: E402
import admin  # noqa: E402
import dashboard  # noqa: E402
import db  # noqa: E402
import diagnose  # noqa: E402
import embeddings  # noqa: E402
import github_client  # noqa: E402
import graph  # noqa: E402
import memory  # noqa: E402
import user_auth  # noqa: E402
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


app = FastAPI(title="BuildDoctor", version="0.7.0", lifespan=lifespan)

# The dashboard's read-only routes, all under /api. A router rather than a
# second FastAPI app: same process, same connection pool, same lifespan -
# see the module docstring in dashboard.py for why this is not its own
# service.
app.include_router(dashboard.router)

# Approving installations (Phase 13). Its own module, and its own router,
# because it holds the only browser-reachable route in the service that
# writes - see the docstring in admin.py. Mounted after the dashboard so
# that /api/admin/* sits visibly apart from the read routes in the OpenAPI
# schema rather than being mixed in among them.
app.include_router(admin.router)

# The dashboard is served by Vite on port 5173 while the API is on 8000.
# Different port means different ORIGIN, and a browser refuses to hand a
# page the response to a cross-origin request unless the server says it is
# allowed. Without this the network tab shows a 200 and the page still
# shows an error, which is a confusing five minutes the first time.
#
# Dev only, and narrow on purpose. Named origins rather than "*", methods
# limited to the two the dashboard uses. This does not weaken /webhook:
# that is verified by HMAC signature, and CORS is a rule browsers apply to
# scripts, not a lock on the endpoint - curl and GitHub never consult it.
DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.environ.get(
            "DASHBOARD_ORIGINS", ",".join(DEV_ORIGINS)
        ).split(",")
        if origin.strip()
    ],
    # Phase 12: the dashboard now sends a session cookie, and a browser will
    # not attach one to a cross-origin fetch unless the server says it may.
    # This is also why allow_origins can never become "*" - the two are
    # mutually exclusive by specification, and a browser rejects the pair
    # outright rather than quietly picking one.
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# THE SESSION COOKIE (Phase 12).
#
# Added BEFORE the CORS middleware in this file, which means CORS ends up
# OUTSIDE it: Starlette runs the most recently added middleware first. That
# ordering matters for one specific case - a 401 from a signed-out fetch has
# to come back carrying CORS headers, or the browser hides the status code
# behind a generic network error and the dashboard cannot tell "not signed
# in" apart from "the API is down".
#
# WHY A SIGNED COOKIE AND NOT A SESSION TABLE
#
# The alternative is a server-side store keyed by an opaque id. In memory,
# that logs every user out on every cold start, and this instance cold-starts
# constantly. In Postgres, it puts a table on the login path of a service
# whose database is the slowest thing it owns, to hold data that is three
# integers and a string.
#
# The cost is real and worth stating plainly: this cookie is SIGNED, not
# ENCRYPTED. Anyone holding it can read what is inside. That is precisely why
# what goes inside is a GitHub user id, a login, and a list of installation
# ids - all things that person already knows about themselves - and never the
# user access token GitHub issued during sign-in.
#
# max_age matches GitHub's own 8-hour user-token lifetime. Beyond that the
# installation list in the cookie is too old to keep trusting, and the fix is
# to ask GitHub again, which is what signing in does.
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60

# Falling back to a random key would "work" - and would silently invalidate
# every session on every restart, which on this instance means several times
# a day. Failing loudly at boot is better than an app that logs people out
# for no visible reason.
_session_secret = os.environ.get("SESSION_SECRET", "").strip()
if not _session_secret:
    print(
        "  WARNING: SESSION_SECRET is not set - dashboard sign-in is "
        "DISABLED. Generate one with: python -c \"import secrets; "
        "print(secrets.token_hex(32))\"",
        flush=True,
    )
else:
    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret,
        session_cookie="builddoctor_session",
        max_age=SESSION_MAX_AGE_SECONDS,
        same_site="lax",
        # Lax, not Strict. The OAuth callback arrives as a top-level
        # navigation FROM github.com, and Strict would refuse to send the
        # cookie on it - so the state parameter written just before the
        # redirect would be invisible on the way back, and every single
        # sign-in would fail its own CSRF check.
        #
        # Secure only when actually served over https. Hard-coding it true
        # would mean the cookie is silently dropped on http://localhost and
        # local development could never log in at all.
        https_only=user_auth.public_base_url().startswith("https://"),
    )

PROJECT_DIR = Path(__file__).parent
LOGS_DIR = PROJECT_DIR / "logs"

CONSOLE_DIFF_MAX_LINES = 200

RULE = "=" * 74
THIN = "-" * 74


@app.get("/health")
async def health():
    """Liveness check. Open this in a browser to confirm the server is up."""
    return {"status": "ok"}


# The public install URL, github.com/apps/<slug>. Configurable because the
# slug is decided when the App is registered, not when this code is written.
APP_SLUG = os.environ.get("GITHUB_APP_SLUG", "").strip()


@app.get("/", response_class=HTMLResponse)
async def landing():
    """The root page. Phase 9 found a 404 here; this is the minimum fix.

    Deliberately small: what BuildDoctor is, and a way to install it. Phase
    13 can make it presentable once installs from other people actually
    matter. Everything here is static - no database query, so this page still
    answers while the database is waking up.

    The install button is rendered only when GITHUB_APP_SLUG is set. A button
    pointing at github.com/apps/ with no slug is a 404 with extra steps, and
    an honest "not configured yet" is more useful than a broken link.
    """
    install = (
        f'<a class="cta" href="https://github.com/apps/{APP_SLUG}/installations/new">'
        f"Install on GitHub</a>"
        if APP_SLUG
        else '<p class="muted">Install link not configured '
        "(<code>GITHUB_APP_SLUG</code> is unset).</p>"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BuildDoctor</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #0f1115; color: #e6e8ee;
    font: 16px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 2rem;
  }}
  main {{ max-width: 34rem; }}
  h1 {{ font-size: 1.75rem; margin: 0 0 .5rem; letter-spacing: -.01em; }}
  p {{ margin: 0 0 1.25rem; color: #a9b0c0; }}
  .cta {{
    display: inline-block; padding: .6rem 1.1rem; border-radius: .5rem;
    background: #2f81f7; color: #fff; text-decoration: none; font-weight: 600;
  }}
  .cta:hover {{ background: #4b93f8; }}
  .muted {{ font-size: .9rem; }}
  code {{ background: #1b1f27; padding: .1rem .35rem; border-radius: .25rem; }}
  footer {{ margin-top: 2rem; font-size: .85rem; color: #6b7280; }}
  a.plain {{ color: #7aa7e8; }}
</style>
</head>
<body>
<main>
  <h1>BuildDoctor</h1>
  <p>
    When a GitHub Actions build fails, BuildDoctor reads the logs and the
    change that caused it, works out what broke, and says so on the commit
    or pull request.
  </p>
  {install}
  <footer>
    <a class="plain" href="/dashboard">Dashboard</a>
    &nbsp;·&nbsp;
    <a class="plain" href="https://github.com/aeturijayasimhayadav2004/builddoctor">Source</a>
    &nbsp;·&nbsp;
    <a class="plain" href="/health">Status</a>
  </footer>
</main>
</body>
</html>"""


# --------------------------------------------------------------------------
# SIGNING IN (Phase 12)
#
# The same GitHub App that posts the comments also identifies the people who
# look at the results. There is no second OAuth App and no password anywhere
# in this system - see the module docstring in user_auth.py.
#
# Three routes, and the whole flow is:
#
#   /login           mint state + PKCE, stash them in the session, redirect
#   /auth/callback   check state, trade the code, ask GitHub who this is,
#                    ask GitHub what they administer, write the session,
#                    throw the token away
#   /logout          drop the session
# --------------------------------------------------------------------------


def _auth_message(title: str, body: str, status: int) -> HTMLResponse:
    """A plain page for the handful of ways signing in can fail.

    Deliberately not a JSON error: everything that reaches these routes is a
    browser following a redirect, and a human staring at a raw error object
    learns less than they do from one sentence and a link.
    """
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BuildDoctor - {title}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:#0f1115; color:#e6e8ee; padding:2rem;
         font:16px/1.6 ui-sans-serif, system-ui, -apple-system, sans-serif; }}
  main {{ max-width:34rem; }}
  h1 {{ font-size:1.4rem; margin:0 0 .5rem; }}
  p {{ color:#a9b0c0; }}
  a {{ color:#7aa7e8; }}
</style></head>
<body><main>
  <h1>{title}</h1>
  <p>{body}</p>
  <p><a href="/login">Try signing in again</a> &middot; <a href="/">Home</a></p>
</main></body></html>""",
        status_code=status,
    )


@app.get("/login")
async def login(request: Request):
    """Start the GitHub sign-in flow.

    The state and the PKCE verifier go into the session cookie rather than
    into a server-side dictionary, for the same reason the session itself
    does: this instance restarts often, and a login that was in flight across
    a restart would fail with a CSRF error that has nothing to do with CSRF.
    """
    if not user_auth.is_configured() or not _session_secret:
        return _auth_message(
            "Sign-in is not configured",
            "This deployment is missing GITHUB_APP_CLIENT_SECRET or "
            "SESSION_SECRET, so it cannot start a GitHub sign-in.",
            503,
        )

    state = user_auth.new_state()
    verifier, challenge = user_auth.new_pkce_pair()
    request.session["oauth"] = {"state": state, "verifier": verifier}

    return RedirectResponse(
        user_auth.authorize_url(state=state, code_challenge=challenge),
        status_code=302,
    )


@app.get("/auth/callback")
async def auth_callback(request: Request):
    """Where GitHub sends the browser back, carrying a one-time code."""
    params = request.query_params

    # Popped FIRST, before anything is validated. A code can only be redeemed
    # once, and a state can only be answered once; leaving either in the
    # session after a failed attempt would let a second request retry it.
    pending = request.session.pop("oauth", None)

    if params.get("error"):
        # The user pressed Cancel, or the App was suspended. Not an error in
        # this code, so it does not read like one.
        return _auth_message(
            "Sign-in was cancelled",
            f"GitHub reported: {params.get('error_description') or params['error']}",
            400,
        )

    code = params.get("code")
    state = params.get("state")
    if not code or not state:
        return _auth_message(
            "That link is incomplete",
            "GitHub did not send a code and a state, so there is nothing to "
            "verify. Start again from the sign-in link.",
            400,
        )

    if not isinstance(pending, dict) or not hmac.compare_digest(
        str(pending.get("state", "")), state
    ):
        # THE CSRF CHECK. Without it, a crafted /auth/callback link carrying
        # somebody else's code silently signs the victim into the attacker's
        # account. compare_digest rather than == so a mismatch does not leak
        # how many characters were right.
        return _auth_message(
            "That sign-in did not start here",
            "The security token did not match. This happens if the link was "
            "opened out of order, if it was reused, or if the session cookie "
            "expired while GitHub was being asked. Signing in again fixes it.",
            400,
        )

    try:
        token = await user_auth.exchange_code(
            code=code, code_verifier=str(pending.get("verifier", ""))
        )
        user = await user_auth.fetch_user(token)
        installations = await user_auth.fetch_installations(token)
        owner = await user_auth.app_owner()
    except user_auth.UserAuthError as exc:
        print(f"  [auth] sign-in failed: {exc}", flush=True)
        return _auth_message("Sign-in failed", str(exc), 502)
    finally:
        # Not security theatre, but not a guarantee either: it drops this
        # frame's reference so the token is not sitting in a local when a
        # later traceback is rendered. The real protection is that it was
        # never written anywhere.
        token = None

    is_app_owner = bool(owner and owner["id"] == user["id"])

    dashboard.sign_in(
        request,
        user=user,
        installations=installations,
        is_app_owner=is_app_owner,
    )

    print(
        f"  [auth] signed in {user['login']} (id {user['id']}) - "
        f"{len(installations)} installation(s), app_owner={is_app_owner}",
        flush=True,
    )

    return RedirectResponse("/dashboard", status_code=302)


@app.post("/logout")
async def logout(request: Request):
    """Drop the session.

    POST rather than GET on purpose. A GET would be reachable from an
    <img src="/logout"> on any page on the internet, and while being logged
    out is a small harm, it is a pointless one to leave available.

    Nothing is revoked at GitHub here. The authorization granted to the App
    lives in the user's own GitHub settings and is theirs to remove; this
    only forgets it locally.
    """
    dashboard.sign_out(request)
    return {"signed_in": False}


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

    # The App's own lifecycle. Handled INLINE rather than in the background,
    # deliberately: it is a single indexed write, and a workflow_run delivery
    # arriving moments after an install must find the row already there. The
    # 10-second budget is in no danger from one INSERT.
    if event == "installation":
        await handle_installation_event(payload)
        return {"received": True}

    is_failed_run = (
        event == "workflow_run"
        and action == "completed"
        and run.get("conclusion") == "failure"
    )

    if is_failed_run:
        # THE ALLOWLIST GATE.
        #
        # Before any GitHub call, any model call, or any row written. An
        # installation that is not allowed costs one database read and
        # nothing else - no tokens minted, no logs downloaded, no money
        # spent with Groq.
        #
        # The answer is still 200. GitHub judges a webhook endpoint by
        # whether it responded, not by whether it agreed to do anything, and
        # a non-2xx here would show up in the App's delivery log as a broken
        # integration. "Received and deliberately ignored" is a success.
        installation_id = await resolve_and_check(payload)
        if installation_id is None:
            return {"received": True, "skipped": "installation not allowed"}

        # Respond to GitHub immediately; do the slow work afterwards.
        # GitHub times a delivery out after about 10 seconds.
        background.add_task(investigate_failure, payload, installation_id)
        print("  -> failed run detected, diagnosing in the background")

    return {"received": True}


async def resolve_and_check(payload: dict) -> int | None:
    """The installation this event belongs to, if it is allowed to be served.

    Returns the id when the pipeline should run, and None when it should not.
    Collapsing "no installation" and "not allowed" into one None is
    intentional - the caller has exactly one decision to make - but the two
    are logged differently, because they need different fixes.
    """
    repo = (payload.get("repository") or {}).get("full_name")

    try:
        installation_id = await app_auth.resolve_installation_id(payload)
    except app_auth.AppAuthError as exc:
        print(f"  SKIPPED {repo}: could not resolve the installation - {exc}")
        return None

    if installation_id is None:
        print(
            f"  SKIPPED {repo}: this delivery carries no installation and the "
            f"App is not installed on that repository. If this came from the "
            f"old manual webhook, that hook should be deleted."
        )
        return None

    allowed = await asyncio.to_thread(db.installation_is_allowed, installation_id)
    if not allowed:
        # Say which of the two it is. "Never recorded" means the install
        # event was missed or predates this code; "recorded but not allowed"
        # means the gate is working as designed.
        row = await asyncio.to_thread(db.get_installation, installation_id)
        if row is None:
            print(
                f"  SKIPPED {repo}: installation {installation_id} is not in "
                f"the installations table, so it has never been approved."
            )
        else:
            print(
                f"  SKIPPED {repo}: installation {installation_id} "
                f"({row.account_login}) has is_allowed = false."
            )
        return None

    print(f"  installation {installation_id} is allowed")
    return installation_id


async def handle_installation_event(payload: dict) -> None:
    """Record, update, or delete an installation as its lifecycle changes.

    GitHub sends five actions on this event and they are not all the same
    shape, so each is handled rather than assuming "created".

      created                     someone installed the App
      deleted                     someone uninstalled it
      new_permissions_accepted    permissions changed and were accepted
      suspend / unsuspend         temporarily disabled without uninstalling

    Only `deleted` removes the row. A suspended installation still exists
    and keeps whatever approval it had; treating suspension as removal would
    silently revoke an approval that a later unsuspend could not restore.
    """
    action = payload.get("action")
    installation = payload.get("installation") or {}
    account = installation.get("account") or {}

    installation_id = installation.get("id")
    if installation_id is None:
        print("  installation event with no installation.id - ignored")
        return

    installation_id = int(installation_id)
    login = account.get("login") or "unknown"
    account_type = account.get("type")

    if action == "deleted":
        removed = await asyncio.to_thread(db.remove_installation, installation_id)
        # Any cached token for it is now dead. Dropping it here means a
        # reinstall cannot be served by a stale credential.
        app_auth.invalidate(installation_id)
        print(
            f"  UNINSTALLED: installation {installation_id} ({login}) "
            f"{'removed' if removed else 'was not on record'}"
        )
        return

    row, created = await asyncio.to_thread(
        db.upsert_installation,
        installation_id=installation_id,
        account_login=login,
        account_type=account_type,
    )

    if created:
        verdict = "ALLOWED" if row.is_allowed else "NOT allowed"
        print(
            f"  INSTALLED: installation {installation_id} on {login} "
            f"({account_type}) -> {verdict}"
        )
        if not row.is_allowed:
            print(
                f"       every new installation lands closed. Builds from "
                f"{login!r} are skipped until the App owner approves it in "
                f"the dashboard's admin view (or is_allowed is set by hand)."
            )
    else:
        print(
            f"  installation {installation_id} ({login}) updated on "
            f"action={action!r}; is_allowed left at {row.is_allowed}"
        )


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
    installation_id: int,
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
        installation_id=installation_id,
        log_excerpt=log_excerpt,
        diff_summary=diff_summary,
        diagnosis_text=diagnosis_text,
        posted_to=db.posted_to_column(posted_target),
        lane=lane,
        raw=extra,
        embedding=embedding,
    )


async def investigate_failure(payload: dict, installation_id: int) -> None:
    """Run the pipeline, and never let an exception escape.

    This runs as a background task. An exception escaping here is
    reported by the server as "Exception in ASGI application" long after
    the 200 was sent, so nobody is watching - the failure simply looks
    like BuildDoctor ignoring the build.
    """
    run_id = (payload.get('workflow_run') or {}).get('id')
    try:
        await _investigate(payload, installation_id)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  PIPELINE FAILED for run {run_id}: "
              f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        print(RULE + "\n", flush=True)


async def _investigate(payload: dict, installation_id: int) -> None:
    """Gather evidence, classify it, act on the lane, and record it.

    Every GitHub call below authenticates as `installation_id`. It is passed
    in rather than re-derived from the payload, so the value the gate
    approved is provably the same value the work is done with - re-resolving
    it here would open a gap between what was checked and what was used.
    """
    repo = payload["repository"]["full_name"]
    run = payload["workflow_run"]
    run_id = run["id"]

    print("\n" + RULE)
    print("  BUILD FAILED - collecting evidence")
    print(f"  repo={repo}  run_id={run_id}  workflow={run.get('name')!r}")
    print(f"  installation={installation_id}")
    print(f"  {run.get('html_url')}")
    print(RULE)

    # --- 1. which jobs failed, and what did they say -------------------
    try:
        failed_jobs = await github_client.list_failed_jobs(
            installation_id, repo, run_id
        )
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
            text = await github_client.download_job_log(
                installation_id, repo, job_id
            )
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
        diff_info = await github_client.get_diff_for_run(installation_id, payload)
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
        "installation_id": installation_id,
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
            installation_id=installation_id,
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


# --------------------------------------------------------------------------
# THE DASHBOARD ITSELF (Phase 12)
#
# Serving the built React app from this same service, rather than from a
# second host, is a security decision before it is a convenience one.
#
# The session is a cookie. A cookie set by builddoctor.onrender.com is not
# sent on a request originating from some-dashboard.example.com, because
# those are different SITES and SameSite=Lax exists precisely to stop that.
# Working around it means SameSite=None, which means a third-party cookie,
# which browsers are in the middle of removing. Same origin sidesteps the
# entire argument: one host, one cookie, no CORS on the path that matters.
#
# It also means there is exactly one URL to sign in to and exactly one thing
# to deploy, on a free plan that only has room for one service anyway.
#
# html=True makes StaticFiles serve index.html for a directory request, so
# /dashboard works as well as /dashboard/index.html.
# --------------------------------------------------------------------------

DASHBOARD_DIST = PROJECT_DIR / "frontend" / "dist"

if DASHBOARD_DIST.is_dir():
    app.mount(
        "/dashboard",
        StaticFiles(directory=str(DASHBOARD_DIST), html=True),
        name="dashboard",
    )
else:
    # Not an error. The Docker image builds the bundle, but somebody running
    # `uvicorn main:app` straight from a checkout has no dist/ and should be
    # told what to run rather than shown a 404 with no explanation.
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_not_built():
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8>"
            "<title>BuildDoctor - dashboard not built</title>"
            "<body style='font:16px/1.6 system-ui;background:#0f1115;"
            "color:#e6e8ee;padding:2rem'>"
            "<h1>The dashboard bundle is not here</h1>"
            "<p>frontend/dist does not exist in this checkout. Build it with "
            "<code>npm --prefix frontend run build</code>, or use the Vite dev "
            "server on port 5173.</p></body>",
            status_code=503,
        )
