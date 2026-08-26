# Evaluation findings

Two rounds so far. **Phase 8.5 is current**; the Phase 8 section below it
is kept verbatim, because the value of a golden set is that old numbers
stay readable next to new ones.

`report.md` holds the machine-generated numbers and is overwritten on
every run. This file is written by hand and is not.

---

# Phase 8.5 - fixing the three things Phase 8 found

Phase 8 measured and deliberately fixed nothing. Phase 8.5 fixed exactly
the three defects it flagged, and re-ran the whole golden set.

## The headline: the aggregate did not move, and that is the story

| | Phase 8 | Phase 8.5 |
| --- | --- | --- |
| **Accuracy** | 22/24 (91.7%) | **22/24 (91.7%)** |
| Excluding ambiguous | 20/21 (95.2%) | 20/21 (95.2%) |
| `informational` | 11/12 | 11/12 |
| `safe_auto_fix` | **3/3** | **2/3** |
| `needs_review` | 8/9 | **9/9** |
| Middle zone | 5/6 | **6/6** |

**Two cases were fixed. Two regressed. The total is identical.**

An accuracy of 91.7% before and 91.7% after would, on its own, have said
"this change did nothing". It did four things. This is the entire reason
the runner now renders a per-case diff against a frozen baseline rather
than reporting a percentage: *an aggregate cannot show a trade.*

| | Case | Was | Now | Expected |
| --- | ---- | --- | --- | -------- |
| **FIXED** | `syn-08` | `informational` | **`needs_review`** | `needs_review` |
| **FIXED** | `mid-06` | `needs_review` | **`informational`** | `informational` |
| **REGRESSED** | `hist-02` | `informational` | **`needs_review`** | `informational` |
| **REGRESSED** | `mid-05` | `safe_auto_fix` | **`informational`** | `safe_auto_fix` |

## Fix 1 - STEP 2 is no longer Python-shaped. WORKED.

Phase 8 found `package.json` being classified as source code, because
STEP 2 listed only Python and generic examples and leaned on "or similar"
to carry everything else.

The list now names `package.json`, `Cargo.toml`, `go.mod`, `pom.xml`,
`build.gradle` and `composer.json` alongside what was there, grouped by
role - CI config, build/image, dependency manifests, lockfiles. More
importantly it now states the **rule** the list is an instance of: a file
whose job is to declare *what to install, which versions, or how to build*.
Unnamed files resolve by that principle rather than by luck.

It also rebuts the exact wrong reasoning the model produced: a dependency
manifest is machinery **even though it sits at the repository root and
developers hand-edit it**.

`mid-06` now returns `informational`, and the model's own words show the
new rule doing the work:

> STEP 2 (build or dependency machinery): The failure is caused by a
> missing dependency entry in package.json, **which declares what to
> install**.

## Fix 2 - the schema is now the gate. WORKED, with a side effect.

Phase 8 found `syn-08` - a permissions failure - classified
`informational` with the reason *"Step 2: ... which is build/CI
machinery"*, skipping STEP 1, which says security and **stop**. The steps
were ordered in the prose and nothing forced the model to evaluate them
in that order.

The model no longer names a lane. It answers four booleans with reasons,
emitted in step order, and `derive_category()` applies the first-match
rule in code. Two things make that stronger than an instruction:

1. Generation is left to right and constrained decoding emits keys in the
   declared order, so the model must commit to `step_1_security_triggered`
   before `step_2` exists as a token to write. The ordering stops being a
   request and becomes a property of how the answer is produced.
2. There is no single field where a wrong lane can be written down.

`syn-08` now returns `needs_review`:

> STEP 1 (secrets, credentials, permissions or a security scan): The error
> message indicates a permissions issue (Resource not accessible by
> integration).

**The cost, measured rather than estimated:** output tokens per call rose
from 154-276 to roughly 300-450. Still far inside the 1000 budget, but
every diagnosis is now meaningfully more expensive, and on a rate-limited
tier that is felt.

