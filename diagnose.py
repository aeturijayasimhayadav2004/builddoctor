"""Ask an LLM what broke the build.

The provider speaks the OpenAI chat-completions protocol, so the official
`openai` SDK is used with its base_url repointed. Nothing else about the call
is provider-specific - swapping providers is a change to BASE_URL, MODEL and
the API key name, and nothing else.

Currently pointed at Groq (an inference provider with a free tier), serving
an open-weights model. Note Groq is a different company from xAI's Grok.

The model is deliberately a cheap non-reasoning one: this is an extraction
and explanation task over evidence we already gathered, not a problem that
needs multi-step reasoning.
"""

from __future__ import annotations

import os

from openai import AsyncOpenAI

BASE_URL = "https://api.groq.com/openai/v1"
MODEL = "openai/gpt-oss-20b"
API_KEY_ENV = "GROQ_API_KEY"

# Cost stop-loss. Excerpting already shrinks the log by ~90%; these caps
# protect against pathological cases (a log where every line is an error, or
# a diff containing a regenerated lockfile).
MAX_EXCERPT_CHARS = 6000
MAX_DIFF_CHARS = 6000

# gpt-oss models think before answering, and that thinking is billed against
# the same output budget as the answer. Left unbounded it will happily spend
# the entire allowance reasoning and return empty content with
# finish_reason="length" - intermittently, which is worse than always.
# "low" keeps the answer well inside the budget and costs ~7x fewer output
# tokens. Set to None for providers that reject the parameter.
REASONING_EFFORT = "low"
MAX_OUTPUT_TOKENS = 1000

SYSTEM_PROMPT = """You are a CI build failure triage assistant.

You will be given two things about a failed GitHub Actions run:
  - a LOG EXCERPT: this is the SYMPTOM. It shows what broke, taken from
    around the error markers the CI runner emitted.
  - a DIFF: this is the LIKELY CAUSE. It shows the code change that
    triggered this run.

Your job is to connect the two: explain what broke, and why the change
caused it.

Rules:
- Answer in 2 to 3 plain sentences. No preamble, no headings, no bullet
  lists.
- Be specific. Name the actual file, package, command, or setting
  involved. Never say "a dependency" when the log names one.
- Then give exactly ONE concrete fix, on its own final line, starting
  with "Fix: ".
- If the diff does not plausibly explain the error, say so plainly
  instead of inventing a connection between them.
- Do not speculate about code you were not shown."""

USER_TEMPLATE = """Repository: {repo}
Failed job: {job_name}
Failed step: {step_name}

=== LOG EXCERPT (the symptom) ===
{log_excerpt}

=== DIFF (the likely cause) ===
{diff}"""


class DiagnosisError(RuntimeError):
    """Raised when the model cannot be reached or returns nothing usable."""


def _truncate(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n<{label} truncated at {limit} characters>"


def build_user_prompt(
    log_excerpt: str,
    diff: str,
    repo: str = "unknown",
    job_name: str = "unknown",
    step_name: str = "unknown",
) -> str:
    """Assemble the user message. Separate so it can be inspected or logged."""
    return USER_TEMPLATE.format(
        repo=repo,
        job_name=job_name,
        step_name=step_name,
        log_excerpt=_truncate(log_excerpt, MAX_EXCERPT_CHARS, "log excerpt")
        or "<no log excerpt available>",
        diff=_truncate(diff, MAX_DIFF_CHARS, "diff") or "<no diff available>",
    )


async def diagnose_failure(
    log_excerpt: str,
    diff: str,
    repo: str = "unknown",
    job_name: str = "unknown",
    step_name: str = "unknown",
) -> str:
    """Return a short plain-English diagnosis of why the build failed."""
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise DiagnosisError(
            f"{API_KEY_ENV} is not set. Add it to the .env file next to main.py."
        )

    client = AsyncOpenAI(api_key=api_key, base_url=BASE_URL, timeout=60.0)
    user_prompt = build_user_prompt(log_excerpt, diff, repo, job_name, step_name)

    request = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        # Near-deterministic: the same failure should diagnose the same
        # way every time. This is not a creative task.
        "temperature": 0.2,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    if REASONING_EFFORT:
        request["reasoning_effort"] = REASONING_EFFORT

    try:
        response = await client.chat.completions.create(**request)
    except Exception as exc:  # noqa: BLE001
        # Not every provider understands reasoning_effort. Retry once without
        # it rather than failing the whole diagnosis over an extra parameter.
        if REASONING_EFFORT and "reasoning_effort" in str(exc):
            request.pop("reasoning_effort")
            try:
                response = await client.chat.completions.create(**request)
            except Exception as retry_exc:  # noqa: BLE001
                raise DiagnosisError(
                    f"{MODEL} request failed: {retry_exc}"
                ) from retry_exc
        else:
            raise DiagnosisError(f"{MODEL} request failed: {exc}") from exc

    if not response.choices:
        raise DiagnosisError("provider returned no choices")

    choice = response.choices[0]
    text = (choice.message.content or "").strip()
    if not text:
        raise DiagnosisError(
            f"provider returned an empty diagnosis "
            f"(finish_reason={choice.finish_reason!r}) - if this is 'length', "
            f"the model spent its whole output budget on reasoning; lower "
            f"REASONING_EFFORT or raise MAX_OUTPUT_TOKENS"
        )

    usage = getattr(response, "usage", None)
    if usage:
        print(
            f"       model={MODEL} tokens in/out="
            f"{usage.prompt_tokens}/{usage.completion_tokens}"
        )

    return text
