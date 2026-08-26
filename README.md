# BuildDoctor

An agent that watches a GitHub repo and reacts when a CI build fails.

**Current stage: Phase 6.** When a workflow run fails, BuildDoctor fetches the
logs and the triggering diff, **checks whether anything like this has failed
before**, asks a model what went wrong *and which of three lanes the failure
belongs in*, then acts on that decision - comment, re-run, or flag for a
human - and stores the whole thing in Postgres. Those actions are carried out
by a separate **MCP server**, which the app calls as a client. Three
containers run together under `docker compose`.

## What it does now

1. GitHub sends a `workflow_run` webhook when a run finishes.
2. The signature on that request is verified before anything else happens.
3. If the run failed, BuildDoctor (in the background, so GitHub gets a fast
   reply):
   - lists the jobs that failed and downloads their logs
   - cuts each log down to the lines around its `##[error]` markers
   - fetches the change that triggered the run (PR diff, or commit vs parent)
   - **searches its memory for a similar past failure** (see below)
   - asks an LLM to connect the symptom to the cause **and pick a lane**,
     passing any past match in as a hint
   - runs the action for that lane by calling the MCP server (see below)
   - writes a row to the `diagnoses` table in Postgres, including the lane
     **and an embedding of the log, so this failure is findable next time**

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
| `mcp_server.py` | The four GitHub **actions**, exposed as MCP tools. Runs as its own service |
| `mcp_client.py` | How `graph.py` calls those tools, over HTTP |
| `github_client.py` | All GitHub API calls (logs, diffs, comments, labels, re-runs) |
| `log_excerpt.py` | Cuts a raw CI log down to the lines around the error |
| `diagnose.py` | Asks the LLM for a diagnosis **and** a lane, as structured JSON |
| `db.py` | The `diagnoses` table, and every line of SQL in the project |
| `embeddings.py` | Turns a log excerpt into 384 numbers, using a local model |
| `memory.py` | "Has this failed before?" - the similarity lookup and its threshold |
| `migrate_jsonl.py` | One-time backfill of the old `diagnoses.jsonl` history |
| `backfill_embeddings.py` | One-time backfill of embeddings for rows written before Phase 6 |
| `Dockerfile` | One image, used by both the app and the MCP server |
| `docker-compose.yml` | Runs app + mcp + postgres together |

## Memory: has this failed before?

Before Phase 6, every failure was diagnosed as if BuildDoctor had never seen a
build in its life. Now it looks things up first.

The problem with looking things up in CI logs is that keyword search is
useless here - every log contains "error", "pytest" and "exit code 1". So each
`log_excerpt` is turned into an **embedding**: a list of 384 numbers standing
for what the text *means*. Two failures described in different words end up
close together in that space, and "close" is something Postgres can sort by.

| Piece | What it does |
| ----- | ------------ |
| `embeddings.py` | Runs `all-MiniLM-L6-v2` locally on the CPU and returns 384 numbers |
| `db.py` | Stores them in a `vector(384)` column and runs the cosine-distance query |
| `memory.py` | Decides whether the closest row is close **enough** |

### The threshold, and where it came from

`memory.SIMILARITY_THRESHOLD = 0.90`.

This was measured, not guessed. The eleven rows already in the database fall
into four groups that are known to be the same underlying failure, so every
pair has a right answer:

| | Measured cosine similarity |
| --- | --- |
| pairs that **should** match | 0.994 - 1.000 |
| pairs that should **not** | 0.321 - **0.811** |

**0.811 is the number that matters.** That is a failed assertion compared
against a failed import - two completely unrelated problems that happen to
produce the same *shape* of pytest output. The first draft of this file used
0.80, and that dry run is what caught it: at 0.80, those two would have
matched.

0.90 sits near the middle of the empty band between the two ranges, so it is
far from both edges rather than tuned to just clear one of them.

One honest limitation: every true pair above is the *same* fixture failing
twice, so 0.994 is the floor for **identical** failures, not for merely
similar ones. There is no example yet of "similar but not identical", so the
low side of the band is where the real uncertainty lives.

### What happens at the boundary

A decent-but-not-great match returns **None**, not a weak guess. Rejections
print the number so the threshold stays tunable from evidence:

```
memory: closest is row 9 at 0.38, below the 0.90 threshold - treating as no match
```

