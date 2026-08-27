# BuildDoctor

An agent that watches a GitHub repo and reacts when a CI build fails.

**Current stage: Phase 10 - secrets rotated, production kept warm.** The
backend runs in production at **<https://builddoctor.onrender.com>** and the
dashboard at **<https://builddoctor-dashboard.onrender.com>**, against a Neon
Postgres. See [Production](#production) for the topology and why it is shaped
that way.

> The dashboard has **no authentication** and `/api/diagnoses` returns every
> row to anyone who asks. That is Phase 11's job. Until then, treat the
> dashboard URL as readable by anyone who finds it.

When a workflow run fails, BuildDoctor fetches the
logs and the triggering diff, **checks whether anything like this has failed
before**, asks a model what went wrong *and which of three lanes the failure
belongs in*, then acts on that decision - comment, re-run, or flag for a
human - and stores the whole thing in Postgres. Those actions are carried out
by a separate **MCP server**, which the app calls as a client. Everything it
has ever concluded is visible on a **React dashboard**. Four containers run
together under `docker compose`.

And it is now **measured** rather than asserted: against a golden set of 24
failures with known-correct answers, it picks the right lane **22 times out of
24 (91.7%)**. See [the eval suite](#does-it-actually-work-the-eval-suite).

Phase 8 found three real defects and deliberately fixed none of them, so the
numbers would not be contaminated by guessing. Phase 8.5 fixed exactly those
three and re-ran the whole set. Two cases were fixed, **two regressed**, and the
total stayed at 22/24 - which is precisely why the eval reports a per-case diff
and not a percentage. The details are in
[`eval/findings.md`](eval/findings.md).

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
4. Every row it has ever written is listed on the **dashboard** at
   <http://localhost:5173>, with no build needed to see it.
5. A **golden set of 24 known-correct cases** can be run against the real
   classifier at any time, without touching GitHub or the `diagnoses` table,
   to check that a change made it better and not worse.

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
| `dashboard.py` | The dashboard's read-only `/api` routes. No writes anywhere in it |
| `embeddings.py` | Turns a log excerpt into 384 numbers, using a local model |
| `memory.py` | "Has this failed before?" - the similarity lookup and its threshold |
| `migrate_jsonl.py` | One-time backfill of the old `diagnoses.jsonl` history |
| `backfill_embeddings.py` | One-time backfill of embeddings for rows written before Phase 6 |
| `Dockerfile` | One image, used by both the app and the MCP server |
| `docker-compose.yml` | Runs app + mcp + postgres + frontend together |
| `frontend/` | The React + TypeScript dashboard (Vite). See below |
| `eval/golden_set.json` | 24 cases with known-correct lanes. The measuring stick |
| `eval/run_eval.py` | Runs the golden set against the real classifier. Writes nothing |
| `eval/report.md` | The numbers from the last run. Regenerated every run |
| `eval/findings.md` | What the numbers mean. Written by hand, survives a re-run |
| `eval/baseline_phase8.json` | The Phase 8 run, frozen. Every later run is diffed against it |
| `eval/report_phase8.md` | The Phase 8 report, kept for comparison |
| `eval/probe_*.py` | Two read-only probes, kept as evidence for a Phase 8 finding |

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

### The untested middle, now measured

This was Phase 6's open question and Phase 7 deliberately left it alone.
**Phase 8 measured it.** The section is kept rather than deleted, because the
reasoning that made it a question is still the reasoning that makes the answer
worth trusting.

**The question was:** every similarity ever seen on real data had landed in one
of two clumps - about 1.00 when the same fixture failed twice, and 0.83 or
below when two failures were genuinely unrelated. Nothing had ever scored
between 0.83 and 0.99, so the threshold had been proven at its edges and never
in its middle.

**The answer:** six cases were built to be deliberately similar-but-different
and three of them landed in the empty band. All three should have matched, and
all three did.

| Case | Phase 8 | Phase 8.5 | Should match? | Correct now? |
| ---- | ------- | --------- | ------------- | ------------ |
| assertion failure, different file and numbers | 0.9254 | 0.9254 | yes | yes |
| apt missing package, different package name | 0.9431 | 0.9431 | yes | yes |
| `ModuleNotFoundError`, different module | 0.9763 | 0.9763 | yes | yes |
| same bad pin, different package | **0.7959** | **0.9600** | yes | **yes, fixed** |
| Node `Cannot find module` vs Python `No module named` | 0.7429 | 0.7429 | **no** | yes |
| pip DNS failure vs pip bad-version rows | 0.5687 | 0.6439 | **no** | yes |

**Phase 8.5 made this 6 for 6.** The one miss is described below; it turned out
not to be the threshold's fault.

The numbers that matter:

- highest score that should **not** have matched: **0.8333**
- lowest score that **should** have matched: **0.9254**
- the gap between them: **0.0921**, and **0.90 sits inside it**

So the threshold survives its first real test. The gap is far narrower than
Phase 6's 0.811-to-0.994, which is exactly what should happen once genuinely
similar-but-different cases exist - but it is still a gap, and 0.90 is still
inside it rather than balanced on an edge.

**The threshold was not changed.** It did not need to be, and Phase 8's rule
was to measure, not tune.

### The thing the eval found instead - since fixed

One middle-zone case failed, and chasing it turned up something more important
than the threshold. **Phase 8 found this and deliberately left it alone; Phase
8.5 fixed it.** The description below is what was wrong, followed by what
changed.

`pip install requests==999.999.999` scored only **0.7959** against the rows for
`pip install pytest==99X.X.X` - the same failure, same file, same fix, only the
package name different. Its nearest neighbour was an unrelated "tests/ not
found" row.

The cause is not the threshold. When pip rejects a version it prints **every
version it does know about**, and for those rows that list is 1368 characters.
The embedding model reads at most 256 word-pieces, so **81% of those rows is
discarded**, and much of what survives is version numbers rather than the
failure.

Pasting that same list of version numbers into a completely unrelated failure
moves it **+0.27 closer** to those rows. Memory is partly matching on
incidental output volume rather than on the failure.

**The fix, in Phase 8.5.** `embeddings.clean()` now collapses any run of three
or more comma-separated version-like tokens to a single constant marker, before
the truncation rather than after it. Three and not two, because a real sentence
can mention a pair of versions. The marker carries **no count** on purpose:
`<47 versions>` versus `<52 versions>` would put back exactly the incidental
difference being removed.

What the embedder sees for row 9:

| | Before | After |
| --- | --- | --- |
| After `clean()` | 2000 chars (truncated) | **1440** |
| Tokens | 1354 | **524** |
| Discarded by the 256-token window | **1098 (81%)** | 268 (51%) |
| Is `No matching distribution found` inside the window? | **NO** | **YES** |

Every stored vector was stale the moment that changed, so all 13 rows were
re-embedded with `backfill_embeddings.py --all`. The offending case went from
0.7959 to **0.9600**, and its nearest neighbour moved from an unrelated
"tests/ not found" row to an actual pytest version pin - matching the right
cluster for the right reason.

The change was surgical, which was not guaranteed: of all 24 scores, 20 moved
by exactly 0.0000, two by 0.0002, and **no case lost a match**. Written up with
the probe scripts that prove both halves in
[`eval/findings.md`](eval/findings.md).

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

**When `embeddings.clean()` changes, every stored vector is stale even though
none of them is NULL**, and the default mode will report "nothing to backfill"
while the table quietly holds a mixture of old and new vectors whose similarity
scores mean nothing. That is what `--all` is for:

```powershell
docker compose exec app python backfill_embeddings.py --all
```

It **overwrites** existing vectors, so it is not the default. It is recoverable
- the log excerpts it reads from are never touched, so it can simply be run
again - but it is not a no-op. Phase 8.5 changed the cleaning and re-embedded
all 13 rows this way.

## The dashboard

A web page listing every diagnosis BuildDoctor has ever made, so the project
can be shown to someone without waiting for a build to break on cue.

```powershell
docker compose up --build
```

Then open **<http://localhost:5173>**. The API it reads is on port 8000; both
have to be up.

| Piece | Where | Role |
| ----- | ----- | ---- |
| `dashboard.py` | in the app | two read-only routes under `/api` |
| `frontend/` | its own container | React + TypeScript, served by Vite |

### Why the routes live in the existing app

The obvious-looking move is a small backend of its own. It would need a second
image, a second connection pool to the same database, a second entry in
compose, and a second thing that can be down - and in exchange it could do
nothing the app cannot already do. `db.py` already owns the pool and already
knows the schema.

That is Phase 5's rule applied one layer up. Phase 5 exposed four MCP tools and
stopped, because a capability only earns a new surface when something actually
needs it to be separate. Reading rows the app already owns does not qualify.

Everything in `dashboard.py` reads. There is no route there that writes,
deletes, re-runs or posts, which is what makes it safe to leave open to a
browser with nothing in front of it.

### The two routes

| Route | Returns |
| ----- | ------- |
| `GET /api/stats` | totals, the lane breakdown as percentages, and the memory hit rate |
| `GET /api/diagnoses?limit=100` | every diagnosis, newest first, flattened for the UI |

`limit` is a **ceiling, not pagination**. There are thirteen rows; paging would
be machinery guarding a problem that does not exist. It is written down in
`db.py` rather than left to be rediscovered: once this table holds a few
thousand rows, the endpoint starts shipping every log excerpt in the database
on every page load, and the fix at that point is a cursor on `created_at` plus
a truncated excerpt in the list view, with the full text fetched only when a
row is expanded.

Half the fields the page shows are not columns - `posted_url`, `run_url`,
`workflow` and `memory_match` all live inside the `raw` JSONB blob.
`dashboard.py` flattens them, so the frontend never has to know which fields
were promoted to real columns and which were not, and promoting one later
changes nothing on the other side of the wire.

### What each stat means

| Card | Meaning |
| ---- | ------- |
| **Diagnoses** | Every row ever written, across every watched repository |
| **By lane** | Share of diagnoses per lane. **Includes a grey `unclassified` slice** for rows 1-4, which were diagnosed before Phase 4 invented lanes |
| **Memory hits** | Of the failures where memory actually ran, how many found a past match above 0.90 |
| **Searchable** | Rows that have an embedding, so a future failure can find them |

**Memory hits deserves its footnote.** The denominator is *not* the total
number of diagnoses. Eleven of the thirteen rows were written before Phase 6
existed and were never asked, so counting them as misses would report a hit
rate of 1-in-13 for a feature that has only ever run twice. The card shows
"1 of 2" underneath the percentage for exactly that reason - the denominator is
the honest part of the number.

The distinction is carried through to the table too. The memory column has
**three** states, not two:

| Shown | Means |
| ----- | ----- |
| `#10 · 100.0%` | memory matched that row, at that similarity, and the model was given it as a hint |
| `no match` | memory ran and deliberately returned nothing - the threshold working, not a failure |
| `—` | memory did not exist when this row was written |

### Nulls

Several rows have one, for unrelated reasons, and every one of them shows a
dash rather than the word `undefined`:

- **row 10** has no `posted_url` - a Phase 5 bug lost it
- **every amber run** has no `posted_url` either, because that lane re-runs a
  job and posts no comment at all
- **rows 1-4** have no `lane`, because they predate Phase 4

The TypeScript types in `frontend/src/api.ts` declare each of these as
`| null`, so the compiler refuses to build code that reads through one without
checking. That is most of the argument for TypeScript on a page this small:
the response shape is exactly the kind of thing that is easy to get subtly
wrong, and this project has already proved that once - row 10's missing url is
a Phase 5 untyped-dict bug that a type would have caught.

### Filtering by lane

The legend in the **By lane** card is not a legend, it is a control. Clicking
`needs review` reduces the table to those four rows and dims the other slices
in the bar; clicking it again, or the `clear` button next to the heading,
brings everything back. It is the fastest way to answer the only question
anyone actually asks of this page - *which ones need me?*

The filtering happens in the browser. All thirteen rows are already in memory,
so asking the server to re-send a subset of what the page is holding would be
slower and would make the API responsible for a purely visual choice.

Each legend row is a real `<button>` with `aria-pressed`, not a `<div>` with a
click handler. That is what gets it Tab focus, Enter and Space, and a correct
screen-reader announcement without any of it being written by hand.

### How the page is put together

| Decision | Why |
| -------- | --- |
| **Dark only** | This is an ops view that sits next to a terminal. There is no light theme to keep in sync, so every colour is stated once and meant. |
| **IBM Plex Sans + JetBrains Mono** | Mono for anything that is an id, a percentage or a log line, so digits line up in a column and stop twitching between rows. Both have real system fallbacks, so the page survives with no network - it just loses the typography. |
| **SVG icons, never text glyphs** | A character like `▸` renders at a different size, weight and baseline in every font on every platform. The previous version used one, and it could not be aligned reliably. |
| **Lane = colour *and* a word** | Every badge carries a dot and a label. Colour alone would make the lane unreadable in a greyscale screenshot, on a bad projector, or to anyone who cannot separate teal from amber. |
| **Skeletons, not a spinner** | Grey blocks the shape of what is coming stop the layout jumping when the data lands. A screen-reader-only `role="status"` says "Loading diagnoses" for anyone who is not looking at the blocks. |
| **`prefers-reduced-motion` honoured** | Every animation on the page is decoration over content that is already complete, so when the operating system asks for it to stop, it stops. |

Contrast is **measured, not eyeballed**. The browser check computes the real
WCAG ratio from the rendered colours, and it earned its keep immediately: the
faint grey used for footnotes and table headers came out at 4.11:1 and 3.63:1,
which looked perfectly fine on screen and was still below the 4.5:1 floor. It
is now `#7d8a9f`, at 5.5:1 and 4.9:1.

### This is a DEV setup

The `frontend` container runs the **Vite dev server**. It compiles each file on
demand and pushes changes straight into the open browser tab, which is what
makes editing a component feel instant. It is also why it must not ship: it
holds the whole toolchain in memory, serves hundreds of small unoptimised
modules, and reports errors in a way meant for the person writing the code.

**Phase 9 (deploy) will have to change this**, and it is worth knowing what
that involves now rather than being surprised by it:

1. `npm run build` typechecks and bundles everything into a handful of hashed
   files in `dist/` - currently about 200 kB of JavaScript, 64 kB gzipped.
2. A two-stage Dockerfile: stage one runs that build, stage two copies **only**
   `dist/` into a small static web server (nginx or Caddy) and discards node
   entirely. The shipped image has no npm, no source and no dev server in it.
3. `VITE_API_BASE` stops being a localhost url and becomes wherever the API
   actually lives - and it gets fixed at **build** time rather than read at
   start-up, which is the same build-time-versus-runtime tradeoff the embedding
   weights posed in Phase 6.
4. CORS narrows from the two localhost dev origins to the real one, or
   disappears entirely if the static files end up served from the same origin
   as the API.

### `localhost:8000`, not `app:8000`

Everywhere else in `docker-compose.yml`, one service reaches another by its
compose service name - the app finds the database at `postgres` and the MCP
server at `mcp`. The frontend is the exception:

```yaml
VITE_API_BASE: http://localhost:8000
```

Not because the rule is different, but because the **audience** is. That value
is baked into JavaScript that runs in a browser **on the host**. The browser is
not on the compose network and has never heard of a service called `app`, so it
needs a url the host can open. `http://app:8000` fails with a DNS error in the
browser console while every container looks perfectly healthy from the inside.

It is the same lesson as `localhost` vs `postgres` further up this file, seen
from the other end: there, `localhost` was wrong because the code ran inside a
container; here, the service name is wrong because the code runs outside one.

## Does it actually work? The eval suite

BuildDoctor scores **22 out of 24 (91.7%)** against a golden set of failures
with known-correct lanes. Excluding the three cases deliberately written to be
ambiguous, 20 of 21 (95.2%).

```powershell
docker compose run --rm --no-deps app python eval/run_eval.py
```

Takes about ten minutes, and that is a floor rather than slowness - see
"the rate limit" below. Results land in
[`eval/report.md`](eval/report.md); the analysis lives in
[`eval/findings.md`](eval/findings.md).

| Breakdown | Phase 8 | Phase 8.5 |
| --------- | ------- | --------- |
| Overall | 22/24 (91.7%) | **22/24 (91.7%)** |
| Excluding 3 ambiguous cases | 20/21 (95.2%) | 20/21 (95.2%) |
| `informational` (teal) | 11/12 | 11/12 |
| `safe_auto_fix` (amber) | 3/3 | **2/3** |
| `needs_review` (coral) | 8/9 | **9/9** |
| Middle zone | 5/6 | **6/6** |
| Live cross-check | 4/4 | **4/4 agree** with what the live pipeline recorded |

**The identical total hides four changes**, which is the single most useful
thing this suite has done. Phase 8.5 fixed two cases (`syn-08`, `mid-06`) and
broke two (`hist-02`, `mid-05`). A percentage cannot show a trade, so the runner
renders a per-case diff against a frozen `baseline_phase8.json` and prints
regressions as their own line whatever the aggregate did.

The amber row is the one worth pausing on, in both directions. The database
contains **zero** examples of `safe_auto_fix` - no watched build has ever failed
in a way the model judged flaky - so this lane is the least observed in
production and the most dependent on written cases. It went 3/3 to 2/3, and the
cause is understood: enforcing step order in Phase 8.5 revealed that **STEP 2
(machinery) is asked before STEP 3 (flaky)**, so a transient failure in a run
that touched `ci.yml` is caught by the machinery question first. That is a
pre-existing ordering defect the fix exposed rather than created, and it is
[written up rather than patched](eval/findings.md) - the golden set has exactly
one amber case with a machinery diff, which is too thin to tune against.

### The golden set

[`eval/golden_set.json`](eval/golden_set.json) holds 24 cases in four groups:

| Group | Count | What it is for |
| ----- | ----- | -------------- |
| historical | 6 | Real excerpts from rows 1-13, verbatim |
| synthetic | 8 | Each lane covered clearly, including 2 deliberately ambiguous |
| middle-zone | 6 | Similar-but-different pairs, to probe the memory threshold |
| live-crosscheck | 4 | Replays of real runs, scored against what the live pipeline stored |

**The stored lane is not the expected lane, and that distinction is the whole
point.** Every `expected_lane` is derived by hand from `diagnose.py`'s own
STEP 1-5 and carries a `lane_rationale` naming the step it came from. What the
database recorded is kept separately as `recorded_lane`.

That is not pedantry. Rows 5 and 9-12 are the *same failure* - a pytest version
pinned in `ci.yml` that does not exist - and row 5 is stored as `needs_review`
while 9-12 are stored as `informational`. Scoring against stored lanes would
have measured whether the model agrees with its own past self, which is not the
same question as whether it is right. (Replaying row 5 today returns
`informational`, matching the others. The prompt was sharpened in between.)

### Calling the real thing without real side effects

The usual way to build a harness like this is to reimplement the interesting
part and then measure the reimplementation, which produces a number about the
test rather than about the system. Nothing here is reimplemented.

The eval calls **`graph.classify(state)`** - not a copy of it, the actual
LangGraph node the live pipeline runs, with the real prompt and the real rerun
guard. It is safe for a structural reason rather than a careful one: posting,
labelling and re-running do not live in `classify()`. They live in three
*sibling* nodes that only execute when the compiled graph routes to them.
Calling the node directly means routing never happens, so that code is never
entered. The eval is not avoiding the side effects - it never reaches the code
that has them.

It also calls **`memory.search_past_failures(...)`**, the real lookup with the
real threshold, alongside `db.nearest_by_embedding` to capture the raw scores
that the gated function discards when it rejects a match.

On top of that, `install_write_landmines()` replaces every write path in the
process - `db.save_diagnosis`, `db.set_embedding`, and all of `mcp_client`'s
posting functions - with functions that **raise**. Deliberately not mocks: a
mock returns something plausible and lets execution continue, which is exactly
how a harness ends up posting to a real repository while its author is certain
it cannot. None of them fired.

So there are two independent reasons nothing can happen: the code is never
reached, and it would explode if it were.

### The rate limit

Groq's free tier allows 8000 tokens per minute for this model, and one case
costs 1900-3350 prompt tokens because CI logs are long. That caps the whole
thing at roughly two and a half cases per minute.

The first run used three concurrent requests and no backoff. **Ten of 24 cases
came back 429**, were dropped, and the script printed a confident accuracy
computed over the surviving 14 - the most dangerous shape a test can have. The
harness now paces one request every 24 seconds and waits out the delay the
provider names.

`diagnose.py` was not touched for this. Being rate limited is a property of
running 24 cases in a burst against a free tier, so the retry lives in the
harness that is being limited.

### Triggering a genuinely live run

The `live-crosscheck` group replays real excerpts and compares against what the
pipeline stored, which proves the eval path and the live path agree. It does
**not** re-test webhook delivery, evidence gathering or posting. Doing that
needs a real broken build, which is a manual job:

1. `docker compose up` and confirm all four containers are healthy.
2. In a second terminal, `ngrok http 8000`, and copy the `https://` URL.
3. In the watched repository's **Settings > Webhooks**, set the payload URL to
   `<ngrok-url>/webhook`, content type `application/json`, secret matching
   `WEBHOOK_SECRET` in `.env`, and subscribe to **Workflow runs** only.
4. Push a commit that breaks the build. The simplest is one line in
   `.github/workflows/ci.yml`: change `pip install pytest` to
   `pip install pytest==997.997.997`.
5. Watch the app container's logs. Expect `memory:` then `[classify]` then one
   of `[teal]` / `[amber]` / `[coral]`.
6. The new row appears on the dashboard at <http://localhost:5173>.

Note that ngrok issues a **new URL every restart** on the free tier, so step 3
has to be redone each session.

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

### How a secret actually leaked here, and what rotation did about it

Phase 10 rotated every credential this project uses. The interesting part is
not the rotation, it is **where the exposure turned out to be**, because that
determined whether rotating was sufficient at all.

**The distinction that mattered.** A secret committed to git and later deleted
is still in the object database forever - anyone who can clone the repository
can recover it, and rotating closes future use without un-exposing the old
value. A secret that leaked somewhere else can be fully retired by rotation.
These need different responses, so the vector was established *before* anything
was changed.

Five surfaces were scanned by comparing raw bytes, reporting booleans only:

| Surface | How | Result |
| ------- | --- | ------ |
| Git object database | all 99 blobs via `git cat-file --batch-all-objects`, which enumerates the object store itself and so includes **unreachable** objects that `git log` would never walk | clean |
| Was `.env` ever committed | `git log --all --full-history` | never - only `.env.example`, which holds placeholders |
| Working tree | every file except `.env` | clean |
| The deployed dashboard bundle | fetched live and searched | clean |
| Neon `diagnoses` | every text and JSONB column, all rows | clean |

**Nothing was ever in git.** The leak was the local session transcript of the
assistant that built this: a redaction step failed and printed live values into
a plaintext log. So rotation *was* sufficient - no history rewrite, no
force-push, no permanently burned credential.

A useful property of that scan: `WEBHOOK_SECRET`, which had already been
rotated, came back clean everywhere. A scan that finds nothing everywhere is
indistinguishable from a scan that is silently broken - which is exactly the
failure that caused the original leak. Having one known-clean and several
known-dirty values in the same run is what made the result trustworthy.

**The rule this leaves behind:** never print a value read from `.env` or the
environment, not even to prove it is correct. Check it with a boolean, or
compare a SHA-256 prefix - a hash is one-way, so it can be shown, logged and
compared across runs without disclosing anything. Both original leaks were a
filter that silently failed to filter, followed by printing its output.

`XAI_API_KEY` was removed rather than rotated: it appeared in `.env` but is
referenced nowhere in the codebase.

## Production

Live at **<https://builddoctor.onrender.com>**.

| Piece | Where it runs | Why |
| ----- | ------------- | --- |
| App + MCP server | One Render **web service**, Free instance, Oregon | see below |
| Postgres + pgvector | **Neon**, `aws-us-west-2`, Postgres 18, pgvector 0.8.6 | Render's free database expires; Neon's does not |
| Dashboard | Render **static site**, Free | <https://builddoctor-dashboard.onrender.com> |

### Why Neon rather than Render Postgres

Render's own free Postgres **expires 30 days after creation**, with 14 days to
upgrade before deletion. For most projects that is an annoyance. For this one
it is fatal: Phase 6 gave BuildDoctor *memory*, and the entire point of the
pgvector similarity search is recalling failures from weeks ago. A database
that deletes itself every month is not memory. Neon's free tier does not
expire, so the diagnoses accumulate for as long as the project lives.

Two details about the connection string, both of which cause failures that do
not look like configuration problems:

- **It must say `postgresql+psycopg://`, not `postgresql://`.** SQLAlchemy
  chooses its driver from the URL scheme, and bare `postgresql` means psycopg2 -
  which this project does not install. The symptom is
  `ModuleNotFoundError: No module named 'psycopg2'` at import time, before a
  single line of application code runs. (This is not hypothetical; it is how
  the first production deploy failed.)
- **Use Neon's UNPOOLED url.** The variable Neon literally names `DATABASE_URL`
  is the *pooled* one, routed through PgBouncer in transaction mode. psycopg 3
  uses prepared statements by default, and the two combine into sporadic
  `prepared statement "_pg3_0" already exists` errors under concurrency - weeks
  later, not at startup. SQLAlchemy already runs its own pool with
  `pool_pre_ping=True`, so PgBouncer adds nothing here.

Neon refuses unencrypted connections outright - verified, not assumed, by
attempting one with `sslmode=disable` and getting
`connection is insecure (try using 'sslmode=require')`. Note that `pg_stat_ssl`
reports `ssl = false` on Neon anyway, because TLS terminates at their proxy;
that is an artefact of their architecture, not an unencrypted client.

### Why one service instead of three

`docker-compose.yml` runs app, mcp and postgres as separate containers, and the
obvious translation is three Render services. That is not purchasable on a free
plan, for two reasons taken from Render's documentation rather than memory:

1. Private services (`type: pserv`) have **no free instance type**.
2. *"Free web services can send private network requests, but they can't
   receive them."*

The second closes the door. A free web service cannot stand in for the private
one, because the app could not **reach** it - not a privacy downgrade, an
outright disconnection.

So the MCP server moves inside the app's container via
[`server_combined.py`](server_combined.py). It is still a separate ASGI app,
still spoken to over real HTTP with a real JSON-RPC handshake, still refusing to
retry a write whose outcome is unknown. Only the socket changed: loopback
instead of a private network. Because the difference is a URL rather than a code
path, one variable switches between them and the same image runs both ways:

```
compose   MCP_SERVER_URL=http://mcp:8001/mcp
Render    MCP_SERVER_URL=http://localhost:10000/internal/mcp
```

One service did not have to mean one *process*, and the first attempt ran both
uvicorns side by side in the same container. Measured under a real 512 MiB cap
before deploying:

| Arrangement | Memory | Result |
| ----------- | ------ | ------ |
| app + mcp, two processes | 549 MiB | **OOM-killed** |
| one process, mcp mounted in | 446 MiB | healthy |

The difference is a second Python interpreter importing torch and
sentence-transformers all over again. Render's free instance is 512 MiB, so the
second interpreter is the thing that had to go.

### The MCP endpoint is public but unusable

Mounting the MCP server on the public app means `https://<host>/internal/mcp`
resolves rather than 404s. It still cannot be used: `mcp_server.py`'s
DNS-rebinding guard compares the `Host` header against `MCP_ALLOWED_HOSTS`,
which in production names only `localhost:10000` and `127.0.0.1:10000`. A
request arriving through Render's router carries `Host: <name>.onrender.com`,
matches nothing, and is answered **421 Misdirected Request** before reaching a
tool. Verified in both directions before deploying.

That guard is not new and was not weakened for this - it is the same setting
that already had to name `mcp:8001` under compose. But it is worth naming the
one way this is weaker than a private service: a private service is unreachable
because the network has no route to it, whereas this is reachable and refuses. A
misconfigured guard fails open; a missing route cannot be misconfigured. So
`MCP_ALLOWED_HOSTS` is load-bearing in production in a way it was not before,
and widening it to include the public hostname would expose four
GitHub-writing tools to the internet with no authentication in front of them.

### Sleeping loses webhooks outright, and keeping warm is the fix

An earlier version of this section said a cold-start delivery shows as failed
in GitHub's UI "even though BuildDoctor handled it fine". **That was wrong**,
and the correction matters more than the original claim did.

Checked against the current docs rather than from memory:

| Fact | Source |
| ---- | ------ |
| Render "spins down a Free web service that goes 15 minutes without receiving any inbound traffic" | [Render](https://render.com/docs/free) |
| Spinning back up "takes about one minute" | [Render](https://render.com/docs/free) |
| GitHub records a failure if the server "takes longer than 10 seconds to respond" | [GitHub](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries) |
| "GitHub does not automatically redeliver failed webhook deliveries" | [GitHub](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries) |

One minute is not ten seconds. So a build that fails while the service is
asleep is not *delayed* and it is not *mishandled* - it is **never received**.
GitHub times out, never tries again, and BuildDoctor has no idea the run
existed. Nothing in the application can detect or log this, because the request
never arrived. Failed deliveries can be replayed by hand from the repository's
webhook settings for **3 days**, and that is the only recovery.

[`.github/workflows/keep-warm.yml`](.github/workflows/keep-warm.yml) pings
`/health` every 10 minutes, which resets the 15-minute idle timer so deliveries
land on a warm process. It checks the response *body* rather than the status
code, because Render "displays a loading page to connecting browsers while a
service is spinning up" and that page is itself an HTTP 200 - asserting 200
alone would report a still-booting service as healthy.

**Why a scheduled Action rather than the obvious alternative.** The other real
fix is Render's paid always-on tier, where nothing spins down and the workflow
would be unnecessary. That is a recurring monthly cost, so it is a decision for
whoever owns the account rather than a default. The ping costs nothing.

**Why this repository is public.** Actions minutes are
[free and unlimited for public repositories](https://docs.github.com/en/billing/concepts/product-billing/github-actions),
but a private repository on the Free plan gets 2,000 minutes a month, and
GitHub "rounds the minutes and partial minutes each job uses up to the nearest
whole minute". A 15-second ping therefore bills a full minute, and one every 10
minutes around the clock is 4,320 billed minutes a month - the quota is gone in
about two weeks. Slowing the ping down does not rescue it either: staying under
2,000 means one ping every ~22 minutes, which is longer than the 15-minute
sleep window and so keeps nothing warm. **A 24/7 keep-warm inside a private
repository's free quota is arithmetically impossible.** Making the repository
public is what makes the ping free, and Phase 10 verified that no secret has
ever existed in this repository's git history before doing it.

**It narrows the window; it does not close it.** GitHub's own docs warn the
`schedule` event "can be delayed during periods of high loads" and that "some
queued jobs may be dropped". A late or skipped ping can still let the idle
timer reach 15 minutes. Scheduled workflows also only run on the default
branch, so this file does nothing until it is on `master`.

## Running with Docker (the normal way)

```powershell
docker compose up --build
```

That starts four containers:

| Service | Port | Role |
| ------- | ---- | ---- |
| `postgres` | 5433 (host) | stores diagnoses |
| `mcp` | 8001 | the four GitHub actions, as MCP tools |
| `app` | 8000 | webhook receiver, evidence gathering, the lane graph, the dashboard API |
| `frontend` | 5173 | the dashboard itself, on the Vite **dev** server |

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

### The dashboard, outside Docker

Same idea, one directory down. Node 20 or newer:

```powershell
cd frontend
npm install          # first time only
npm run dev
```

That serves <http://localhost:5173> and expects the API on
<http://localhost:8000>, which is the default in `frontend/src/api.ts` - so
whether the API is a container or a local `uvicorn` makes no difference to it.

`npm run typecheck` checks the types without building anything, which is the
fastest way to find out that a field was renamed on the Python side.

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
provider restricts decoding to tokens the schema allows - enforced *while the
tokens are generated*, not checked afterwards.

**Since Phase 8.5 the model does not name a lane at all.** It answers the four
triage steps as booleans with one-line reasons, and `derive_category()` applies
the first-match rule in code. The eval had caught the model skipping STEP 1
(security) and answering from STEP 2 on a permissions failure - the steps were
ordered in the prose and nothing forced them to be *evaluated* in order.

Two things make the schema stronger than the instruction was. Generation is
left to right and constrained decoding emits keys in the declared order, so the
model has to commit to `step_1_security_triggered` before `step_2` exists as a
token to write; the ordering becomes a property of how the answer is produced
rather than a request. And there is no single field where a wrong lane can be
written down.

It costs roughly 120-200 extra output tokens per diagnosis. `parse_triage`
returns `None` - forcing the retry below - if any of the four booleans is
missing or is not a boolean; a missing `step_1` is never read as false, because
accidentally answering the security question "no" is the one direction the
change exists to prevent.

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
