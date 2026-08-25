"""GitHub REST API calls that BuildDoctor needs - Phase 1.

Every call here is read-only. The module answers two questions about a
workflow run that has just failed:

  1. Which jobs failed, and what do their logs say?
  2. What code change triggered the run?

Nothing in this file knows about FastAPI or webhooks; it is plain API access
so it can be tested or reused on its own.
"""

from __future__ import annotations

import os

import httpx

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


def _headers(accept: str = JSON_ACCEPT) -> dict:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise GitHubError(
            "GITHUB_TOKEN is not set. Put it in the .env file next to main.py."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": API_VERSION,
    }


def _check(response: httpx.Response, what: str) -> None:
    """Turn HTTP failures into errors that name their likely cause."""
    status = response.status_code
    if status == 401:
        raise GitHubError(f"{what}: 401 - token is invalid, expired, or malformed.")
    if status == 403:
        raise GitHubError(
            f"{what}: 403 - token is valid but lacks the required permission "
            f"(needs Actions / Contents / Pull requests: Read-only), "
            f"or the rate limit is exhausted."
        )
    if status == 404:
        raise GitHubError(
            f"{what}: 404 - wrong repo or id, or the token is not scoped to "
            f"this repository."
        )
    if status >= 400:
        raise GitHubError(f"{what}: HTTP {status} - {response.text[:300]}")


async def list_failed_jobs(repo: str, run_id: int) -> list:
    """Return only the jobs of `run_id` whose conclusion was a failure.

    A run can hold many jobs (matrix builds, lint + test + deploy). Most of
    them may be green; only the failed ones are worth reading.
    """
    url = f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}/jobs"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            url, headers=_headers(), params={"per_page": 100, "filter": "latest"}
        )
    _check(response, "list jobs")

    jobs = response.json().get("jobs", [])
    # Note: a job can also end as "timed_out" or "cancelled". Phase 1 treats
    # only an outright "failure" as interesting.
    return [job for job in jobs if job.get("conclusion") == "failure"]


async def download_job_log(repo: str, job_id: int) -> str:
    """Return the full plain-text log for one job."""
    url = f"{GITHUB_API}/repos/{repo}/actions/jobs/{job_id}/logs"

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        response = await client.get(url, headers=_headers())

        # GitHub replies with a redirect to a short-lived, pre-signed storage
        # URL. That URL authenticates via its own query string and rejects
        # requests that ALSO carry an Authorization header - so the follow-up
        # request deliberately sends no headers of ours.
        if response.status_code in (301, 302, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise GitHubError("download job log: redirect had no location header")
            response = await client.get(location)

    _check(response, "download job log")
    return response.text


async def get_pull_request_diff(repo: str, pr_number: int) -> str:
    """Diff of an entire pull request branch against its base branch."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        response = await client.get(url, headers=_headers(DIFF_ACCEPT))
    _check(response, f"pull request #{pr_number} diff")
    return response.text


async def get_commit_diff(repo: str, sha: str) -> str:
    """Diff of a single commit against its parent.

    Requesting a commit with the diff media type returns exactly that
    comparison, so no separate call to the compare endpoint is needed.
    """
    url = f"{GITHUB_API}/repos/{repo}/commits/{sha}"
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        response = await client.get(url, headers=_headers(DIFF_ACCEPT))
    _check(response, f"commit {sha[:7]} diff")
    return response.text


async def get_diff_for_run(payload: dict) -> dict:
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
            "diff": await get_pull_request_diff(repo, number),
        }

    head_sha = run["head_sha"]
    return {
        "source": "push",
        "ref": head_sha[:7],
        "description": f"commit {head_sha[:7]} vs its parent",
        "diff": await get_commit_diff(repo, head_sha),
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


async def post_pull_request_comment(repo: str, pr_number: int, body: str) -> dict:
    """Add a conversation comment to a pull request."""
    url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        response = await client.post(url, headers=_headers(), json={"body": body})
    _check(response, f"comment on PR #{pr_number}")
    return response.json()


async def post_commit_comment(repo: str, sha: str, body: str) -> dict:
    """Add a comment directly to a commit."""
    url = f"{GITHUB_API}/repos/{repo}/commits/{sha}/comments"
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        response = await client.post(url, headers=_headers(), json={"body": body})
    _check(response, f"comment on commit {sha[:7]}")
    return response.json()


async def post_diagnosis(payload: dict, body: str) -> dict:
    """Post `body` wherever the person who triggered this run will see it.

    Uses the same signal as get_diff_for_run: a run belonging to a pull
    request gets a PR comment, a plain push gets a commit comment.
    """
    repo = payload["repository"]["full_name"]
    run = payload["workflow_run"]
    pull_requests = run.get("pull_requests") or []

    if pull_requests:
        number = pull_requests[0]["number"]
        created = await post_pull_request_comment(repo, number, body)
        return {
            "target": "pull_request",
            "ref": f"PR #{number}",
            "url": created.get("html_url"),
        }

    head_sha = run["head_sha"]
    created = await post_commit_comment(repo, head_sha, body)
    return {
        "target": "commit",
        "ref": head_sha[:7],
        "url": created.get("html_url"),
    }