`parse_triage` returns `None` - forcing a retry - if any of the four
booleans is missing or is not a bool. A missing `step_1` is **not** read
as false. Answering the security question "no" by accident is the one
direction this change exists to prevent.

## Fix 3 - version lists no longer crowd out the error. WORKED.

For row 9, what the embedder actually reads:

| | Before | After |
| --- | --- | --- |
| Raw excerpt | 2809 chars | 2809 chars |
| After `clean()` | 2000 (truncated) | **1440** |
| Tokens | 1354 | **524** |
| Discarded by the 256-token window | **1098 (81%)** | 268 (51%) |
| Is `No matching distribution found` inside the window? | **NO** | **YES** |

Before, the window ended mid-list at `... 2.6.3, 2` and the actual
conclusion of the failure never reached the model at all.

`mid-01` - the same bad-pin failure with a different package name - moved
from **0.7959 to 0.9600** and crossed the threshold. Its nearest
neighbour moved from row 2, an unrelated "tests/ not found" failure, to
**row 5**, which is an actual pytest version pin. It is now matching the
right cluster for the right reason.

**The change was surgical, which was not guaranteed.** All 24 scores were
re-checked because changing `clean()` can move any similarity:

- 20 of 24 moved by **exactly 0.0000**
- 2 moved by 0.0002
- only `mid-01` (+0.1641) and `mid-05` (+0.0752) moved meaningfully
- **no case lost a match** - no true positive became a false negative
- `mid-06`'s true negative held at **exactly 0.7429**, unchanged

That last one was worth checking explicitly. `mid-06` distinguishes Node's
`Cannot find module` from Python's `No module named` - nearly the same
English sentence - and it relies on word content, so the cleaning change
could plausibly have disturbed it. It did not, because neither that text
nor row 2's contains a version list, so neither vector changed.

**The middle zone is now 6 for 6.** Every prediction written before the
Phase 8 run has now been confirmed.

## Regression 1: `hist-02` - the ambiguity was real

`hist-02` is pytest failing with `file or directory not found: tests/`,
where the diff only edited `ci.yml`. Expected `informational`, now
returns `needs_review`:

> STEP 4 (source or test code): The failure is caused by a missing tests/
> directory, which is part of the source/test code.

**This is a case I flagged `ambiguous: true` when I wrote it**, and the
rationale stored in the golden set says, in advance:

> AMBIGUOUS: a reasonable engineer could say the real fix is to create
> `tests/`, which would be source code and STEP 4.

So the model has switched to the *other* defensible reading. Enforcing
step order removed the holistic weighing that previously landed it on
STEP 2. It is a genuine regression against the golden set and it is
counted as one - but it is a disagreement on a case built to be
disagreed about, not a malfunction.

## Regression 2: `mid-05` - STEP 2 now pre-empts STEP 3, and this one matters

`mid-05` is a DNS failure reaching pypi.org - a textbook flaky failure -
in a run whose diff touched `.github/workflows/ci.yml`. Expected
`safe_auto_fix`. Now returns `informational`:

> STEP 2 (build or dependency machinery): The failure occurs during the
> pip install step in the CI workflow file .github/workflows/ci.yml,
> which is build machinery.

**STEP 2 is evaluated before STEP 3.** Once ordering is genuinely
enforced, any transient failure that happens in a run touching a
machinery file is caught by the machinery question before the flaky
question is ever asked.

The evidence that this is the mechanism and not a coincidence:

| Amber case | Diff touches | Result |
| ---------- | ------------ | ------ |
| `syn-02` | `README.md` | still correct |
| `syn-03` | `src/api.py` | still correct |
| `mid-05` | **`.github/workflows/ci.yml`** | **regressed** |

`mid-05` is the only `safe_auto_fix` case whose diff touches a machinery
file, and it is the only one that broke.

