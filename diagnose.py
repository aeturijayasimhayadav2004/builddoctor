"""Ask an LLM what broke the build, and which lane it belongs in.

The provider speaks the OpenAI chat-completions protocol, so the official
`openai` SDK is used with its base_url repointed. Nothing else about the call
is provider-specific - swapping providers is a change to BASE_URL, MODEL and
the API key name, and nothing else.

Currently pointed at Groq (an inference provider with a free tier), serving
an open-weights model. Note Groq is a different company from xAI's Grok.

Phase 4 changed the return type from a plain string to a Triage object:
the same diagnosis text as before, plus a category that decides what the
pipeline does next.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

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

# The three lanes. Colour names are the ones used throughout the project:
#   informational = teal, safe_auto_fix = amber, needs_review = coral.
INFORMATIONAL = "informational"
SAFE_AUTO_FIX = "safe_auto_fix"
NEEDS_REVIEW = "needs_review"

CATEGORIES = (INFORMATIONAL, SAFE_AUTO_FIX, NEEDS_REVIEW)

# The category anything uncertain, malformed or unrecognised falls back to.
# It is the only lane that takes no action beyond leaving a comment, which
# is what makes it the safe default.
FALLBACK_CATEGORY = INFORMATIONAL

# Handed to the provider as response_format={"type": "json_schema", ...}.
# With strict=True the provider constrains decoding, so "category" cannot
# come back as anything but one of the three literals below - that is
# enforced while the tokens are generated, not checked afterwards.
#
# "diagnosis" is listed FIRST on purpose. Generation is left to right, so
# the model writes its explanation before it has to commit to a label. It
# reasons, then categorises, rather than categorising then rationalising.
TRIAGE_SCHEMA = {
    "name": "build_failure_triage",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "diagnosis": {"type": "string"},
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "reason": {"type": "string"},
        },
        "required": ["diagnosis", "category", "reason"],
        "additionalProperties": False,
    },
}

SYSTEM_PROMPT = """You are a CI build failure triage assistant.

You will be given two things about a failed GitHub Actions run:
  - a LOG EXCERPT: this is the SYMPTOM. It shows what broke, taken from
    around the error markers the CI runner emitted.
  - a DIFF: this is the LIKELY CAUSE. It shows the code change that
    triggered this run.

You have two jobs.

JOB 1 - DIAGNOSE (the "diagnosis" field)

Connect the two: explain what broke, and why the change caused it.
- Answer in 2 to 3 plain sentences. No preamble, no headings, no bullet
  lists.
- Be specific. Name the actual file, package, command, or setting
  involved. Never say "a dependency" when the log names one.
- Then give exactly ONE concrete fix, on its own final line, starting
  with "Fix: ".
- If the diff does not plausibly explain the error, say so plainly
  instead of inventing a connection between them.
- Do not speculate about code you were not shown.

JOB 2 - CATEGORISE (the "category" field)

Work through these steps IN ORDER. Stop at the first one that applies and
answer with its category.

STEP 1. Does the failure involve secrets, credentials, tokens, API keys,
        permissions, or a security or vulnerability scan?
        YES -> "needs_review". Stop.

STEP 2. Name the ONE file a person would edit to fix this. Is that file
        build or CI machinery: anything under .github/workflows/, a
        Dockerfile, requirements.txt, setup.py, pyproject.toml, a Makefile,
        a lockfile, or similar?
        YES -> "informational". Stop.
        This holds however obviously wrong that file is - a pinned version
        that does not exist, a mistyped command, a missing install step, a
        bad matrix entry. Such a file is machinery, not program behaviour,
        and STEP 4 never applies to it.

STEP 3. Is the failure FLAKY: nothing in the diff plausibly explains it,
        and re-running the same code is genuinely likely to pass?
        Signs: network timeout, connection reset or refused to an external
        host, a step that hung or timed out, "resource temporarily
        unavailable", a runner or infrastructure error, a race condition, a
        test that depends on timing or random values.
        YES -> "safe_auto_fix". Stop.
        Do NOT answer yes merely because you cannot see the cause. Answer
        yes only when re-running unchanged is likely to succeed.

STEP 4. Would the fix be an edit to source code or test code: a wrong
        assertion, broken logic, or an import of a module the project is
        supposed to contain but does not?
        YES -> "needs_review". Stop.

STEP 5. Otherwise -> "informational".
        This is also the answer whenever you are unsure. A needless extra
        comment costs nothing; a wrong automated action costs trust.

