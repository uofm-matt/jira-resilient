"""JQL composition with project-key validation and an extra-filter injection guard.

These are pure functions — no network calls, no state. They live separately from
the JiraClient class so callers can compose JQL outside of any request flow.
"""
from __future__ import annotations

import re
from datetime import datetime

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
    updated_after: str | None = None,
    extra_filter:  str | None = None,
) -> str:
    """Build a project-scoped JQL with optional `updated >=` clause + extra filter.

    `updated_after` accepts ISO-8601-ish strings; JQL only honors minute precision,
    so the string is truncated to 16 chars and any `T` becomes a space.

    >>> build_jql("DMDHMSM")
    'project = "DMDHMSM" ORDER BY updated ASC'
    >>> build_jql("DMDHMSM", updated_after="2026-05-18T07:30:00")
    'project = "DMDHMSM" AND updated >= "2026-05-18 07:30" ORDER BY updated ASC'
    """
    _check_project_key(project_key)
    base = f'project = "{project_key}"'
    if updated_after:
        ts = updated_after.replace("T", " ")[:16]
        base += f' AND updated >= "{ts}"'
    if extra_filter:
        _check_extra_filter(extra_filter)
        base += f" AND {extra_filter}"
    return base + " ORDER BY updated ASC"


def build_seek_jql(
    project_key: str,
    *,
    after_ts:     datetime | str | None = None,
    after_key:    str | None = None,
    extra_filter: str | None = None,
) -> str:
    """Build a seek-pagination JQL using the `(updated, key)` tuple cursor.

    JIRA Server JQL has no native tuple comparison, so we rewrite the cursor as
    `(updated > X) OR (updated = X AND key > Y)` — strictly past the boundary
    OR tied on `updated` with a key past the tiebreaker.

    JQL date literals only accept minute precision; ties at the minute boundary
    are handled by the `key > after_key` tiebreaker and idempotent upserts
    downstream. See `JiraClient.search_seek` for the runtime handling.

    >>> from datetime import datetime, timezone
    >>> ts = datetime(2026, 5, 18, 7, 30, tzinfo=timezone.utc)
    >>> build_seek_jql("DMDHMSM", after_ts=ts, after_key="DMDHMSM-100")
    'project = "DMDHMSM" AND (updated > "2026-05-18 07:30" OR (updated = "2026-05-18 07:30" AND key > "DMDHMSM-100")) ORDER BY updated ASC, key ASC'
    """
    _check_project_key(project_key)
    base = f'project = "{project_key}"'
    if after_ts:
        if isinstance(after_ts, datetime):
            ts = after_ts.strftime("%Y-%m-%d %H:%M")
        else:
            ts = str(after_ts)[:16].replace("T", " ")
        if after_key:
            base += f' AND (updated > "{ts}" OR (updated = "{ts}" AND key > "{after_key}"))'
        else:
            base += f' AND updated >= "{ts}"'
    if extra_filter:
        _check_extra_filter(extra_filter)
        base += f" AND {extra_filter}"
    return base + " ORDER BY updated ASC, key ASC"