**This is a pre-existing ordering defect that Fix 2 exposed rather than
created.** The prompt has always listed STEP 2 before STEP 3. Before,
the model weighed the whole picture and often landed correctly anyway;
that leniency was hiding the bug. The gate is working as designed - it is
faithfully executing an order that is wrong.

**Why it matters more than the raw score suggests.** CI steps are where
things run, so *most* transient failures occur during one. And `ci.yml`
is a file people edit often. The combination is not exotic. This
undermines the amber lane specifically, which is the lane with **zero**
real examples in the database and therefore the least observed in
production.

### What a fix would involve - deliberately NOT done here

Phase 8's rule applies to Phase 8.5 as well: measure, then change one
thing. Two candidates, both plausible, neither tested:

1. **Reorder**: ask STEP 3 (flaky) before STEP 2 (machinery). "Is this
   transient?" is arguably a question about the *nature* of the failure,
   while "which file?" is a question about the fix, and the former should
   probably come first.
2. **Exclude**: keep the order and add to STEP 2 that it applies to what
   the file *declares*, not to failures that merely *occur during* a step
   the file defines.

Option 2 is narrower and does not disturb the three lanes that currently
pass. Option 1 is more principled and riskier. Choosing between them
needs more than one amber case with a machinery diff in the golden set -
the set currently has exactly one, which is too thin to tune against.

**Adding those cases is the next step, before either fix.**

---

# Phase 8 findings

*(Kept verbatim. These were the numbers before the three fixes above.)*

**Nothing described here was fixed.** Phase 8 measures; a fix without a
before-and-after number is a guess with extra steps. Each finding below
ends with what a fix would have to be, so Phase 9 or later has somewhere
to start.

---

## The headline

| | |
| --- | --- |
| Classification accuracy | **22/24 (91.7%)** |
| Excluding the 3 cases I flagged ambiguous | 20/21 (95.2%) |
| Live cross-check | **4/4 agree** with what the live pipeline recorded |
| `safe_auto_fix` (amber) | 3/3 — the lane with **zero** real examples in the database |

Two cases came back wrong. Neither is a crash or a malformed response;
both are the classifier applying a different step of its own rulebook than
I did, and both are traceable to specific wording in the prompt.

---

## The answer to Phase 6's open question

Phase 6 left this behind: every similarity the system had ever seen was
either about 1.00 (the same fixture failing twice) or at most 0.83 (two
unrelated failures). **The band between them was empty**, so the 0.90
threshold had never actually been asked to make a hard call.

Six cases were built to land in it. Three did:

| Case | Score | Should it match? | Did it? |
| ---- | ----- | ---------------- | ------- |
| `mid-04` assertion failure, different file and numbers | **0.9254** | yes | **yes** |
| `mid-02` apt missing package, different package name | **0.9431** | yes | **yes** |
| `mid-03` ModuleNotFoundError, different module | **0.9763** | yes | **yes** |

And the rejections held:

| Case | Score | Should it match? | Did it? |
| ---- | ----- | ---------------- | ------- |
| `mid-05` pip DNS failure vs pip bad-version rows | 0.5687 | **no** | no |
| `mid-06` Node `Cannot find module` vs Python `No module named` | 0.7429 | **no** | no |
| `mid-01` same bad-pin failure, different package | 0.7959 | yes | **no — see Finding 1** |

**The threshold is vindicated, with one asterisk.**

- Highest score that should NOT have matched: **0.8333**
- Lowest score that SHOULD have matched: **0.9254**
- The gap between them: **0.0921**, and 0.90 sits inside it.

The gap is much narrower than Phase 6's (0.811 → 0.994), which is exactly
what you would expect once genuinely similar-but-different cases exist.
But it is still a real gap, and the threshold is still inside it rather
than on an edge. `mid-06` is the reassuring one: `Cannot find module` and
`No module named` are nearly the same English sentence, and it was still
correctly rejected at 0.74.

The asterisk is `mid-01`, which is a false negative — and it turned out
not to be the threshold's fault at all.

---