The costs are not symmetric, which is the whole argument. A hint does not
arrive in the prompt labelled as weak - it looks exactly as authoritative as a
strong one, and it can drag a correct diagnosis towards a failure that never
happened. **Missing a match costs one ordinary diagnosis. A false match costs
a wrong one.**

A run also cannot match **itself**: the lookup excludes the current `run_id`.
Redelivering a webhook re-processes the same run, and without that exclusion
the new diagnosis would match the row the previous delivery just wrote, at
~1.00, and learn only that it equals itself.

### How the hint reaches the model

Only when a match clears the threshold, appended to the user message *after*
the evidence:

```
=== PAST SIMILAR FAILURE (context, may be wrong - see instructions) ===
Date: 2026-08-25
Repository: owner/repo
Workflow run: 32875124103
How it was handled: informational
Similarity to the current failure: 0.99
What was concluded then:
...
```

The system prompt then tells the model how to treat it: decide from the log
and the diff **first**, read the past failure **second**, and if the two
disagree, the evidence wins. Crucially, the categorising steps are fenced off
from it entirely - *"the past failure's category is not evidence and does not
appear anywhere in STEP 1 to STEP 5"*.

That wording is deliberate. Phase 4 established that a **caveat** competing
with an attractive rule loses, while a **gate that terminates** wins. A past
diagnosis is very attractive - it looks like a confident answer that already
exists - so "be careful with it" would not have held. Removing it from the
classification steps' world does.

When nothing matches, the prompt is byte-for-byte the Phase 5 prompt.

### Why this is not an MCP tool

Phase 5 drew the line at reads versus writes, and this stays on the same side
of it.

| | Exposed over MCP? | Why |
| --- | --- | --- |
| post a comment, add a label, re-run a job | **Yes** | they change the outside world, another client would genuinely want them, and each needs its "do not retry" rule published where a client can see it |
| list failed jobs, download a log, fetch a diff | **No** | BuildDoctor's own evidence gathering; changes nothing, and the retry rule lives in one place (`@_reads`) |
| **search past failures** | **No** | same reasoning - a read, gathering better evidence about a failure already being investigated |

It changes nothing and has no side effect worth publishing a hint about.
Putting it behind MCP would add a network hop, a serialisation format, and a
second thing that can be down, in exchange for nothing.

### Why the model weights are baked into the image

The Dockerfile downloads `all-MiniLM-L6-v2` at **build** time rather than at
container start. That makes a bigger image and a more boring runtime, which is
the right trade here.

BuildDoctor sits idle until a webhook arrives. Downloading at first use means
the download happens at *exactly* the moment we can least afford a network
problem - and the failure mode is not "slow", it is "the one build we exist to
explain goes unexplained, and the reason is a stack trace nobody is watching".
Phase 5 already lost a whole background task to one transient DNS blip.

`HF_HUB_OFFLINE=1` is set in the image so this is a rule rather than a
preference: at runtime the library cannot reach the network at all, so a
missing weight fails loudly at startup instead of quietly during an incident.
The model is also loaded during app startup, so the first webhook does not
pay the few seconds it takes to load.

The cost is honest: `sentence-transformers` pulls in PyTorch. The Dockerfile
installs the **CPU-only** build explicitly, because plain `pip install torch`
on Linux drags in the entire CUDA stack for a GPU that does not exist here.
The `app` and `mcp` containers share the same image, so the size is paid once
on disk, not twice.

### Backfilling the rows from before Phase 6

Rows written by Phases 3-5 have no embedding and are invisible to memory:

```powershell
docker compose exec app python backfill_embeddings.py
```

Safe to run repeatedly - it selects only rows `WHERE embedding IS NULL`, so a
second run finds nothing. It reads `id` and `log_excerpt` and writes
`embedding`, and looks at nothing else, which is why row 10 - whose
`posted_url` is NULL because of the Phase 5 structured-output bug - backfills
exactly like every other row. That NULL is a record of a bug in a different
column and has nothing to do with what the log said.

## The MCP server

The three lanes decide *what* should happen. Since Phase 5 they no longer do
it themselves - they ask a separate service.

**MCP (Model Context Protocol)** is a wire protocol for offering capabilities
to an AI client. It distinguishes three kinds of thing:

