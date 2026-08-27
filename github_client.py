"""GitHub REST API calls that BuildDoctor needs.

Reads, about a workflow run that has just failed:
  1. Which jobs failed, and what do their logs say?
  2. What code change triggered the run?

Writes, once it has an answer:
  3. Post the diagnosis (PR comment or commit comment).
  4. Label a pull request.
  5. Re-run the failed jobs of a run.

Nothing in this file knows about FastAPI, webhooks, or the lane a failure
was sorted into; it is plain API access so it can be tested or reused on
its own.

AUTHENTICATION, SINCE PHASE 11

Every function here takes an `installation_id` and authenticates as that
GitHub App installation. There is no static token any more and no fallback
to one - one auth path, not two, so there is no way to be confused about
which credential a call actually used.

The practical consequence is that `installation_id` is a required argument
everywhere rather than an optional one with a default. A default would mean
some call could silently authenticate as the wrong tenant, which is the one
mistake this whole phase exists to make impossible.
"""

from __future__ import annotations

import asyncio
import functools

import httpx

import app_auth

GITHUB_API = "https://api.github.com"
API_VERSION = "2022-11-28"

# Asking for this media type makes GitHub return a unified diff (plain text)
# instead of the usual JSON representation of the same resource.
DIFF_ACCEPT = "application/vnd.github.v3.diff"
JSON_ACCEPT = "application/vnd.github+json"

# Log downloads are slower than metadata calls, so reads get a longer budget.
TIMEOUT = httpx.Timeout(30.0, read=60.0)


class GitHubError(RuntimeError):
    """Raised when the GitHub API returns something we cannot use."""


async def _headers(installation_id: int, accept: str = JSON_ACCEPT) -> dict:
    """Headers authenticated as one GitHub App installation.

    Async because minting a token may require a round trip to GitHub. In the
    common case app_auth answers from its cache and this never awaits
    anything real.
    """
    try:
        return await app_auth.installation_headers(installation_id, accept)
    except app_auth.AppAuthError as exc:
        # Re-raised as GitHubError so that every caller in the pipeline keeps
        # the single except clause it already has. A failure to authenticate
        # and a failure to call are the same thing from a lane's point of
        # view: the action did not happen, and the reason is in the message.
        raise GitHubError(f"could not authenticate: {exc}") from exc


def _check(
    response: httpx.Response, what: str, installation_id: int | None = None
) -> None:
    """Turn HTTP failures into errors that name their likely cause."""
    status = response.status_code
    if status == 401:
        # The cached token was rejected. Drop it, so the next call mints a
        # fresh one instead of replaying a credential GitHub has already
        # refused - otherwise a revoked or rotated installation keeps failing
        # for up to an hour until the cache entry ages out on its own.
        if installation_id is not None:
            app_auth.invalidate(installation_id)
        raise GitHubError(
            f"{what}: 401 - the installation token was rejected. It has been "
            f"discarded; the next call will mint a new one."
        )
    if status == 403:
        raise GitHubError(
            f"{what}: 403 - the installation is valid but lacks the required "
            f"permission (needs Actions: read, Contents: read and write, "
            f"Issues: read and write, Pull requests: read and write), or the "
            f"rate limit is exhausted. A permission added after installing "
            f"must be ACCEPTED on the installation before it takes effect."
        )
    if status == 404:
        raise GitHubError(
            f"{what}: 404 - wrong repo or id, or this installation does not "
            f"cover that repository."
        )
    if status >= 400:
        raise GitHubError(f"{what}: HTTP {status} - {response.text[:300]}")


# --------------------------------------------------------------------------
# Network failures
#
# httpx raises its own exceptions for DNS failures, refused connections and
# timeouts. Callers here catch GitHubError, so anything else escapes and
# kills the whole pipeline - which is how a single flaky DNS lookup once
# took out an entire diagnosis. These two decorators make sure every
# network fault arrives as a GitHubError.
#
# The split matters. A GET can be repeated safely, so reads retry. A POST
# cannot: retrying a comment posts it twice, and retrying a re-run starts
# two of them. Writes are converted but never repeated.
# --------------------------------------------------------------------------

RETRIES = 2
RETRY_BACKOFF = 1.5


