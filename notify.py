"""Tell the owner when an installation needs a decision (Phase 14).

WHY THIS EXISTS AT ALL

Phase 13 shipped approval with no way to find out that anything was waiting.
The installer was told to message the owner by hand, which is a workflow that
only survives while every installer already knows the owner personally. The
installation guide had to carry an entire numbered step reading "tell the
owner you installed it", flagged as the step people skip.

Phase 14 removes that step for most installers by approving public-only
installations automatically. What is left is the smaller, sharper case: an
installation that includes a private repository genuinely does need a human,
and that human has to hear about it.

WHY AN OUTGOING WEBHOOK AND NOT EMAIL

Email needs an account, a sending domain, a provider that will not silently
spam-folder a hobby project, and a secret with real blast radius if it leaks.
An incoming webhook URL from Discord or Slack needs none of that: it is one
string, created from a menu in about thirty seconds, it grants nothing except
the ability to post into one channel, and revoking it is deleting it.

WHY IT IS FIRE-AND-FORGET AND CANNOT RAISE

This is called from the webhook handler, which is on GitHub's ten-second
delivery clock. A notification service being slow, rate-limiting, or simply
deleted must never turn into a failed delivery - GitHub would record the App
as a broken integration, and the actual work (recording the installation) has
already succeeded by the time this runs. So every failure is caught, printed,
and dropped. A missed message is a much smaller problem than a webhook
endpoint that starts returning errors.

NOT CONFIGURED IS A SUPPORTED STATE, NOT A DEGRADED ONE. With no URL set,
this prints the message to the log and returns. The admin panel remains the
authoritative place to see what is pending; this is a convenience on top of
it, never the only copy.
"""

from __future__ import annotations

import os

import httpx

# Short. This runs inside a webhook handler with a ten-second budget that is
# already partly spent, and a notification is the least important thing
# happening in that window.
TIMEOUT = httpx.Timeout(5.0)

ENV_VAR = "OWNER_NOTIFY_WEBHOOK"


def webhook_url() -> str:
    """The configured URL, or empty. Read at call time, never logged.

    Read per call rather than at import for the same reason github_client and
    diagnose do it: the value can be set in a hosting dashboard after the
    process was written, and an import-time read bakes in whatever the
    environment looked like at boot.
    """
    return os.environ.get(ENV_VAR, "").strip()


def is_configured() -> bool:
    return bool(webhook_url())


def _payload(url: str, text: str) -> dict:
    """Shape the body for whichever service this URL belongs to.

    Slack and Discord want the same thing under different key names, and
    neither accepts the other's. Sending both keys at once is the tempting
    shortcut and it is wrong: Discord rejects a body with no `content`, so a
    combined payload would work on Slack and fail on Discord for reasons the
    log would not explain.

    The host is the only honest signal available. Anything that is not
    recognisably Slack is treated as Discord, because Discord's shape is also
    what most generic webhook receivers accept.
    """
    if "hooks.slack.com" in url:
        return {"text": text}
    return {"content": text}


async def owner(text: str) -> None:
    """Post one message. Never raises, never blocks for long.

    The message is plain text on purpose - no embeds, no blocks, no
    formatting that is specific to one service. It has to be legible in a
    log line as well as in a chat client, because with no URL configured the
    log IS the delivery.
    """
    url = webhook_url()
    if not url:
        print(f"  [notify] {ENV_VAR} not set - message not sent: {text}", flush=True)
        return

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(url, json=_payload(url, text))
        # Discord answers 204, Slack answers 200 with the body "ok". Both are
        # 2xx, so the status class is the check rather than an exact code.
        if response.is_success:
            print("  [notify] owner notified", flush=True)
        else:
            # The STATUS, never the URL. The URL is the credential here -
            # anyone holding it can post into that channel - so it is treated
            # like every other secret in this project and never printed.
            print(
                f"  [notify] webhook refused the message: HTTP "
                f"{response.status_code}",
                flush=True,
            )
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad. Every failure mode here - DNS, TLS, timeout, a
        # deleted webhook - has the same correct response: give up quietly.
        # An exception escaping this function would propagate into the
        # webhook handler and cost a delivery.
        print(
            f"  [notify] could not send: {type(exc).__name__}: {exc}",
            flush=True,
        )