| | |
| --- | --- |
| **resources** | things a client **reads**, with no side effects |
| **tools** | things a client **invokes**, with side effects expected |
| **prompts** | reusable prompt templates a user picks |

All four of BuildDoctor's actions write to GitHub, so all four are **tools**.

### Why bother

The actions were already four Python functions that worked. Exposing them over
a protocol buys three things:

- **They are usable by something other than BuildDoctor.** Any MCP client -
  Claude Code, an IDE, another agent - can connect and use them, without
  importing this codebase.
- **The boundary is real.** Previously "post a comment" was a function call
  that could quietly grow a dependency on the graph's state. Now it is a
  network call with a declared schema; anything it needs must be an argument.
- **Capability, not just code.** Only the MCP container holds the token that
  writes to GitHub. Actions are reachable exactly one way, through a described
  interface, rather than from anywhere that can `import github_client`.

### The four tools

| Tool | Parameters | Repeatable? |
| ---- | ---------- | ----------- |
| `post_pr_comment` | `repo`, `pull_number`, `body` | **No** - each call adds another comment |
| `post_commit_comment` | `repo`, `commit_sha`, `body` | **No** - each call adds another comment |
| `add_pr_label` | `repo`, `pull_number`, `label` | **Yes** - re-adding a label is a no-op |
| `rerun_workflow_job` | `repo`, `run_id` | **No** - each call burns CI minutes |

A tool's **description** is not documentation. It is what an AI client reads
when deciding whether this is the right call, so each one says what it does,
when to choose it over its neighbour, and what it costs to get wrong.
"Repeatable?" is published in the protocol as the `idempotentHint` annotation,
so a client that never reads this README can still see it.

Only **actions** are exposed. The reads - listing failed jobs, downloading
logs, fetching the diff - stay as direct calls in `main.py`, because they are
BuildDoctor's own evidence gathering, not a capability worth offering anyone.

### Where retry lives, after the refactor

Adding a network hop did **not** add a retry layer. There is still exactly
one, and it is still in `github_client.py`:

| Rule | Where it lives now |
| ---- | ------------------ |
| Reads may retry | `@_reads` in `github_client.py`, untouched - reads were never exposed as tools |
| Writes never retry | `@_writes` in `github_client.py`, **and** `mcp_client.py` makes each call exactly once |
| A missing job log does not abort the run | `main.py`, untouched - log download never went through MCP |
| Background failures cannot vanish | `main.py`'s top-level guard, untouched |

The write rule gets *stronger* with the extra hop, for the same reason it
existed. If an MCP request fails, we cannot tell whether the server performed
the GitHub write before the connection broke - a failed response and a lost
response look identical. Retrying would turn one comment into two.

`mcp_client.py` raises `github_client.GitHubError` on any failure, so the
`except` clauses in `graph.py` are unchanged from Phase 4. The lanes cannot
tell that the transport moved.

### DNS-rebinding protection

The SDK checks the `Host` header against an allowlist and answers **421
Misdirected Request** when it does not match. The default allows only
localhost, which stops a malicious web page resolving a name to `127.0.0.1`
and driving a local MCP server from someone's browser.

Reaching the server as `mcp:8001` therefore fails until that name is
allowlisted, which `mcp_server.py` does via `MCP_ALLOWED_HOSTS`. The check
stays **on** - the fix is naming the hosts actually served, not disabling it.

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

That starts three containers:

| Service | Port | Role |
| ------- | ---- | ---- |
| `postgres` | 5433 (host) | stores diagnoses |
| `mcp` | 8001 | the four GitHub actions, as MCP tools |
| `app` | 8000 | webhook receiver, evidence gathering, the lane graph |

Compose waits for Postgres's healthcheck before starting the app, and the app
creates its table on startup. Confirm at <http://127.0.0.1:8000/health>.

`mcp` has no healthcheck: the endpoint speaks JSON-RPC over a session
handshake, so there is no cheap GET that means "ready". It is not urgent
either - no tool is called until a build actually fails.

Useful commands:

```powershell
docker compose logs -f app       # follow the pipeline output
docker compose logs -f mcp       # watch tool calls arrive
docker compose ps                # what is running
docker compose down              # stop; the database survives
docker compose down -v           # stop AND DELETE the database
docker compose up -d --build     # rebuild after a code change
```