## Finding 1: memory is partly matching on a wall of version numbers

**Severity: high.** This is the real discovery of Phase 8.

`mid-01` is `pip install requests==999.999.999` failing. Rows 9–12 are
`pip install pytest==99X.X.X` failing. Same error, same file, same fix —
only the package name differs. I predicted 0.90–0.97.

It scored **0.7959, and its nearest neighbour was row 2** — an unrelated
"tests/ directory not found" failure.

### What is actually happening

`eval/probe_embedding_window.py` shows what the embedder reads:

```
ROW 9   raw 2809 chars -> 1354 tokens -> the model reads 256, DISCARDS 1098 (81%)
        the discarded part begins mid-version-list: "2.6.4, 2.7.0, 2.7.1, ..."
mid-01  raw 1505 chars ->  550 tokens -> the model reads 256, discards 294
```

When pip rejects a version it prints **every version it does know about**.
For rows 9–12 that list is 1368 characters. `all-MiniLM-L6-v2` reads at
most 256 word-pieces, so ~81% of those rows is thrown away, and a large
part of what survives is version numbers rather than the failure.

`eval/probe_version_list.py` confirms the list is doing the work:

```
sim(mid-01 as written      , row 9) = 0.6191
sim(mid-01 + row 9's list  , row 9) = 0.9138   <-- +0.29, crosses the threshold

control: row 2's UNRELATED failure with row 9's version list pasted in
sim(that, row 9) = 0.5895   (row 2 alone scores 0.3229)   <-- +0.27 from noise
```

Pasting an irrelevant blob of version numbers into an unrelated failure
moves it **+0.27 toward** row 9. The list is not a tiebreaker; it is a
large fraction of the signal.

### Why this matters beyond one case

Two failures that are genuinely the same can be pushed apart because one
of them happened to print a longer list. Two failures that are unrelated
can be pushed together because both printed one. Similarity is being
computed over a mixture of *the error* and *how much incidental text the
tool emitted*, and there is no control over the ratio.

It also explains a number that looked reassuring in Phase 6: rows 9–12
score 0.9938 against each other. Some of that agreement is the error.
Some of it is that they share a nearly identical 1368-character list.

### What a fix would involve — NOT done here

- `embeddings.clean()` currently strips only BuildDoctor's own
  `--- log lines N-M of T ---` header. It could also strip pip's
  `(from versions: ...)` list, the Node-deprecation banner, and the
  `git config` boilerplate that ends every excerpt in this repository.
- Or embed a smaller, error-centred window rather than the first 2000
  characters.
- Or use a model with a longer context than 256 word-pieces.

All three change every stored embedding, so all three require a full
re-embed of the table and a re-measure against this same golden set.
That is the point of having the golden set. **Not done in this phase.**

---

## Finding 2: STEP 1 is being skipped for permission failures

**Severity: medium, and it is in the unsafe direction.**

`syn-08` is a workflow failing with `Resource not accessible by
integration` after `permissions:` was deleted from `ci.yml`.

| | |
| --- | --- |
| Expected | `needs_review` |
| Returned | `informational` |
| The model's reason | *"Step 2: the failure is due to a change in the CI workflow file (.github/workflows/ci.yml), which is build/CI machinery."* |

The prompt's STEP 1 says:

> Does the failure involve secrets, credentials, tokens, API keys,
> **permissions**, or a security or vulnerability scan?
> YES -> "needs_review". Stop.

STEP 1 runs before STEP 2 and says *stop*. The model reached STEP 2
anyway. Note it was not confused about the facts — it correctly named the
file and the change — it just applied the wrong step.

I did flag this case ambiguous when I wrote it, because a person could
reasonably call a `ci.yml` edit machinery. But the prompt itself is not
ambiguous about the order, and the direction of the error matters: a
permissions failure was routed to the lane that only leaves a comment,
rather than the one that flags a human.

