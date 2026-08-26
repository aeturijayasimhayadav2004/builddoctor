"""Production entrypoint: one process, both ASGI apps.

WHY THIS FILE EXISTS

Phase 5 split the four GitHub write actions into their own MCP server, and
docker-compose runs it as its own container - app and mcp, two services on
a private network, the app reaching the server at "mcp:8001". That is the
honest topology and it is still what local development uses.

Render cannot host that shape for free. Two facts from its documentation,
checked in Phase 9 rather than remembered:

  * Private services have no free instance type. They start at Starter.
  * "Free web services can send private network requests, but they can't
    receive them."

The second one is the important one. It means a free web service cannot
stand in for the private service either: it would not merely be public, it
would be UNREACHABLE by the app. There is no free two-service arrangement.

So: one service. But one service does not have to mean one process, and
the first attempt ran both uvicorns side by side in the same container.
That was measured, not assumed, and it does not fit:

    app + mcp, two processes   549 MiB   -> OOM-killed under a 512 MiB cap
    app alone,  one process    446 MiB   -> healthy under the same cap

The ~103 MiB difference is a second Python interpreter importing torch,
sentence-transformers and SQLAlchemy all over again. Render's free instance
is 512 MiB. So the second interpreter is the thing that has to go, and this
file is what removes it.

WHAT THIS CHANGES ABOUT THE ARCHITECTURE: NOTHING

main.py, mcp_server.py and mcp_client.py are untouched, and so is
docker-compose.yml. This module imports the two apps that already exist and
arranges them; it defines no route, no tool and no policy of its own.

The MCP hop is still a real hop. mcp_client still opens an HTTP connection,
still performs the JSON-RPC session handshake, still calls a tool by name
over the wire, and still refuses to retry a write whose outcome is unknown.
The only thing that changed is which socket that connection lands on - a
loopback one inside this container, instead of a private-network one
between two containers. Same protocol, same server code, same rules.

Because the difference is a URL and not a code path, one env var switches
between them and the same image runs both ways:

    compose    MCP_SERVER_URL=http://mcp:8001/mcp
    Render     MCP_SERVER_URL=http://localhost:10000/internal/mcp

WHY THE MCP ENDPOINT IS NOT PUBLICLY USABLE

It is mounted on the public app, so https://<host>/internal/mcp resolves
rather than 404s. It still cannot be used.

mcp_server.py's DNS-rebinding guard compares the Host header against
MCP_ALLOWED_HOSTS, which in production names only localhost:10000 and
127.0.0.1:10000. A request arriving through Render's router carries
Host: <something>.onrender.com, matches nothing, and is answered
421 Misdirected Request before it ever reaches a tool.

That guard is not new and was not weakened to make this work - it is the
same setting that already had to name "mcp:8001" to work under compose.
Phase 5 built it to stop a browser driving a local MCP server; here it
does a second job for free.

This is weaker than a private service in one specific way, and it is worth
naming rather than glossing: a private service is unreachable because the
network has no route to it, whereas this is reachable but refuses. A guard
that is misconfigured fails open; a missing route cannot be misconfigured.
MCP_ALLOWED_HOSTS is therefore load-bearing in production in a way it was
not before, and the end-to-end check in the README asserts the 421.

THE ONE PIECE OF REAL WIRING

Mounting an ASGI app does NOT run its lifespan. Starlette runs the lifespan
of the outermost app only. mcp_server.app's lifespan is what starts the
StreamableHTTP session manager, so without the wrapper below, every tool
call fails with "Task group is not initialized" - a mounted-app mistake
that reads like a protocol bug and sends you looking in the wrong file.

So main's existing lifespan is WRAPPED, not replaced. The original still
runs first and unchanged (wait for database, migrations, load the embedding
model); the MCP session manager starts inside it; both unwind in reverse.
"""

from __future__ import annotations

import contextlib

from dotenv import load_dotenv

# Same reason as main.py: db builds its engine at import time and reads
# DATABASE_URL right then, so this has to happen before that import runs.
load_dotenv()

import main  # noqa: E402
import mcp_server  # noqa: E402

# Where the MCP server hangs off the public app.
#
# mcp_server.app serves its endpoint at /mcp (the SDK's default), so
# mounting it here puts the real endpoint at /internal/mcp. MCP_SERVER_URL
# must point at the FULL path - the mount prefix alone is not an endpoint
# and answers 404, which looks like the server failed to start.
MCP_MOUNT = "/internal"

# The public app IS main's app, not a new wrapper around it. Anything else
# would mean re-registering its routes, its CORS middleware and its
# exception handlers somewhere they could drift out of step with main.py.
app = main.app

_main_lifespan = app.router.lifespan_context


@contextlib.asynccontextmanager
async def _combined_lifespan(fastapi_app):
    """main's startup, with the MCP session manager started inside it."""
    async with _main_lifespan(fastapi_app):
        async with mcp_server.app.router.lifespan_context(mcp_server.app):
            print(f"  mcp mounted at {MCP_MOUNT}/mcp (loopback only)", flush=True)
            yield


app.router.lifespan_context = _combined_lifespan

# After the lifespan swap, so that a failure above leaves no half-wired app
# serving an endpoint whose session manager was never started.
app.mount(MCP_MOUNT, mcp_server.app)