def _reads(fn):
    """Retry transient network errors. Only for calls that change nothing."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        delay = 1.0
        for attempt in range(RETRIES + 1):
            try:
                return await fn(*args, **kwargs)
            except httpx.RequestError as exc:
                if attempt == RETRIES:
                    raise GitHubError(
                        f"{fn.__name__}: network error after "
                        f"{RETRIES + 1} attempts - {type(exc).__name__}: {exc}"
                    ) from exc
                print(
                    f"       network error in {fn.__name__} "
                    f"({type(exc).__name__}), retrying in {delay:.1f}s ..."
                )
                await asyncio.sleep(delay)
                delay *= RETRY_BACKOFF
    return wrapper


def _writes(fn):
    """Convert network errors, but never repeat the call."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except httpx.RequestError as exc:
            raise GitHubError(
                f"{fn.__name__}: network error - {type(exc).__name__}: {exc}. "
                f"Not retried, because repeating a write could duplicate it."
            ) from exc
    return wrapper


@_reads
async def list_failed_jobs(installation_id: int, repo: str, run_id: int) -> list:
    """Return only the jobs of `run_id` whose conclusion was a failure.

    A run can hold many jobs (matrix builds, lint + test + deploy). Most of
    them may be green; only the failed ones are worth reading.
    """
    url = f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}/jobs"
    headers = await _headers(installation_id)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            url, headers=headers, params={"per_page": 100, "filter": "latest"}
        )
    _check(response, "list jobs", installation_id)

    jobs = response.json().get("jobs", [])
    # Note: a job can also end as "timed_out" or "cancelled". Phase 1 treats
    # only an outright "failure" as interesting.
    return [job for job in jobs if job.get("conclusion") == "failure"]