**One case is not a pattern.** There is exactly one permissions case in
this set. Establishing whether STEP 1 is reliably reached would need
several more, which is a golden-set change, not a prompt change.

**Not fixed.**

---

## Finding 3: STEP 2's file list is Python-shaped

**Severity: low. Wrong in the safe direction.**

`mid-06` is Node failing with `Cannot find module 'express'` after the
dependency was deleted from `package.json`.

| | |
| --- | --- |
| Expected | `informational` |
| Returned | `needs_review` |
| The model's reason | *"Step 4: the failure requires editing source code (package.json) to restore a missing dependency."* |

STEP 2 lists the files it considers machinery:

> anything under `.github/workflows/`, a Dockerfile, `requirements.txt`,
> `setup.py`, `pyproject.toml`, a Makefile, a lockfile, **or similar**

Every named example is Python or generic. `package.json` is not named, and
"or similar" did not carry it. The model classified a dependency manifest
as source code.

This errs toward escalation, which is the cheap direction — a needless
`needs-review` label costs a glance. Worth knowing before this is pointed
at anything that is not a Python repository.

**Not fixed.**

---

## Finding 4: row 5's stored lane is stale, and the eval proves it

Rows 5 and 9–12 are the *same failure* — a pytest version pinned in
`ci.yml` that does not exist. Row 5 is stored as `needs_review`. Rows
9–12 are stored as `informational`.

Replaying row 5 through today's classifier returns **`informational`**,
matching rows 9–12 and matching what I derived by hand from STEP 2.

This is not a defect. It is the prompt having been sharpened between
Phase 4 and now — STEP 2 gained the sentence *"This holds however
obviously wrong that file is - a pinned version that does not exist..."* —
and it confirms something the golden set was built around:

> **The lane stored in the database is not ground truth.** It is what the
> system output at the time, under whatever prompt existed that day.
> Scoring a classifier against its own past output measures agreement,
> not correctness.

Every `expected_lane` in `golden_set.json` is derived by hand from
`diagnose.py`'s STEP 1–5 and carries a `lane_rationale` naming the step.
The stored lane is kept in a separate `recorded_lane` field so the two can
be compared without being confused.

---

## Finding 5: the free tier, not the model, is the eval's bottleneck

The first run of `run_eval.py` used three concurrent requests and no
backoff. **Ten of 24 cases returned HTTP 429** and were dropped, and the
script cheerfully printed an accuracy computed over the surviving 14.

That is the most dangerous shape a test can have: a confident number over
a silently reduced denominator.

The harness now paces to one request every 24 seconds and waits out the
delay the provider names in its 429. A full run takes about ten minutes,
which is the floor: 8000 tokens/minute against 1900–3350 tokens per case.

`diagnose.py` was **not** touched for this. Being rate limited is a fact
about running 24 cases in a burst against a free tier — a property of the
harness, so the retry lives in the harness.

---

## What has NOT been measured

Stated plainly, so none of the above is read as more than it is.

1. **One sample per case.** Temperature is 0.2, not 0. Run-to-run variance
   is unmeasured. `run_eval.py --repeat 3` would measure it.
2. **24 cases.** One case is about four percentage points. A one-case
   change is noise.
3. **Historical diffs are reconstructed.** The pipeline stores
   `diff_summary` — which files changed, how many lines — but never the
   diff text. Those cases carry the real log excerpt byte for byte and a
   diff rebuilt from the summary.
4. **Similarity scores are relative to thirteen rows.** They describe this
   database, not the world.
5. **I wrote both the cases and the answers.** The lanes trace to a
   written rule in `diagnose.py` rather than to taste, but a second person
   would disagree somewhere. The three cases marked `ambiguous` are where
   I would expect it first.
6. **No genuinely live run was triggered.** The four `live-crosscheck`
   cases are verbatim replays compared against what the live pipeline
   stored, which tests that the eval path and the live path agree. It does
   not re-test webhook delivery, evidence gathering, or posting. See the
   README for how to trigger a real one.
