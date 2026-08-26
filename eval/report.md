# BuildDoctor evaluation report

Generated 2026-08-26 11:46 UTC by `eval/run_eval.py` against `eval/golden_set.json`.
24 cases, 556 seconds, model `openai/gpt-oss-20b` at temperature 0.2.

This run **measured** the system. Nothing was tuned in response to it: no prompt, no threshold, no guard was changed. A fix without a before-and-after number is a guess with extra steps.

## Headline

| | |
| --- | --- |
| **Classification accuracy** | **22/24 (91.7%)** |
| Excluding cases I flagged ambiguous | 20/21 (95.2%) |

### By expected lane

| Lane | Correct | Accuracy |
| ---- | ------- | -------- |
| `informational` | 11/12 | 92% |
| `safe_auto_fix` | 3/3 | 100% |
| `needs_review` | 8/9 | 89% |

### By group

| Group | Correct | Accuracy | What it tests |
| ----- | ------- | -------- | ------------- |
| historical | 6/6 | 100% | Real failures from rows 1-13, verbatim excerpts |
| live-crosscheck | 4/4 | 100% | Replays of real runs, vs what the live pipeline stored |
| middle-zone | 5/6 | 83% | Similar-but-different pairs, to probe the threshold |
| synthetic | 7/8 | 88% | Written to cover each lane, including 2 ambiguous |

### Confusion

| Expected | What came back |
| -------- | -------------- |
| `needs_review` | `needs_review` x8, `informational` x1 |
| `informational` | `informational` x11, `needs_review` x1 |
| `safe_auto_fix` | `safe_auto_fix` x3 |

## The middle zone: 0.83 to 0.99

This is the open question Phase 6 left behind. Every similarity ever seen by this system had been either about 1.00 (the same fixture failing twice) or at most 0.83 (two unrelated failures). The band between them was empty, so the 0.90 threshold had never actually been asked to make a hard call.

Six cases were built to land in it: pairs that are deliberately similar-but-different. The `predicted` column is my guess, written **before** the run; `verdict` is what happened.

| Case | Top similarity | Nearest row | Matched? | I predicted | Verdict |
| ---- | -------------- | ----------- | -------- | ----------- | ------- |
| `mid-01` | **0.7959** | 2 | no | should match | MISS |
| `mid-02` | **0.9431** | 13 | **yes** | should match | AS PREDICTED |
| `mid-03` | **0.9763** | 4 | **yes** | should match | AS PREDICTED |
| `mid-04` | **0.9254** | 3 | **yes** | should match | AS PREDICTED |
| `mid-05` | **0.5687** | 9 | no | should not match | AS PREDICTED |
| `mid-06` | **0.7429** | 2 | no | should not match | AS PREDICTED |

### Case by case

#### `mid-01` - Same missing-version failure as rows 9-12, different package (requests).

- **Top similarity: 0.7959** against row 2; memory rejected it.
- Next closest: row 13 at 0.7681, row 9 at 0.6191.
- I predicted **should match** against rows [9, 10, 11, 12].
- Verdict: **MISS** - closest was 0.7959, below the 0.90 threshold, but a human would have wanted this hint.

  > Identical failure mode, identical fix shape, identical file. Only the package name differs. A past diagnosis of the pytest version would genuinely help here, so a hint would be welcome. GUESS: I expect roughly 0.90-0.97 - high, but below the ~1.00 the true pairs have scored so far, because the package name is a real token difference.

#### `mid-02` - Same apt failure as row 13, different package name.

- **Top similarity: 0.9431** against row 13; memory RETURNED it.
- Next closest: row 2 at 0.8155, row 1 at 0.5387.
- I predicted **should match** against rows [13].
- Verdict: **AS PREDICTED** - matched at 0.9431, and a match was wanted.

  > The two logs differ in one token: the package name. Row 13's diagnosis transfers almost word for word. GUESS: very high, possibly 0.95+, which would put it above the threshold. If this one lands BELOW 0.90 the threshold is too strict.

#### `mid-03` - Same ModuleNotFoundError shape as rows 4/6/7/8, different module and file.

- **Top similarity: 0.9763** against row 4; memory RETURNED it.
- Next closest: row 6 at 0.9760, row 7 at 0.9753.
- I predicted **should match** against rows [4, 6, 7, 8].
- Verdict: **AS PREDICTED** - matched at 0.9763, and a match was wanted.

  > Same error class, same fix shape - create the package or fix the import. Two tokens differ: the module name and the test file name. GUESS: high, 0.90-0.97. A hint here would help.

#### `mid-04` - Same assertion-failure shape as rows 1/3, different numbers and file.

