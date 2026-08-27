"""Client side of the MCP hop.

graph.py used to import github_client and call its write functions
directly. It now calls this module instead, which speaks MCP over HTTP to
mcp_server.py running in its own container.

The function names and signatures here deliberately mirror the ones in
github_client, and failures are raised as github_client.GitHubError, so the
action nodes in graph.py keep the exact error handling they had in Phase 4.
The lanes cannot tell the difference; only the transport changed.

RETRY POLICY - the important part of this file.

    Reads are not here at all. list_failed_jobs, download_job_log and
    get_diff_for_run are not exposed as MCP tools, so they stay direct
    calls in main.py and keep the @_reads retry decorator in
    github_client.

    Writes are not retried here, and must never be. That is the same rule
    as Phase 4, and it survives the extra hop for a stronger reason than
    before: if the MCP request fails, we cannot tell whether the server
    performed the GitHub write before the connection broke. A failed
    response is indistinguishable from a lost response. Retrying would
    turn one comment into two, or one re-run into two.

    So there is exactly one retry layer in the whole system - the one
    inside github_client, wrapping reads - and adding the MCP hop did not
    create a second.
"""

from __future__ import annotations

import json
import os

from mcp.client.client import Client

import github_client
from github_client import GitHubError

# Inside Docker this must be the compose SERVICE NAME ("mcp"), not
# localhost - see docker-compose.yml. The default here is the value that
# works when running the app directly on the machine, against the MCP
# container's published port.
DEFAULT_MCP_URL = "http://localhost:8001/mcp"

# How long to wait for one tool call. Generous, because the server has to
# make its own round trip to GitHub inside this window.
CALL_TIMEOUT = 45.0


def mcp_url() -> str:
    return os.environ.get("MCP_SERVER_URL", "").strip() or DEFAULT_MCP_URL


def _result_payload(result) -> dict:
    """Pull the tool's return value out of an MCP result.

    Tools declare a typed return model, so the SDK sends structured
    content and this takes the first branch. The text branch is the
    fallback for a tool with no output schema, where the payload arrives
    only as a JSON string - which is exactly what happened before the
    tools were given return models, and silently turned a comment URL
    into None.
    """
    data = result.structured_content
    if data:
        data = dict(data)
        # Some servers wrap a non-object return under "result".
        inner = data.get("result")
        return dict(inner) if isinstance(inner, dict) else data

    for block in result.content or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    return {}


def _error_text(result) -> str:
    """Readable reason from a failed tool call."""
    parts = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return " ".join(parts) or "the tool reported an error with no message"


async def _call(tool: str, arguments: dict) -> dict:
    """Invoke one MCP tool. Exactly once, whatever happens.

    Raises GitHubError on any failure so that callers written against
    github_client keep working unchanged.
    """
    url = mcp_url()
    try:
        # A fresh connection per call. The alternative - one long-lived
        # session - would need reconnect handling and a shared lock for
        # concurrent webhooks, to save a handshake on an operation that
        # already involves a round trip to GitHub.
        async with Client(url, read_timeout_seconds=CALL_TIMEOUT) as client:
            result = await client.call_tool(tool, arguments)
    except Exception as exc:  # noqa: BLE001
        # Transport-level failure: refused, DNS, timeout, protocol error.
        # NOT retried - see the module docstring. The write may already
        # have happened on the far side.
        raise GitHubError(
            f"MCP call {tool!r} to {url} failed: "
            f"{type(exc).__name__}: {exc}. Not retried, because the action "
            f"may already have been performed."
        ) from exc

    if result.is_error:
        raise GitHubError(f"MCP tool {tool!r} failed: {_error_text(result)}")

    return _result_payload(result)


# --------------------------------------------------------------------------
# The same surface github_client offers, over MCP.
# --------------------------------------------------------------------------


async def post_pull_request_comment(
    installation_id: int, repo: str, pr_number: int, body: str
) -> dict:
    payload = await _call(
        "post_pr_comment",
        {
            "installation_id": installation_id,
            "repo": repo,
            "pull_number": pr_number,
            "body": body,
        },
    )
    return {"html_url": payload.get("url"), "id": payload.get("id")}


async def post_commit_comment(
    installation_id: int, repo: str, sha: str, body: str
) -> dict:
    payload = await _call(
        "post_commit_comment",
        {
            "installation_id": installation_id,
            "repo": repo,
            "commit_sha": sha,
            "body": body,
        },
    )
    return {"html_url": payload.get("url"), "id": payload.get("id")}


async def add_labels(
    installation_id: int, repo: str, issue_number: int, labels: list
) -> list:
    """Add labels one at a time; the MCP tool takes a single label."""
    added: list = []
    for label in labels:
        payload = await _call(
            "add_pr_label",
            {
                "installation_id": installation_id,
                "repo": repo,
                "pull_number": issue_number,
                "label": label,
            },
        )
        added = payload.get("labels") or added
    return added


async def rerun_failed_jobs(installation_id: int, repo: str, run_id: int) -> None:
    await _call(
        "rerun_workflow_job",
        {"installation_id": installation_id, "repo": repo, "run_id": run_id},
    )


async def post_diagnosis(installation_id: int, payload: dict, body: str) -> dict:
    """Post `body` wherever the person who triggered this run will see it.

    The PR-or-push decision stays on the client side deliberately. The MCP
    tools are primitives - "comment on a PR", "comment on a commit" - and
    choosing between them is BuildDoctor's policy, not GitHub's. Baking
    the choice into a server-side tool would hide the decision from the
    lane that is supposed to be making it.

    Reads payload only; makes no API call of its own.
    """
    repo = payload["repository"]["full_name"]
    run = payload["workflow_run"]
    number = github_client.pull_request_number(payload)

    if number is not None:
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
