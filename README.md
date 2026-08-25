# BuildDoctor

An agent that watches a GitHub repo and reacts when a CI build fails.

**Current stage: Phase 4.** When a workflow run fails, BuildDoctor fetches the
logs and the triggering diff, asks a model what went wrong *and which of three
lanes the failure belongs in*, then acts on that decision - comment, re-run, or
flag for a human - and stores the whole thing in Postgres. The app and the
database run together under `docker compose`.

## What it does now

1. GitHub sends a `workflow_run` webhook when a run finishes.
2. The signature on that request is verified before anything else happens.
3. If the run failed, BuildDoctor (in the background, so GitHub gets a fast
   reply):
   - lists the jobs that failed and downloads their logs
   - cuts each log down to the lines around its `##[error]` markers
   - fetches the change that triggered the run (PR diff, or commit vs parent)
   - asks an LLM to connect the symptom to the cause **and pick a lane**
   - runs the action for that lane (see below)
   - writes a row to the `diagnoses` table in Postgres, including the lane

## The three lanes

The classifier returns one of three categories, and a small LangGraph state
machine in `graph.py` routes to the matching action node.

| Lane | Colour | When | What it does |
| ---- | ------ | ---- | ------------ |
| `informational` | teal | environment, dependency, config and setup problems - and **anything uncertain** | posts the diagnosis as a PR or commit comment |
| `safe_auto_fix` | amber | the failure looks **flaky**: nothing in the diff explains it and re-running is a reasonable next step | re-runs the failed jobs of that run |
| `needs_review` | coral | secrets / credentials / security scan failures, or a genuine bug in code or test content | on a PR: adds the `needs-review` label **and** comments. On a plain push: comments with a `NEEDS REVIEW` prefix, because a commit comment cannot carry a label |

Teal is the deliberate default. The prompt says so explicitly, the schema
validator falls back to it, and an unrecognised lane routes to it. A needless
extra comment costs nothing; a wrong automated action costs trust.

The rule that separates coral from teal is **where the fix goes**: editing
source or test code is coral; installing a package, creating a directory, or
editing CI config is teal. So a missing *third-party* package (pytest) is teal,
while a missing *first-party* module the project should contain is coral.

### The re-run loop guard

Amber is the only lane that takes an action on your repository, so it has a
hard guard in front of it.

Before re-running anything, BuildDoctor checks `run_attempt` in the webhook
payload. That is **GitHub's own counter**: 1 on the first try, 2 or more after
any re-run. If it is already above 1, this failure *is* the result of a
re-run - so re-running again would start a loop that never ends:

```
fail -> "looks flaky" -> re-run -> fail -> "looks flaky" -> re-run -> ...
```

The evidence barely changes between attempts, so the classifier would keep
reaching the same verdict forever, burning Actions minutes. When the guard
fires, the run is downgraded to teal and gets a comment instead.

Reading GitHub's counter is more robust than BuildDoctor tracking re-runs
itself, for four reasons:

- it arrives in the payload already, so there is no window between triggering
  a re-run and recording that we did
- our own counter would have to survive restarts, image rebuilds and a wiped
  database; `docker compose down -v` would erase it and re-arm the loop
- a **human** pressing "Re-run failed jobs" also increments GitHub's counter,
  so BuildDoctor will not pile a re-run on top of someone else's
- there is no key to get wrong (run id? job id? commit sha?), and a drifting
  key silently disables a guard rather than failing loudly

It is the difference between remembering and observing. Observing is harder to
get wrong.

| Route | Method | Purpose |
| ----- | ------ | ------- |
| `/health` | GET | Returns `{"status": "ok"}`. Confirms the server is alive. |
| `/webhook` | POST | Receives GitHub deliveries. Requires a valid signature. |

## Files

| File | Role |
| ---- | ---- |
| `main.py` | Web routes, signature check, evidence gathering, and recording |
| `graph.py` | The LangGraph state machine: classify, route, act |
| `github_client.py` | All GitHub API calls (logs, diffs, comments, labels, re-runs) |
| `log_excerpt.py` | Cuts a raw CI log down to the lines around the error |
| `diagnose.py` | Asks the LLM for a diagnosis **and** a lane, as structured JSON |
| `db.py` | The `diagnoses` table, and every line of SQL in the project |
| `migrate_jsonl.py` | One-time backfill of the old `diagnoses.jsonl` history |
| `Dockerfile` | Image for the app |
| `docker-compose.yml` | Runs the app and Postgres together |

