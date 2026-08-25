"""Turning a raw GitHub Actions log into the part worth reading.

A CI log is mostly setup, environment dumps and teardown. The lines that
explain a failure sit around the runner's own "##[error]" markers, so those
are what this module extracts - for the console and for the model alike.
"""

from __future__ import annotations

import re

ERROR_MARKER = "##[error]"
LINES_BEFORE = 5
LINES_AFTER = 10

# Every Actions log line is prefixed with an ISO timestamp, e.g.
# "2026-08-25T12:12:05.3164876Z ". It carries no diagnostic value and is
# roughly a third of the characters, so it is stripped from excerpts.
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")


def strip_timestamp(line: str) -> str:
    return TIMESTAMP.sub("", line)


def extract_error_excerpt(
    log_text: str,
    before: int = LINES_BEFORE,
    after: int = LINES_AFTER,
) -> str:
    """Return the lines around each error marker in `log_text`.

    Windows that overlap are merged, so a cluster of adjacent errors produces
    one continuous excerpt rather than the same lines repeated.

    If the log holds no error marker at all, the tail is returned as a
    fallback - it is a poor excerpt, but better than nothing.
    """
    lines = log_text.splitlines()
    if not lines:
        return "<log was empty>"

    hits = [i for i, line in enumerate(lines) if ERROR_MARKER in line]

    if not hits:
        tail = [strip_timestamp(line) for line in lines[-(before + after):]]
        return (
            f"<no '{ERROR_MARKER}' marker found; showing last {len(tail)} "
            f"lines of {len(lines)}>\n" + "\n".join(tail)
        )

    windows: list[list[int]] = []
    for index in hits:
        start = max(0, index - before)
        end = min(len(lines), index + after + 1)
        if windows and start <= windows[-1][1]:
            # Overlaps the previous window - extend it instead of adding one.
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])

    chunks = []
    for start, end in windows:
        chunks.append(f"--- log lines {start + 1}-{end} of {len(lines)} ---")
        chunks.extend(strip_timestamp(line) for line in lines[start:end])

    return "\n".join(chunks)