@_reads
async def download_job_log(installation_id: int, repo: str, job_id: int) -> str:
    """Return the full plain-text log for one job."""
    url = f"{GITHUB_API}/repos/{repo}/actions/jobs/{job_id}/logs"
    headers = await _headers(installation_id)

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        response = await client.get(url, headers=headers)

        # GitHub replies with a redirect to a short-lived, pre-signed storage
        # URL. That URL authenticates via its own query string and rejects
        # requests that ALSO carry an Authorization header - so the follow-up
        # request deliberately sends no headers of ours.
        if response.status_code in (301, 302, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise GitHubError("download job log: redirect had no location header")
            response = await client.get(location)

    _check(response, "download job log", installation_id)
    return response.text


@_reads
async def get_pull_request_diff(
    installation_id: int, repo: str, pr_number: int
) -> str:
    """Diff of an entire pull request branch against its base branch."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    headers = await _headers(installation_id, DIFF_ACCEPT)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
    _check(response, f"pull request #{pr_number} diff", installation_id)
    return response.text


@_reads
async def get_commit_diff(installation_id: int, repo: str, sha: str) -> str:
    """Diff of a single commit against its parent.

    Requesting a commit with the diff media type returns exactly that
    comparison, so no separate call to the compare endpoint is needed.
    """
    url = f"{GITHUB_API}/repos/{repo}/commits/{sha}"
    headers = await _headers(installation_id, DIFF_ACCEPT)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
    _check(response, f"commit {sha[:7]} diff", installation_id)
    return response.text


async def get_diff_for_run(installation_id: int, payload: dict) -> dict:
    """Fetch the change that triggered a run, picking the right comparison.

    A run started by a pull request must be diffed as a whole branch against
    its base; a run started by a plain push is diffed one commit against its
    parent. The webhook payload distinguishes them via `pull_requests`.
    """
    repo = payload["repository"]["full_name"]
    run = payload["workflow_run"]
    pull_requests = run.get("pull_requests") or []

    if pull_requests:
        number = pull_requests[0]["number"]
        return {
            "source": "pull_request",
            "ref": f"PR #{number}",
            "description": f"pull request #{number} vs its base branch",
            "diff": await get_pull_request_diff(installation_id, repo, number),
        }

    head_sha = run["head_sha"]
    return {
        "source": "push",
        "ref": head_sha[:7],
        "description": f"commit {head_sha[:7]} vs its parent",
        "diff": await get_commit_diff(installation_id, repo, head_sha),
    }


# --------------------------------------------------------------------------
# Writing back to GitHub (Phase 2)
#
# Two endpoints, because GitHub stores these comments differently:
#
#   * A pull request is an issue underneath, so its conversation comments are
#     issue comments. (/pulls/{n}/comments is a different thing - review
#     comments anchored to a file and line - which is not what we want.)
#   * A commit is not an issue. It has no number and no conversation, but it
#     can carry comments of its own, keyed by SHA.
# --------------------------------------------------------------------------


@_writes
async def post_pull_request_comment(
    installation_id: int, repo: str, pr_number: int, body: str
) -> dict:
    """Add a conversation comment to a pull request."""
    url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
    headers = await _headers(installation_id)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        response = await client.post(url, headers=headers, json={"body": body})
    _check(response, f"comment on PR #{pr_number}", installation_id)
    return response.json()


@_writes
async def post_commit_comment(
    installation_id: int, repo: str, sha: str, body: str
) -> dict:
    """Add a comment directly to a commit.

    Needs the Contents permission at read AND WRITE. Read-only returns 403,
    which is easy to get wrong because reading the diff for the same commit
    needs only read - so the pipeline appears to work right up to the moment
    it tries to say something.
    """
    url = f"{GITHUB_API}/repos/{repo}/commits/{sha}/comments"
    headers = await _headers(installation_id)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        response = await client.post(url, headers=headers, json={"body": body})
    _check(response, f"comment on commit {sha[:7]}", installation_id)
    return response.json()


async def post_diagnosis(installation_id: int, payload: dict, body: str) -> dict:
    """Post `body` wherever the person who triggered this run will see it.

    Uses the same signal as get_diff_for_run: a run belonging to a pull
    request gets a PR comment, a plain push gets a commit comment.
    """
    repo = payload["repository"]["full_name"]
    run = payload["workflow_run"]
    pull_requests = run.get("pull_requests") or []

    if pull_requests:
        number = pull_requests[0]["number"]
        created = await post_pull_request_comment(
            installation_id, repo, number, body
        )
        return {
            "target": "pull_request",
            "ref": f"PR #{number}",
            "url": created.get("html_url"),
        }

    head_sha = run["head_sha"]
    created = await post_commit_comment(installation_id, repo, head_sha, body)
    return {
        "target": "commit",
        "ref": head_sha[:7],
        "url": created.get("html_url"),
    }


# --------------------------------------------------------------------------
# Actions the three lanes can take (Phase 4)
# --------------------------------------------------------------------------


def pull_request_number(payload: dict) -> int | None:
    """The PR this run belongs to, or None for a plain push.

    Same signal used by get_diff_for_run and post_diagnosis, pulled out so
    the lanes can ask the question without doing anything.
    """
    pull_requests = (payload.get("workflow_run") or {}).get("pull_requests") or []
    return pull_requests[0]["number"] if pull_requests else None


@_writes
async def rerun_failed_jobs(installation_id: int, repo: str, run_id: int) -> None:
    """Re-run only the failed jobs of a run, leaving the green ones alone.

    Needs the Actions permission at Read AND WRITE; read-only returns 403.
    The App is currently installed with Actions at read only, so this lane
    is expected to 403 until that is raised - see the note in graph.py, which
    already falls back to commenting rather than doing nothing.

    GitHub answers 201 with an empty body, so there is nothing to return -
    the new attempt appears asynchronously as run_attempt 2 of the same
    run id, which is exactly the counter the amber lane guards against.
    """
    url = f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs"
    headers = await _headers(installation_id)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        response = await client.post(url, headers=headers)
    _check(response, f"rerun failed jobs of run {run_id}", installation_id)


@_writes
async def add_labels(
    installation_id: int, repo: str, issue_number: int, labels: list
) -> list:
    """Add labels to a pull request (a PR is an issue underneath).

    GitHub creates a label that does not exist yet, so there is no need to
    define it in the repository first. Adding a label that is already
    present is not an error either, which keeps this safe to repeat.
    """
    url = f"{GITHUB_API}/repos/{repo}/issues/{issue_number}/labels"
    headers = await _headers(installation_id)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        response = await client.post(url, headers=headers, json={"labels": labels})
    _check(response, f"label PR #{issue_number}", installation_id)
    return [item.get("name") for item in response.json()]