## Secrets

Copy `.env.example` to `.env` and fill it in. The same `.env` is used both by
`docker compose` and by a local run.

**`GITHUB_TOKEN`** - a fine-grained personal access token from
<https://github.com/settings/personal-access-tokens>, scoped to the one
repository you are watching, with these repository permissions:

| Permission | Level | Needed for |
| ---------- | ----- | ---------- |
| Actions | Read and write | listing jobs and downloading logs (read); re-running failed jobs for the amber lane (write) |
| Contents | Read and write | reading commit diffs, posting commit comments |
| Issues | Read and write | posting pull request comments, adding the `needs-review` label |
| Pull requests | Read and write | reading PR diffs |

With Actions left at Read-only, everything still works except the amber lane,
which reports a 403 and falls back to leaving a comment.

**`WEBHOOK_SECRET`** - any random string. Generate one with:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

The same value goes in the repository's webhook settings, in the **Secret**
field. Without it the server accepts unverified requests and warns loudly.

**`GROQ_API_KEY`** - from <https://console.groq.com> under **API Keys**.
Groq has a free tier, which is why the project points there.

> Groq (an inference provider serving open-weights models) is a different
> company from xAI's Grok, despite the near-identical name. The code is
> provider-agnostic either way.

**`DATABASE_URL`** - optional. Only read when running *without* Docker;
`docker-compose.yml` sets its own value, which wins. See the note on hosts
below.

## Running with Docker (the normal way)

```powershell
docker compose up --build
```

That starts two containers: `postgres` and `app`. Compose waits for
Postgres's healthcheck to pass before starting the app, and the app creates
its table on startup. Confirm at <http://127.0.0.1:8000/health>.

Useful commands:

```powershell
docker compose logs -f app       # follow the pipeline output
docker compose ps                # what is running
docker compose down              # stop; the database survives
docker compose down -v           # stop AND DELETE the database
docker compose up -d --build     # rebuild after a code change
```

The code is baked into the image, so a code change needs `--build`. If you
are iterating quickly, run locally instead (below).

### `localhost` vs `postgres` - the one thing to get right

| Where the app runs | Host in `DATABASE_URL` | Why |
| ------------------ | ---------------------- | --- |
| Inside Docker | `postgres` | the compose **service name**, which compose's internal DNS resolves to the database container |
| Directly on Windows | `localhost:5433` | reaches the same container through its published port |

Inside a container, `localhost` means *that container's own* loopback
interface, where nothing is listening on 5432. Using it produces a bare
`connection refused` that looks like the database is down when it is fine.

### Why port 5433 on the host

This machine already runs a native **PostgreSQL 18** Windows service on 5432.
On Windows both it and Docker can bind that port without an error, and
connections then reach the wrong server - the giveaway is
`FATAL: role "builddoctor" does not exist`. So the container publishes
`5433:5432`. Inside Docker it is still plain 5432; only the host-facing port
moved.

## Running locally without Docker

Useful while iterating, since `--reload` picks up code changes instantly.
Postgres still comes from Docker.

```powershell
docker compose up -d postgres            # database only
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload --port 8000
```

With no `DATABASE_URL` in `.env`, `db.py` defaults to `localhost:5433`, which
is exactly where that container is published.

First-time setup, if the virtualenv does not exist yet:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks the activate script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Requires Python 3.10 or newer. The Docker image uses 3.12.

## Exposing it to GitHub

GitHub cannot reach `127.0.0.1`, so a tunnel is needed. ngrok runs on the
host and points at port 8000 either way - Docker changes nothing here.

```powershell
ngrok http 8000
```

The webhook's Payload URL is the printed HTTPS address with `/webhook`
appended. On the free plan this address changes every restart, and the
webhook settings have to be updated to match.

Webhook settings: content type `application/json`, secret set, and only the
**Workflow runs** event selected.

## Storage

Diagnoses live in the `diagnoses` table in Postgres.

