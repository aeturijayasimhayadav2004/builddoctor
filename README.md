# BuildDoctor

An agent that watches a GitHub repo and reacts when a CI build fails.

**Current stage: Phase 2.** When a workflow run fails, BuildDoctor fetches the
logs and the triggering diff, asks a model what went wrong, and posts the
answer back to GitHub as a comment.

## What it does now

1. GitHub sends a `workflow_run` webhook when a run finishes.
2. The signature on that request is verified before anything else happens.
3. If the run failed, BuildDoctor (in the background, so GitHub gets a fast
   reply):
   - lists the jobs that failed and downloads their logs
   - cuts each log down to the lines around its `##[error]` markers
   - fetches the change that triggered the run (PR diff, or commit vs parent)
   - asks an LLM to connect the symptom to the cause
   - posts the diagnosis as a PR comment or a commit comment
   - appends the whole thing to `diagnoses.jsonl`

| Route | Method | Purpose |
| ----- | ------ | ------- |
| `/health` | GET | Returns `{"status": "ok"}`. Confirms the server is alive. |
| `/webhook` | POST | Receives GitHub deliveries. Requires a valid signature. |

## Files

| File | Role |
| ---- | ---- |
| `main.py` | Web routes, signature check, and the failure pipeline |
| `github_client.py` | All GitHub API calls (read logs/diffs, write comments) |
| `log_excerpt.py` | Cuts a raw CI log down to the lines around the error |
| `diagnose.py` | Sends the excerpt and diff to the LLM, returns the diagnosis |

## Setup

Requires Python 3.10 or newer.

### 1. Virtual environment and dependencies

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks the activate script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### 2. Secrets

Copy `.env.example` to `.env` and fill in three values.

**`GITHUB_TOKEN`** - a fine-grained personal access token from
<https://github.com/settings/personal-access-tokens>, scoped to the one
repository you are watching, with these repository permissions:

| Permission | Level | Needed for |
| ---------- | ----- | ---------- |
| Actions | Read-only | listing jobs, downloading logs |
| Contents | Read and write | reading commit diffs, posting commit comments |
| Issues | Read and write | posting pull request comments |
| Pull requests | Read and write | reading PR diffs |

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
>
> **If diagnosis starts returning 403:** check the provider's billing or
> rate-limit page. An xAI team, for instance, starts with zero credits and
> refuses every request until it has some, and free signup credits there
> expire 30 days after they are claimed.

### 3. Run

```powershell
uvicorn main:app --reload --port 8000
```

Confirm at <http://127.0.0.1:8000/health>.

### 4. Expose it to GitHub

GitHub cannot reach `127.0.0.1`, so a tunnel is needed:

```powershell
ngrok http 8000
```

The webhook's Payload URL is the printed HTTPS address with `/webhook`
appended. On the free plan this address changes every restart, and the
webhook settings have to be updated to match.

Webhook settings: content type `application/json`, secret set, and only the
**Workflow runs** event selected.

## Model

`openai/gpt-oss-20b` via Groq's OpenAI-compatible endpoint at
`https://api.groq.com/openai/v1`. It is the smallest general chat model on
the account, which suits this task - connecting a log to a diff is extraction
and explanation, not multi-step reasoning.

Switching providers means changing three constants in `diagnose.py`
(`BASE_URL`, `MODEL`, `API_KEY_ENV`) and adding the matching key to `.env`.
Nothing else in the codebase is provider-specific.

Cost control comes mostly from the excerpt: a raw log is trimmed by roughly
90% before it is sent, which cuts both the bill and the chance of the model
latching onto unrelated warnings.

## Output

- `logs/run_<id>.txt` - the complete untouched logs of the failed jobs
- `diagnoses.jsonl` - one JSON object per diagnosis, appended

`diagnoses.jsonl` is a placeholder for real storage, kept so the history is
not lost.

## Note on version control

`.venv/`, `__pycache__/`, `.env`, and `logs/` are gitignored. `.env` holds
live credentials and must never be committed - git history is permanent, so
deleting the file in a later commit does not undo the exposure.
