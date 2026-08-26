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
from dataclasses import dataclass, field

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

# THE SCHEMA IS THE GATE.
#
# Until Phase 8.5 this asked for one "category" field, and the ordering of
# STEP 1 to STEP 5 in the prompt was only an instruction. The eval found
# what that costs: on a workflow failing with "Resource not accessible by
# integration" after `permissions:` was deleted, STEP 1 says security ->
# needs_review and STOP, and the model answered "informational" with the
# reason "Step 2: ... which is build/CI machinery". It was not confused
# about the facts. It skipped a step, because nothing made it evaluate
# them in order rather than pattern-match to whichever read most relevant.
#
# So the model no longer names a lane at all. It answers the four
# questions, and the CODE applies the first-match rule.
#
# Two things make that stronger than an instruction:
#
#   1. Generation is left to right, and constrained decoding emits the
#      keys in the order declared here. The model must commit to
#      step_1_security_triggered before step_2 exists as a token to write.
#      The ordering stops being a request and becomes a property of how
#      the answer is produced.
#
#   2. There is no single field where the wrong lane can be written down.
#      Whatever the model would have "wanted" to answer, if it marks
#      step 1 true then derive_category() returns needs_review.
#
# The cost is real and worth stating: four extra booleans and four short
# reasons, roughly 120-200 more output tokens on every diagnosis. Phase 8
# runs used 154-276 tokens against a 1000 budget, so it fits - but it is
# more expensive per call, and on a rate-limited tier that is felt.
#
# "diagnosis" stays FIRST, for the reason it always was: the model writes
# its explanation before it has to answer anything categorical, so it
# reasons and then classifies rather than classifying then rationalising.
TRIAGE_SCHEMA = {
    "name": "build_failure_triage",
    "strict": True,
    "schema": {
        "type": "object",
        # Key order here is the evaluation order. Do not reorder these
        # without understanding that the ordering IS the mechanism.
        "properties": {
            "diagnosis": {"type": "string"},
            "step_1_security_triggered": {"type": "boolean"},
            "step_1_reason": {"type": "string"},
            "step_2_machinery_triggered": {"type": "boolean"},
            "step_2_reason": {"type": "string"},
            "step_3_flaky_triggered": {"type": "boolean"},
            "step_3_reason": {"type": "string"},
            "step_4_source_code_triggered": {"type": "boolean"},
            "step_4_reason": {"type": "string"},
        },
        "required": [
            "diagnosis",
            "step_1_security_triggered",
            "step_1_reason",
            "step_2_machinery_triggered",
            "step_2_reason",
            "step_3_flaky_triggered",
            "step_3_reason",
            "step_4_source_code_triggered",
            "step_4_reason",
        ],
        "additionalProperties": False,
    },
}

# The first-match rule, as data. Order is STEP 1 to STEP 4; the first one
# the model marks true decides the lane. STEP 5 - all four false - is the
# fallthrough at the bottom of derive_category().
#
# Note STEP 1 and STEP 4 both land on needs_review. They are kept separate
# because they are different questions and the eval needs to tell them
# apart: a security failure reaching the right lane via STEP 4 would look
# correct in the aggregate while STEP 1 was still being skipped.
STEP_RULES = (
    ("step_1_security_triggered", "step_1_reason", NEEDS_REVIEW,
     "STEP 1 (secrets, credentials, permissions or a security scan)"),
    ("step_2_machinery_triggered", "step_2_reason", INFORMATIONAL,
     "STEP 2 (build or dependency machinery)"),
    ("step_3_flaky_triggered", "step_3_reason", SAFE_AUTO_FIX,
     "STEP 3 (flaky, infrastructure or timing)"),
    ("step_4_source_code_triggered", "step_4_reason", NEEDS_REVIEW,
     "STEP 4 (source or test code)"),
)


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

JOB 2 - EVALUATE EACH STEP (the step_N_* fields)

There are four questions below. Answer EVERY one of them with true or
false and a one-line reason, in the order they are written.

You are NOT choosing a category. There is no category field to fill in.
The rule is "the first step marked true decides the outcome", and that
rule is applied afterwards, in code, by the program calling you.