To confirm the app can reach the MCP server and see the four tools exactly as
a client sees them, save this as `probe.py` and run
`docker compose exec app python probe.py`:

```python
import asyncio, os
from mcp.client.client import Client

async def main():
    async with Client(os.environ["MCP_SERVER_URL"]) as c:
        for t in (await c.list_tools()).tools:
            print(t.name, "idempotent =", t.annotations.idempotent_hint)

asyncio.run(main())
```

The code is baked into the image, so a code change needs `--build`. If you
are iterating quickly, run locally instead (below).

### `localhost` vs `postgres` - the one thing to get right

The same rule now applies twice - to Postgres and to the MCP server.

| Where the app runs | `DATABASE_URL` host | `MCP_SERVER_URL` |
| ------------------ | ------------------- | ---------------- |
| Inside Docker | `postgres` | `http://mcp:8001/mcp` |
| Directly on Windows | `localhost:5433` | `http://localhost:8001/mcp` |

Inside a container, `localhost` means *that container's own* loopback
interface, where nothing is listening on 5432 or 8001. Using it produces a
bare `connection refused` that looks like the other service is down when it is
fine. The compose **service name** is what its internal DNS resolves to the
right container.

This is verifiable rather than theoretical - from inside the app container,
`http://mcp:8001/mcp` lists four tools and `http://localhost:8001/mcp` fails.

### Why the Postgres image changed in Phase 6

`postgres:16-alpine` became `pgvector/pgvector:pg16`. pgvector is an
**extension**, not part of Postgres, and the alpine image does not ship it -
`CREATE EXTENSION vector` simply fails there. The replacement is the same
Postgres 16 with the extension installed, so the existing `pgdata` volume is
reused as-is and no data moves.

One caveat came with it: alpine uses musl and this image uses Debian/glibc,
and they sort text slightly differently. Indexes on text columns are built in
sort order, so they need a one-time reindex after the switch:

```powershell
docker compose exec postgres psql -U builddoctor -d builddoctor -c "REINDEX DATABASE builddoctor;"
```

Instant on a table this size, and skipping it is the kind of thing that bites
silently much later.

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
docker compose up -d postgres mcp        # dependencies only
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload --port 8000
```

With no `DATABASE_URL` or `MCP_SERVER_URL` in `.env`, `db.py` defaults to
`localhost:5433` and `mcp_client.py` to `http://localhost:8001/mcp` - exactly
where those two containers are published.

To iterate on the MCP server itself instead, run `docker compose up -d postgres`
and start it locally with `uvicorn mcp_server:app --reload --port 8001`.

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
| `embedding` | vector(384) | the meaning of `log_excerpt`, for memory. NULL means "not searchable yet" |
| `raw` | jsonb | everything else recorded: `run_url`, `posted_url`, `failed_jobs`, `workflow`, `model`, `diff_source`, `diff_ref`, `failed_step`, `run_attempt`, `category_from_model`, `category_reason`, `guard_note`, `action`, `labels`, `rerun_requested` |

`lane` records what happened, not what the model wanted. When the re-run guard
downgrades amber to teal, `lane` says `informational` while
`raw->>'category_from_model'` still says `safe_auto_fix` and
`raw->>'guard_note'` explains why they differ. Find every guarded run with:

```sql
select id, run_id, lane, raw->>'category_from_model', raw->>'guard_note'
from diagnoses where raw->>'guard_note' is not null;
```

`raw->>'memory_match'` records whether a past failure was used for that row,
and how similar it was.

### Schema changes without Alembic

Tables are created by `create_all()` on startup. That handles a brand new
database, and it is exactly why it could not deliver Phase 6 on its own:
`create_all` only ever **creates** what is missing. The `diagnoses` table
already existed with eleven rows in it, so `create_all` saw a table by that
name and moved on. The new column would never have appeared.

So `db.py` also carries a short list of hand-written statements, run in order
on every startup:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS embedding vector(384);
```

Both are `IF NOT EXISTS`, so the second boot and the two-hundredth do nothing.
Order matters - the `vector` type does not exist until the extension is
installed.

This style works because the change is purely **additive**: one new nullable
column. The day a column has to be renamed, retyped, or backfilled with a real
default while holding live data, this stops being enough. That is the day
Alembic earns its place.

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