- **Top similarity: 0.9254** against row 3; memory RETURNED it.
- Next closest: row 1 at 0.9254, row 6 at 0.7696.
- I predicted **should match** against rows [1, 3].
- Verdict: **AS PREDICTED** - matched at 0.9254, and a match was wanted.

  > Structurally identical to rows 1 and 3 - a pytest assertion failure on arithmetic - but a different file, a different operator and different numbers. This is the case memory.py's own comment flags as untested: the true pairs so far are the SAME fixture twice, so 0.994 is the floor for identical, not for merely similar. GUESS: 0.88-0.95, and this is the one I would least like to bet on.

#### `mid-05` - pip fails on the same command as rows 9-12, but from a network timeout rather than a bad version. Opposite cause, opposite lane.

- **Top similarity: 0.5687** against row 9; memory rejected it.
- Next closest: row 5 at 0.5687, row 7 at 0.5682.
- I predicted **should not match** against rows [9, 10, 11, 12].
- Verdict: **AS PREDICTED** - closest was 0.5687, correctly rejected.

  > THE DANGEROUS ONE. Same job, same step, same pip command, and a lot of shared vocabulary - ERROR, pip, pytest, Could not install. But the cause is the opposite kind of thing and so is the correct lane: this is amber, those rows are teal. A hint from row 10 would push the model towards 'a version that does not exist', which is not what happened. GUESS: 0.80-0.92, and if it lands above 0.90 the threshold is admitting a match that a human would not want.

#### `mid-06` - Node cannot find a module. Same words as the Python ModuleNotFoundError rows, completely different ecosystem and fix.

- **Top similarity: 0.7429** against row 2; memory rejected it.
- Next closest: row 13 at 0.6926, row 1 at 0.5615.
- I predicted **should not match** against rows [4, 6, 7, 8].
- Verdict: **AS PREDICTED** - closest was 0.7429, correctly rejected.

  > 'Cannot find module' versus 'No module named' is nearly the same English sentence, which is exactly the kind of surface similarity an embedding can be fooled by. But the fix is different (npm install versus create a Python package) and the lane is different. GUESS: 0.70-0.88, so probably rejected - but if it lands above 0.90 that is a false match worth knowing about.

### Where every case landed

Including the ones with no memory expectation, because the shape of the whole distribution is what says whether the threshold sits in a gap or in the middle of a crowd.

| Case | Closest | 2nd | 3rd | Nearest row | Above threshold? |
| ---- | ------- | --- | --- | ----------- | ---------------- |
| `hist-01` | 1.0000 | 0.8109 | 0.8103 | 3 | yes |
| `hist-04` | 1.0000 | 0.9938 | 0.9938 | 9 | yes |
| `live-02` | 1.0000 | 0.9938 | 0.9938 | 5 | yes |
| `hist-05` | 0.9999 | 0.9938 | 0.9938 | 12 | yes |
| `live-03` | 0.9999 | 0.9999 | 0.9935 | 10 | yes |
| `live-01` | 0.9997 | 0.9993 | 0.8095 | 6 | yes |
| `hist-03` | 0.9995 | 0.9993 | 0.9993 | 6 | yes |
| `mid-03` | 0.9763 | 0.9760 | 0.9753 | 4 | yes |
| `mid-02` | 0.9431 | 0.8155 | 0.5387 | 13 | yes |
| `mid-04` | 0.9254 | 0.9254 | 0.7696 | 3 | yes |
| `hist-02` | 0.8333 | 0.6850 | 0.6850 | 13 | no |
| `hist-06` | 0.8333 | 0.5193 | 0.5193 | 2 | no |
| `live-04` | 0.8333 | 0.5193 | 0.5193 | 2 | no |
| `syn-03` | 0.8060 | 0.6791 | 0.6640 | 2 | no |
| `syn-01` | 0.8026 | 0.7218 | 0.5622 | 2 | no |
| `mid-01` | 0.7959 | 0.7681 | 0.6191 | 2 | no |
| `syn-04` | 0.7456 | 0.6912 | 0.5446 | 2 | no |
| `mid-06` | 0.7429 | 0.6926 | 0.5615 | 2 | no |
| `syn-05` | 0.7400 | 0.7400 | 0.6986 | 3 | no |
| `syn-08` | 0.6832 | 0.5986 | 0.4889 | 2 | no |
| `syn-06` | 0.6099 | 0.5917 | 0.5014 | 2 | no |
| `mid-05` | 0.5687 | 0.5687 | 0.5682 | 9 | no |
| `syn-07` | 0.5578 | 0.5578 | 0.5191 | 3 | no |
| `syn-02` | 0.5013 | 0.5013 | 0.5009 | 8 | no |

## Every wrong case (2)

### `syn-08` (synthetic) - **I flagged this one ambiguous**

The workflow token lacks permission to comment on the PR.

| | |
| --- | --- |
| Expected | `needs_review` |
| Actually returned | `informational` |