So answer each step independently and honestly, on its own merits. You do
not need to make your answers consistent with each other, and you must not
work backwards from an outcome you have in mind. If STEP 1 is true, say
so, even when a later step also looks true - marking a later step true as
well changes nothing, because the earliest true one wins.

STEP 1. Does the failure involve secrets, credentials, tokens, API keys,
        permissions, or a security or vulnerability scan?
        -> step_1_security_triggered

STEP 2. Name the ONE file a person would edit to fix this. Is that file
        BUILD OR DEPENDENCY MACHINERY rather than program behaviour?

        Machinery means a file whose job is to declare WHAT TO INSTALL,
        WHICH VERSIONS to use, or HOW TO BUILD AND RUN - in any language
        or ecosystem. Judge by that role, not by whether the filename
        appears below. These are examples, not the whole list:

          CI config     anything under .github/workflows/, .gitlab-ci.yml,
                        .circleci/config.yml, azure-pipelines.yml
          Build / image Dockerfile, docker-compose.yml, Makefile
          Dependencies  requirements.txt, setup.py, pyproject.toml,
                        package.json, Cargo.toml, go.mod, pom.xml,
                        build.gradle, composer.json
          Lockfiles     package-lock.json, yarn.lock, poetry.lock,
                        Cargo.lock, go.sum, and the equivalent in any
                        other ecosystem

        -> step_2_machinery_triggered

        A dependency manifest IS machinery even though it sits at the
        repository root and developers edit it by hand. Declaring a
        package is not program behaviour. If the fix is to add, remove or
        correct a dependency entry or a version number, that is STEP 2 and
        NOT STEP 4 - whatever the language.

        This holds however obviously wrong that file is - a pinned version
        that does not exist, a mistyped command, a missing install step, a
        bad matrix entry, a package name that is not in the registry.

STEP 3. Is the failure FLAKY: nothing in the diff plausibly explains it,
        and re-running the same code is genuinely likely to pass?
        Signs: network timeout, connection reset or refused to an external
        host, a step that hung or timed out, "resource temporarily
        unavailable", a runner or infrastructure error, a race condition, a
        test that depends on timing or random values.
        -> step_3_flaky_triggered
        Do NOT answer yes merely because you cannot see the cause. Answer
        yes only when re-running unchanged is likely to succeed.

STEP 4. Would the fix be an edit to source code or test code: a wrong
        assertion, broken logic, or an import of a module the project is
        supposed to contain but does not?
        -> step_4_source_code_triggered

STEP 5. There is no field for this one. If you mark all four of the
        steps above false, the program treats the failure as
        "informational" by default. That is also the right outcome
        whenever you are unsure: a needless extra comment costs nothing,
        while a wrong automated action costs trust.

JOB 3 - JUSTIFY (the step_N_reason fields)

Each step gets one short sentence saying why you answered true or false.
Name the specific evidence - the file, the package, the error - rather
than restating the question.

USING THE PAST FAILURE SECTION

The message may contain a section titled PAST SIMILAR FAILURE. It is a
diagnosis of a DIFFERENT, EARLIER build that an automated similarity
search believed resembles this one. The search compares wording. It has no
understanding of either build and it is sometimes wrong.

Handle it in this order, and do not reorder these:

  A. First decide what broke using ONLY the LOG EXCERPT and the DIFF.
  B. Then read the past failure. If it agrees with what you already
     decided, you may borrow specifics from it and you should say so in
     your diagnosis, for example "this is the same failure as an earlier
     run, where ...". If it disagrees with the evidence in front of you,
     the EVIDENCE WINS: ignore the past failure entirely and do not
     mention it.
  C. JOB 2 is decided from the LOG EXCERPT and the DIFF alone. The past
     failure's category is not evidence and does not appear anywhere in
     STEP 1 to STEP 5. Never pick a category because the past failure
     used it. Work through the steps as if that section were not there.

Never state something as fact because the past record said it. It is a
record of what was concluded once, not a record of what is true."""

USER_TEMPLATE = """Repository: {repo}
Failed job: {job_name}
Failed step: {step_name}

=== LOG EXCERPT (the symptom) ===
{log_excerpt}