| Column | Type | Notes |
| ------ | ---- | ----- |
| `id` | integer | surrogate key; `run_id` is not unique because a workflow can be re-run |
| `run_id` | **bigint** | must be 64-bit: real run ids are around 3.3e10, well past a 32-bit integer |
| `repo` | varchar(255) | indexed |
| `created_at` | timestamptz | database clock, so all rows share one time source |
| `log_excerpt` | text | the trimmed log that was sent to the model |
| `diff_summary` | jsonb | `files_changed`, `lines_added`, `lines_removed` |
| `diagnosis_text` | text | what the model said |
| `posted_to` | varchar(32) | `pr_comment`, `commit_comment`, or NULL if nothing was posted (the amber lane posts nothing) |
| `lane` | varchar(32) | the lane that **actually ran**: `informational`, `safe_auto_fix`, or `needs_review` |
| `raw` | jsonb | everything else recorded: `run_url`, `posted_url`, `failed_jobs`, `workflow`, `model`, `diff_source`, `diff_ref`, `failed_step`, `run_attempt`, `category_from_model`, `category_reason`, `guard_note`, `action`, `labels`, `rerun_requested` |

`lane` records what happened, not what the model wanted. When the re-run guard
downgrades amber to teal, `lane` says `informational` while
`raw->>'category_from_model'` still says `safe_auto_fix` and
`raw->>'guard_note'` explains why they differ. Find every guarded run with:

```sql
select id, run_id, lane, raw->>'category_from_model', raw->>'guard_note'
from diagnoses where raw->>'guard_note' is not null;
```

Tables are created by `create_all()` on startup. There is no Alembic yet, on
purpose - `create_all` only ever creates what is missing, so the day a column
actually changes is the day migrations earn their place.

Look at the data:

```powershell
docker compose exec postgres psql -U builddoctor -d builddoctor -c "select id, created_at, run_id, posted_to, lane from diagnoses order by id;"
```

Full CI logs are still written to `logs/run_<id>.txt`, which
`docker-compose.yml` bind-mounts to the host so they do not vanish with the
container.

### Migrating the old `diagnoses.jsonl`

Phase 2 appended each diagnosis to `diagnoses.jsonl`. One-time backfill:

```powershell
docker compose up -d postgres
.\.venv\Scripts\Activate.ps1
python migrate_jsonl.py
```

Safe to run more than once - rows are matched on `(run_id, created_at)`, so a
second run inserts nothing. Original timestamps are preserved. The script
never modifies or deletes the `.jsonl`; delete it yourself once the rows are
visible in Postgres.

## Model

`openai/gpt-oss-20b` via Groq's OpenAI-compatible endpoint at
`https://api.groq.com/openai/v1`. Connecting a log to a diff is extraction
and explanation, not multi-step reasoning, so a small model suits it.

### Structured output

The classification comes back as JSON constrained by a schema, sent as
`response_format={"type": "json_schema", ...}` with `strict: true`. The
provider restricts decoding to tokens the schema allows, so `category` cannot
be anything but one of the three lanes - that is enforced *while the tokens
are generated*, not checked afterwards.

Plain JSON mode (`{"type": "json_object"}`) was tried and rejected: it
guarantees the reply parses, but not that it has your keys. Asked for this
schema it returned `failure_type` and `description` instead.

There is still a validate-and-retry-once fallback in `diagnose.py`, because
constrained decoding does not protect against a provider changing what it
supports or a response being truncated. If validation fails twice the lane
becomes `informational` - the fallback is never a lane that takes an action.

Switching providers means changing three constants in `diagnose.py`
(`BASE_URL`, `MODEL`, `API_KEY_ENV`) and adding the matching key to `.env`.
Nothing else in the codebase is provider-specific.

Cost control comes mostly from the excerpt: a raw log is trimmed by roughly
90% before it is sent, which cuts both the bill and the chance of the model
latching onto unrelated warnings.

## Note on version control

`.venv/`, `__pycache__/`, `.env`, `logs/`, and `diagnoses.jsonl` are
gitignored. `.env` holds live credentials and must never be committed - git
history is permanent, so deleting the file in a later commit does not undo
the exposure. `.dockerignore` excludes `.env` for the same reason: image
layers are permanent and shippable too.