JOB 3 - JUSTIFY (the "reason" field)

One sentence naming which rule above you applied and why."""

USER_TEMPLATE = """Repository: {repo}
Failed job: {job_name}
Failed step: {step_name}

=== LOG EXCERPT (the symptom) ===
{log_excerpt}

=== DIFF (the likely cause) ===
{diff}"""

# Appended on the retry, if the first response somehow fails validation.
RETRY_NUDGE = (
    "Your previous reply was not valid. Reply with a single JSON object and "
    'nothing else, with exactly these keys: "diagnosis" (string), '
    '"category" (one of: ' + ", ".join(CATEGORIES) + '), "reason" (string).'
)


class DiagnosisError(RuntimeError):
    """Raised when the model cannot be reached or returns nothing usable."""


@dataclass
class Triage:
    """What the model decided: the explanation, the lane, and the why."""

    diagnosis: str
    category: str
    reason: str

    @property
    def is_fallback(self) -> bool:
        return self.reason.startswith("[fallback]")


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


def parse_triage(text: str) -> Triage | None:
    """Turn the model's reply into a Triage, or None if it is unusable.

    Belt and braces. strict json_schema should make every one of these
    checks impossible to fail, but "should" is doing a lot of work in a
    sentence about someone else's inference server.
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    diagnosis = (data.get("diagnosis") or "").strip()
    category = (data.get("category") or "").strip()
    reason = (data.get("reason") or "").strip()

    if not diagnosis or category not in CATEGORIES:
        return None
    return Triage(diagnosis=diagnosis, category=category, reason=reason)


async def _call(client: AsyncOpenAI, messages: list[dict]) -> str:
    """One chat completion with the schema attached. Returns raw content."""
    request = {
        "model": MODEL,
        "messages": messages,
        # Near-deterministic: the same failure should diagnose and classify
        # the same way every time. This is not a creative task.
        "temperature": 0.2,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_schema", "json_schema": TRIAGE_SCHEMA},
    }
    if REASONING_EFFORT:
        request["reasoning_effort"] = REASONING_EFFORT

    try:
        response = await client.chat.completions.create(**request)
    except Exception as exc:  # noqa: BLE001
        # Not every provider understands reasoning_effort. Retry once
        # without it rather than failing over an extra parameter.
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
    usage = getattr(response, "usage", None)
    if usage:
        print(
            f"       model={MODEL} tokens in/out="
            f"{usage.prompt_tokens}/{usage.completion_tokens}"
        )

    text = (choice.message.content or "").strip()
    if not text:
        raise DiagnosisError(
            f"provider returned an empty diagnosis "
            f"(finish_reason={choice.finish_reason!r}) - if this is 'length', "
            f"the model spent its whole output budget on reasoning; lower "
            f"REASONING_EFFORT or raise MAX_OUTPUT_TOKENS"
        )
    return text


async def diagnose_failure(
    log_excerpt: str,
    diff: str,
    repo: str = "unknown",
    job_name: str = "unknown",
    step_name: str = "unknown",
) -> Triage:
    """Diagnose the failure and decide which lane it belongs in.

    Never raises over a malformed classification. If the reply cannot be
    validated twice in a row, the diagnosis text is kept and the category
    falls back to "informational" - the lane that only leaves a comment.
    Failing into a lane that TAKES AN ACTION would be the dangerous choice.
    """
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise DiagnosisError(
            f"{API_KEY_ENV} is not set. Add it to the .env file next to main.py."
        )

    client = AsyncOpenAI(api_key=api_key, base_url=BASE_URL, timeout=60.0)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_user_prompt(log_excerpt, diff, repo, job_name, step_name),
        },
    ]

    first = await _call(client, messages)
    triage = parse_triage(first)
    if triage:
        return triage

    print("       classification was malformed, retrying once ...")
    messages = messages + [
        {"role": "assistant", "content": first},
        {"role": "user", "content": RETRY_NUDGE},
    ]
    second = await _call(client, messages)
    triage = parse_triage(second)
    if triage:
        return triage

    # Both attempts unusable. Keep whatever text came back so a human still
    # sees something, and take the safe lane.
    print(f"       classification failed twice, defaulting to {FALLBACK_CATEGORY}")
    return Triage(
        diagnosis=second or first,
        category=FALLBACK_CATEGORY,
        reason="[fallback] the model did not return a usable category twice",
    )