=== DIFF (the likely cause) ===
{diff}{past}"""

# Appended to the user message only when memory found a match above the
# threshold. When there is no match this is the empty string and the
# prompt is byte-for-byte the Phase 4 prompt.
PAST_TEMPLATE = """

=== PAST SIMILAR FAILURE (context, may be wrong - see instructions) ===
{past_summary}"""

# Appended on the retry, if the first response somehow fails validation.
RETRY_NUDGE = (
    "Your previous reply was not valid. Reply with a single JSON object and "
    "nothing else, with exactly these keys, in this order: "
    '"diagnosis" (string), then for each of N = 1, 2, 3, 4 a boolean '
    '"step_N_..._triggered" and a string "step_N_reason". Do not include a '
    '"category" key - you are not choosing a category, only answering the '
    "four questions."
)


class DiagnosisError(RuntimeError):
    """Raised when the model cannot be reached or returns nothing usable."""


@dataclass
class Triage:
    """What the model decided: the explanation, the lane, and the why.

    `category` and `reason` are DERIVED, not answered. The model fills in
    `steps`; derive_category() turns that into a lane. The three public
    attributes are unchanged from Phase 4 so nothing downstream - graph.py,
    main.py, the database - had to change when the schema did.
    """

    diagnosis: str
    category: str
    reason: str
    # Every step the model answered, so a wrong lane can be traced to the
    # question that produced it rather than to a single opaque label.
    steps: dict = field(default_factory=dict)

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
    past_summary: str = "",
) -> str:
    """Assemble the user message. Separate so it can be inspected or logged.

    The past-failure section is appended LAST, after the evidence. Reading
    order matters: the model meets the log and the diff before it meets
    anyone else's conclusion about them, which is the same reason the
    schema puts "diagnosis" before the step questions.
    """
    return USER_TEMPLATE.format(
        repo=repo,
        job_name=job_name,
        step_name=step_name,
        log_excerpt=_truncate(log_excerpt, MAX_EXCERPT_CHARS, "log excerpt")
        or "<no log excerpt available>",
        diff=_truncate(diff, MAX_DIFF_CHARS, "diff") or "<no diff available>",
        past=PAST_TEMPLATE.format(past_summary=past_summary) if past_summary else "",
    )


def derive_category(steps: dict) -> tuple[str, str]:
    """Apply the first-match rule in code. Returns (category, reason).

    This is JOB 2, and the model no longer does it. It answers four
    independent questions; the ordering of STEP_RULES turns those answers
    into a lane. Doing it here rather than asking for a label means the
    ordering cannot be skipped under pressure - which is precisely the
    failure the Phase 8 eval caught.

    All four false is STEP 5: informational, the lane that only comments.
    That is also where anything uncertain lands, on the standing rule that
    a needless comment costs nothing and a wrong action costs trust.
    """
    for flag, reason_key, category, label in STEP_RULES:
        if steps.get(flag) is True:
            why = (steps.get(reason_key) or "").strip()
            return category, f"{label}: {why}" if why else label
    return FALLBACK_CATEGORY, (
        "STEP 5 (default): none of the four steps applied"
    )


def parse_triage(text: str) -> Triage | None:
    """Turn the model's reply into a Triage, or None if it is unusable.

    Belt and braces. strict json_schema should make every one of these
    checks impossible to fail, but "should" is doing a lot of work in a
    sentence about someone else's inference server.

    A MISSING BOOLEAN IS NOT TREATED AS FALSE. If any of the four is
    absent or not a bool, this returns None so the caller retries. Reading
    a missing step_1 as false would silently answer the security question
    "no" on a malformed reply, which is the one direction this whole
    change exists to prevent.
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
    if not diagnosis:
        return None

    steps: dict = {}
    for flag, reason_key, _category, _label in STEP_RULES:
        value = data.get(flag)
        if not isinstance(value, bool):
            return None
        steps[flag] = value
        steps[reason_key] = (data.get(reason_key) or "").strip()

    category, reason = derive_category(steps)
    return Triage(
        diagnosis=diagnosis, category=category, reason=reason, steps=steps
    )


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
    past_summary: str = "",
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
            "content": build_user_prompt(
                log_excerpt, diff, repo, job_name, step_name, past_summary
            ),
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