**The model's own reason:**

> Step 2: the failure is due to a change in the CI workflow file (.github/workflows/ci.yml), which is build/CI machinery.

**Its diagnosis:**

> The workflow removed the permissions block that granted the actions/github-script@v7 write access to pull‑request comments; without pull‑requests: write the script cannot post a comment and fails with a 403. Fix: add a permissions section granting pull‑requests: write (and any other needed scopes).

**Why I expected something else:**

> GENUINELY AMBIGUOUS. STEP 1 names permissions and stops there, which gives needs_review. But the one file to edit is ci.yml, which is exactly what STEP 2 calls machinery - and STEP 2 would give informational. The prompt resolves it by order: STEP 1 runs first and says stop. I expect needs_review, and informational is the reasonable disagreement.

### `mid-06` (middle-zone)

Node cannot find a module. Same words as the Python ModuleNotFoundError rows, completely different ecosystem and fix.

| | |
| --- | --- |
| Expected | `informational` |
| Actually returned | `needs_review` |

**The model's own reason:**

> Step 4: the failure requires editing source code (package.json) to restore a missing dependency.

**Its diagnosis:**

> The test fails because the package.json no longer lists the express dependency, so running the built server cannot resolve the 'express' module. The diff removed the "express" entry from dependencies, causing the MODULE_NOT_FOUND error. Fix: Add "express": "^4.19.2" back to the dependencies in package.json.

**Why I expected something else:**

> STEP 2. The one file to edit is package.json - the dependency was removed from it. Dependency machinery, not program behaviour.

## Live cross-check

Four verbatim replays of runs the live pipeline actually processed. The question is narrow: does calling the classifier the way this harness calls it produce what the live pipeline produced when it called it for real?

| Case | Source row | Live recorded | Eval returned | Agree? |
| ---- | ---------- | ------------- | ------------- | ------ |
| `live-01` | 7 | `needs_review` | `needs_review` | yes |
| `live-02` | 9 | `informational` | `informational` | yes |
| `live-03` | 12 | `informational` | `informational` | yes |
| `live-04` | 13 | `informational` | `informational` | yes |

**4 of 4 agree.**

## Where the system now disagrees with its own history

The lane stored in the database is not ground truth - it is what the system output at the time, sometimes under an earlier prompt. These are the historical cases where today's answer differs from what was stored.

| Case | Row | Stored then | Returns now | My expected |
| ---- | --- | ----------- | ----------- | ----------- |
| `hist-04` | 5 | `needs_review` | **`informational`** | `informational` |
| `hist-05` | 10 | `informational` | `informational` | `informational` |
| `hist-06` | 13 | `informational` | `informational` | `informational` |

1 of 3 historical cases with a stored lane now return something different.

## How this was run

- `graph.classify(state)` - the **real** LangGraph node, not a copy. It calls `diagnose.diagnose_failure` with the real prompt and applies the real rerun guard.
- `memory.search_past_failures(...)` - the **real** lookup, threshold and all. `db.nearest_by_embedding` records the raw scores that the gated function discards on a rejection.
- Posting, labelling and re-running live in three sibling nodes that only run when the compiled graph routes to them. Calling the node directly means routing never happens, so that code is never reached.
- On top of that, these write paths were replaced with functions that raise before the run started, and none of them fired:

  - `db.save_diagnosis`
  - `db.set_embedding`
  - `db.init_db`
  - `mcp_client.post_diagnosis`
  - `mcp_client.rerun_failed_jobs`
  - `mcp_client.add_labels`
  - `mcp_client._call`
  - `mcp_client.post_pull_request_comment`
  - `mcp_client.post_commit_comment`

- Replayed rows pass `exclude_run_id`, so a real excerpt cannot match the row it wrote. That is the same exclusion the live pipeline uses.

## Caveats, stated rather than buried

1. **The diffs on historical cases are reconstructed.** The pipeline stores `diff_summary` - which files changed and how many lines - but never the diff text. Those cases have the real log excerpt byte for byte and a diff rebuilt from the summary. Faithful in shape and in which file changed; not identical to what the model originally saw.
2. **One sample per case.** Temperature is 0.2, not 0, so these numbers carry unmeasured run-to-run variance. `--repeat N` runs each case N times if you want to measure it.
3. **Twenty-four cases is a small set.** One case is roughly four percentage points. Treat a change of one or two cases as noise.
4. **The middle-zone cases are compared against thirteen rows.** A similarity score is a statement about this database, not about the world. The same case against a thousand rows could find a closer neighbour and behave differently.
5. **I wrote both the cases and the expected answers.** The lanes are derived from `diagnose.py`'s own STEP 1-5, so they are at least traceable to a written rule rather than to taste - but a second person would disagree somewhere, and the three cases marked ambiguous are where I would expect it first.

