"""JQL composition with project-key validation and an extra-filter injection guard.

These are pure functions — no network calls, no state. They live separately from
the JiraClient class so callers can compose JQL outside of any request flow.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, tzinfo

# JIRA project keys: ASCII letter followed by 1-19 alphanumeric/underscore chars.
_SAFE_PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,19}$")

# Tokens that must not appear in a trusted-operator extra_filter clause.
# Note: this is a defensive guard against accidental SQL-style injection in a JQL
# string; it is NOT a security boundary. Callers must still treat extra_filter as
# operator-supplied, not user-supplied.
_JQL_DANGEROUS = re.compile(
    r";\s*|--(?!.*\bORDER\b)|/\*|\*/"
    r"|\b(UNION|DROP|DELETE|INSERT|UPDATE|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)


def _check_project_key(project_key: str) -> None:
    if not _SAFE_PROJECT_KEY.match(project_key):
        raise ValueError(
            f"Invalid project key: {project_key!r}. Must match [A-Z][A-Z0-9_]{{1,19}}."
        )


def _check_extra_filter(extra_filter: str) -> None:
    if _JQL_DANGEROUS.search(extra_filter):
        raise ValueError(
            f"Unsafe characters or keywords in extra_filter: {extra_filter!r}. "
            "Provide a single JQL field comparison clause."
        )


def build_jql(
    project_key: str,
    *,
    updated_after: str | datetime | None = None,
    extra_filter: str | None = None,
    tz: tzinfo | None = None,
) -> str:
    """Build a project-scoped JQL with optional `updated >=` clause + extra filter.

    `updated_after` accepts ISO-8601 strings or `datetime` instances; JQL only honors
    minute precision, so values are truncated to 16 chars.

    JIRA Server's JQL date parser interprets bare `"YYYY-MM-DD HH:MM"` strings in the
    JIRA server's local timezone — *not* UTC. If `tz` is given and `updated_after` is
    a tz-aware datetime, it is converted to `tz` before formatting so the filter means
    what the caller intended. Get JIRA's TZ via `JiraClient.server_tz`.

    >>> build_jql("PROJ")
    'project = "PROJ" ORDER BY updated ASC'
    >>> build_jql("PROJ", updated_after="2026-05-18T07:30:00")
    'project = "PROJ" AND updated >= "2026-05-18 07:30" ORDER BY updated ASC'
    """
    _check_project_key(project_key)
    base = f'project = "{project_key}"'
    if updated_after is not None:
        ts = _fmt_jql_ts(updated_after, tz)
        base += f' AND updated >= "{ts}"'
    if extra_filter:
        _check_extra_filter(extra_filter)
        base += f" AND {extra_filter}"
    return base + " ORDER BY updated ASC"


def _fmt_jql_ts(ts: str | datetime, tz: tzinfo | None) -> str:
    """Render a timestamp as JIRA Server expects (`YYYY-MM-DD HH:MM`, JIRA-local TZ)."""
    if isinstance(ts, datetime):
        return (ts.astimezone(tz) if tz and ts.tzinfo else ts).strftime("%Y-%m-%d %H:%M")
    return str(ts).replace("T", " ")[:16]


# The delta scan is built from two minute-scoped primitives rather than one
# `(updated, key)` JQL. JQL's `updated` *sort* is second-precision, but a bare minute
# date LITERAL is the INSTANT `MM:00` for every operator (verified on JIRA Server DC:
# `updated = "11:17"` matches only rows at exactly 11:17:00, not the whole minute). So
# the drain pins a minute with the half-open RANGE `updated >= "MM" AND updated < "MM+1"`
# (correct under either reading of `=`), and seeks within it on numeric `id` where the
# filter and sort agree exactly. Soundness lives in the drain-then-advance protocol
# (`JiraClient._search_by_updated`), not in any single JQL string — so these builders are
# deliberately NOT in `__all__`.


def build_delta_minute_jql(
    project_key: str,
    *,
    minute: datetime,
    after_id: int | None = None,
    extra_filter: str | None = None,
    tz: tzinfo | None = None,
) -> str:
    """JQL that pages one `updated` minute by issue `id` — the delta drain step.

    Bounds `updated` to one minute with the half-open range `>= "MM" AND < "MM+1"`
    (NOT `= "MM"`: JIRA reads a bare minute literal as the instant `MM:00`, so `=`
    would match only the `:00`-second rows). Within the minute the cursor is issue
    `id`, so the filter (`id > N`, numeric) and the sort (`ORDER BY id ASC`, numeric)
    agree exactly — a same-minute cluster of any size drains cleanly, one id-ascending
    page at a time, immune to the minute-precision / lexical-vs-numeric pitfalls a
    `(updated, key)` cursor hits.

    `minute` is rendered at minute precision (seconds dropped). Pass `tz`
    (typically `JiraClient.server_tz`) so a tz-aware `minute` is converted to the
    timezone JIRA Server parses bare JQL dates in.

    >>> from datetime import datetime, timezone
    >>> m = datetime(2026, 5, 18, 7, 30, tzinfo=timezone.utc)
    >>> build_delta_minute_jql("PROJ", minute=m, after_id=10042)
    'project = "PROJ" AND updated >= "2026-05-18 07:30" AND updated < "2026-05-18 07:31" AND id > 10042 ORDER BY id ASC'
    """
    _check_project_key(project_key)
    lo = _fmt_jql_ts(minute, tz)
    hi = _fmt_jql_ts(minute + timedelta(minutes=1), tz)
    clauses = [f'project = "{project_key}"', f'updated >= "{lo}"', f'updated < "{hi}"']
    if after_id is not None:
        clauses.append(f"id > {int(after_id)}")
    if extra_filter:
        _check_extra_filter(extra_filter)
        clauses.append(f"({extra_filter})")
    return " AND ".join(clauses) + " ORDER BY id ASC"


def build_next_minute_jql(
    project_key: str,
    *,
    after_minute: datetime,
    extra_filter: str | None = None,
    tz: tzinfo | None = None,
) -> str:
    """JQL probing for the first issue in a minute after `after_minute`.

    Advance step: once minute MM is fully drained, the next change is at or after the
    NEXT minute, so this filters `updated >= "MM+1"`. (Not `updated > "MM"`: JIRA reads
    that as `> MM:00`, which would re-include MM's own `:01`-`:59` rows.) The caller
    reads only the first row's `updated`.

    >>> from datetime import datetime, timezone
    >>> m = datetime(2026, 5, 18, 7, 30, tzinfo=timezone.utc)
    >>> build_next_minute_jql("PROJ", after_minute=m)
    'project = "PROJ" AND updated >= "2026-05-18 07:31" ORDER BY updated ASC, id ASC'
    """
    _check_project_key(project_key)
    ts = _fmt_jql_ts(after_minute + timedelta(minutes=1), tz)
    clauses = [f'project = "{project_key}"', f'updated >= "{ts}"']
    if extra_filter:
        _check_extra_filter(extra_filter)
        clauses.append(f"({extra_filter})")
    return " AND ".join(clauses) + " ORDER BY updated ASC, id ASC"
