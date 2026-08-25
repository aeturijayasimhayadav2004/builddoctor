"""BuildDoctor's GitHub actions, exposed as an MCP server.

MCP (Model Context Protocol) is a wire protocol for offering capabilities to
an AI client. It distinguishes three kinds of thing:

    resources   things a client READS, with no side effects
    tools       things a client INVOKES, with side effects expected
    prompts     reusable prompt templates a user picks

All four capabilities here write to GitHub, so all four are tools.

The important consequence of that framing: a tool's DESCRIPTION is not
documentation for a human reading this file. It is a runtime input to a
decision - the text a model reads when deciding whether this is the right
call to make. So each description below states what the tool does, when to
choose it over its neighbour, and what it costs to get it wrong.

This module deliberately contains no GitHub logic. Every tool is a thin
wrapper over the existing function in github_client.py, which keeps one
implementation of retries, error mapping and header handling rather than
two that can drift apart.

Runs over the Streamable HTTP transport (not the deprecated SSE one), and
is served as an ordinary ASGI app so uvicorn can host it exactly like the
main app.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

from typing import Annotated  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.transport_security import (  # noqa: E402
    TransportSecuritySettings,
)
from mcp.types import ToolAnnotations  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import github_client  # noqa: E402

mcp = MCPServer(
    name="builddoctor-github",
    version="1.0.0",
    instructions=(
        "Actions BuildDoctor can take on a GitHub repository after "
        "diagnosing a failed CI run. Every tool here writes to GitHub. "
        "None of them read anything."
    ),
)


class CommentResult(BaseModel):
    """A comment that was created on GitHub."""

    url: str | None = Field(default=None, description="Web URL of the new comment.")
    id: int | None = Field(default=None, description="GitHub's id for the comment.")


class LabelResult(BaseModel):
    """The labels a pull request carries after the call."""

    labels: list[str] = Field(
        default_factory=list, description="All label names now on the pull request."
    )


class RerunResult(BaseModel):
    """Confirmation that a re-run was requested."""

    requested: bool = Field(description="True when GitHub accepted the request.")
    run_id: int = Field(description="The workflow run whose failed jobs were re-run.")


# Annotations shared by the three tools that must never be repeated.
#
# idempotent_hint=False is the machine-readable form of the rule the
# project has followed since Phase 4: a failed write must not be retried,
# because a network error is indistinguishable from "it worked and the
# reply was lost". Publishing it here means any MCP client can see it,
# not only ours.
NOT_REPEATABLE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)


@mcp.tool(
    name="post_pr_comment",
    annotations=NOT_REPEATABLE,
)
async def post_pr_comment(
    repo: Annotated[str, Field(description='Repository as "owner/name", for example "octocat/hello-world".')],
    pull_number: Annotated[int, Field(description='Pull request number, as shown in its URL.')],
    body: Annotated[str, Field(description='Comment text. GitHub-flavoured Markdown is rendered.')],
) -> CommentResult:
    """Post a comment on a pull request's conversation timeline.

    Choose this when the failed workflow run belongs to a pull request:
    the conversation is where the PR author is already looking, and it is
    what a notification email quotes.

    This is NOT a review comment. It is not attached to any file or line,
    so do not use it to point at a specific diff hunk.

    NOT repeatable. Each call creates another comment, so a call whose
    outcome is unknown must not be retried - retrying turns one comment
    into two.
    """
    created = await github_client.post_pull_request_comment(repo, pull_number, body)
    print(f"  [tool] post_pr_comment repo={repo} pr=#{pull_number} -> ok", flush=True)
    return CommentResult(url=created.get("html_url"), id=created.get("id"))


@mcp.tool(
    name="post_commit_comment",
    annotations=NOT_REPEATABLE,
)
async def post_commit_comment(
    repo: Annotated[str, Field(description='Repository as "owner/name", for example "octocat/hello-world".')],
    commit_sha: Annotated[str, Field(description='Full 40-character commit SHA to attach the comment to.')],
    body: Annotated[str, Field(description='Comment text. GitHub-flavoured Markdown is rendered.')],
) -> CommentResult:
    """Post a comment attached to a single commit.

    Choose this when the failed workflow run came from a direct push and
    has no associated pull request, so there is no conversation to post
    into. The comment appears on the commit page and in the author's
    notifications.

    A commit comment cannot carry a label, so if the message needs to be
    visually distinct, that has to be done in the body text itself.

    NOT repeatable. Each call creates another comment.
    """
    created = await github_client.post_commit_comment(repo, commit_sha, body)
    print(
        f"  [tool] post_commit_comment repo={repo} sha={commit_sha[:7]} -> ok",
        flush=True,
    )
    return CommentResult(url=created.get("html_url"), id=created.get("id"))


@mcp.tool(
    name="add_pr_label",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        # Unlike the others, this one IS safe to repeat: adding a label
        # that is already present changes nothing and is not an error.
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
async def add_pr_label(
    repo: Annotated[str, Field(description='Repository as "owner/name", for example "octocat/hello-world".')],
    pull_number: Annotated[int, Field(description='Pull request number, as shown in its URL.')],
    label: Annotated[str, Field(description='Label name to add, for example "needs-review". Created automatically if it does not exist.')],
) -> LabelResult:
    """Add a label to a pull request.

    Choose this when a human needs to notice the pull request itself, not
    just a comment on it. A label shows up wherever pull requests are
    listed and can be filtered on, which a comment cannot.

    Only pull requests can be labelled. Commits cannot.

    A label that does not exist yet is created automatically. Adding a
    label that is already present does nothing and is not an error, so
    this call is safe to repeat.
    """
    names = await github_client.add_labels(repo, pull_number, [label])
    print(
        f"  [tool] add_pr_label repo={repo} pr=#{pull_number} "
        f"label={label!r} -> {names}",
        flush=True,
    )
    return LabelResult(labels=names)


@mcp.tool(
    name="rerun_workflow_job",
    annotations=NOT_REPEATABLE,
)
async def rerun_workflow_job(
    repo: Annotated[str, Field(description='Repository as "owner/name", for example "octocat/hello-world".')],
    run_id: Annotated[int, Field(description='Workflow run id whose failed jobs should be re-run.')],
) -> RerunResult:
    """Re-run only the failed jobs of a workflow run, leaving passes alone.

    Choose this ONLY when the failure looks flaky - a network timeout, a
    race, an infrastructure error - so that running the same code again
    is genuinely likely to pass. Re-running a real bug just fails again
    and costs CI minutes.

    Do NOT call this if the run's run_attempt is already above 1. That
    means the failure being looked at IS the result of an earlier re-run,
    and re-running again starts a loop that never ends, because the
    evidence barely changes between attempts.

    NOT repeatable. Each call starts a new attempt and consumes CI
    minutes, so a call whose outcome is unknown must not be retried.
    """
    await github_client.rerun_failed_jobs(repo, run_id)
    print(f"  [tool] rerun_workflow_job repo={repo} run={run_id} -> ok", flush=True)
    return RerunResult(requested=True, run_id=run_id)


# DNS-rebinding protection.
#
# The SDK checks the Host header against an allowlist and answers 421
# Misdirected Request when it does not match. The default allows only
# localhost, which is right for a server meant to be reached from the same
# machine - it stops a malicious web page resolving a name to 127.0.0.1 and
# driving a local MCP server from the browser.
#
# Inside compose the app reaches this container as "mcp:8001", so that name
# has to be added. The protection stays ON: the fix is to name the hosts we
# actually serve, not to turn the check off.
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "MCP_ALLOWED_HOSTS",
        "mcp:8001,localhost:8001,127.0.0.1:8001",
    ).split(",")
    if host.strip()
]

SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=ALLOWED_HOSTS,
    allowed_origins=["*"],
)

# Served as a plain ASGI app so uvicorn hosts it the same way it hosts the
# main app: one process model, one set of flags, one thing to reason about.
app = mcp.streamable_http_app(transport_security=SECURITY)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - inside a container, see Dockerfile
        port=int(os.environ.get("MCP_PORT", "8001")),
    )
