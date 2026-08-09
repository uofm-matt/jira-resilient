"""JQL composition with project-key validation and an extra-filter injection guard.

These are pure functions — no network calls, no state. They live separately from
the JiraClient class so callers can compose JQL outside of any request flow.

Two public shapes, and the distinction is the point. `project_clause` returns a WHERE
fragment for callers assembling a query of their own; `build_jql` returns that fragment
plus `ORDER BY updated ASC`, i.e. something runnable. A trailing `ORDER BY` is the marker
throughout this module: if a string has one it is a query, if it does not it is a piece.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, tzinfo

from jira_resilient.exceptions import JiraQueryValidationError

# JIRA project keys: ASCII letter followed by 1-19 alphanumeric/underscore chars.
_SAFE_PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,19}$")

# Tokens that must not appear in a trusted-operator extra_filter clause.
# Note: this is a defensive guard against accidental SQL-style injection in a JQL
# string; it is NOT a security boundary. Callers must still treat extra_filter as
# operator-supplied, not user-supplied.
_JQL_COMMENT = re.compile(r";\s*|--(?!.*\bORDER\b)|/\*|\*/")
_JQL_DDL_WORD = re.compile(r"\b(UNION|DROP|DELETE|INSERT|UPDATE|EXEC|EXECUTE)\b", re.IGNORECASE)
_QUOTED = re.compile(r'"[^"]*"')


def _check_project_key(project_key: str) -> None:
    if not _SAFE_PROJECT_KEY.match(project_key):
        raise JiraQueryValidationError(
            f"Invalid project key: {project_key!r}. Must match [A-Z][A-Z0-9_]{{1,19}}."
        )


def _check_extra_filter(extra_filter: str) -> None:
    # The DDL-word arm runs against the filter with quoted literals blanked, so ordinary
    # data — status = "Update Required", component = "Union Pay" — is not mistaken for a
    # keyword. The comment arm deliberately runs against the ORIGINAL: its `--` rule carries
    # a `(?!.*\bORDER\b)` exemption, and blanking a literal containing ORDER would delete the
    # very token the exemption keys on, turning this check from looser into stricter.
    if _JQL_COMMENT.search(extra_filter) or _JQL_DDL_WORD.search(_QUOTED.sub('""', extra_filter)):
        raise JiraQueryValidationError(
            f"Unsafe characters or keywords in extra_filter: {extra_filter!r}. "
            "Provide a single JQL field comparison clause."
        )


def project_clause(project_key: str, extra_filter: str | None = None) -> str:
    """Scope a filter to one project. Returns a WHERE fragment — no `ORDER BY`, not a query.

    This is the composition primitive. Reach for it whenever you would otherwise write
    `f'project = "{key}" AND {filter}'`: that spelling drops both validators and, worse,
    leaves a top-level `OR` unparenthesized. JQL binds `AND` tighter than `OR`, so
    `project = "P" AND a OR b` parses as `(project = "P" AND a) OR b` and returns rows from
    EVERY project. Two separate downstream files hand-rolled that exact string and shipped
    that exact defect, which is why this function is exported and the cursor builders below
    are not — theirs is one page of a paging protocol whose correctness lives in the caller,
    while this one's whole contract is local and visible in its own return value.

    Conjoin the result with `AND`, or append an `ORDER BY` of your own. Do NOT append
    ` OR ...`: that unbinds the project clause again. Put the `OR` inside `extra_filter`,
    where it gets parenthesized.

    Deliberately unordered. Ordering belongs to whoever runs the query, and an offset-paged
    scan needs a deterministic one (`search_seek` supplies its own — prefer it over paging
    this by hand).

    >>> project_clause("PROJ")
    'project = "PROJ"'
    >>> project_clause("PROJ", 'status = "Done" OR labels = "nightly"')
    'project = "PROJ" AND (status = "Done" OR labels = "nightly")'
    >>> project_clause("PROJ", None) == project_clause("PROJ", "")
    True
    >>> f = 'status = "Done"'
    >>> build_jql("PROJ", extra_filter=f) == f'{project_clause("PROJ", f)} ORDER BY updated ASC'
    True
    """
    _check_project_key(project_key)
    if not extra_filter:
        return f'project = "{project_key}"'
    _check_extra_filter(extra_filter)
    return f'project = "{project_key}" AND ({extra_filter})'


def build_jql(
    project_key: str,
    *,
    updated_after: str | datetime | None = None,
    extra_filter: str | None = None,
    tz: tzinfo | None = None,
) -> str:
    """Build a project-scoped JQL with optional `updated >=` clause + extra filter.

    A complete, runnable query: the trailing `ORDER BY updated ASC` is part of what this
    returns. If you want the WHERE half alone — to conjoin into a query of your own, or to
    give it an ordering this one does not — use `project_clause`, which is exactly this
    minus the sort and minus the `updated` cursor.

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
    >>> build_jql("PROJ", extra_filter='status = "Done" OR labels = "nightly"')
    'project = "PROJ" AND (status = "Done" OR labels = "nightly") ORDER BY updated ASC'
    """
    _check_project_key(project_key)
    base = f'project = "{project_key}"'
    if updated_after is not None:
        ts = _fmt_jql_ts(updated_after, tz)
        base += f' AND updated >= "{ts}"'
    if extra_filter:
        _check_extra_filter(extra_filter)
        base += f" AND ({extra_filter})"
    return base + " ORDER BY updated ASC"


def _fmt_jql_ts(ts: str | datetime, tz: tzinfo | None) -> str:
    """Render a timestamp as JIRA Server expects (`YYYY-MM-DD HH:MM`, JIRA-local TZ)."""
    if isinstance(ts, datetime):
        return (ts.astimezone(tz) if tz and ts.tzinfo else ts).strftime("%Y-%m-%d %H:%M")
    return str(ts).replace("T", " ")[:16]


# The three builders below are CURSOR PRIMITIVES: each emits the query for ONE page of a
# scan, and the scan's correctness lives in the paging protocol that calls them
# (`JiraClient._search_by_id` / `._search_by_updated`), not in any single JQL string. Called
# standalone each returns a plausible-looking first page, which is exactly why none of them
# is in `__all__` — exporting one invites a caller to use it as a whole query. `project_clause`
# is exported under the same rule, not despite it: it carries no cursor and no protocol, so
# there is no second half for a caller to be missing.
#
# The delta pair is minute-scoped rather than one `(updated, key)` JQL because JQL's
# `updated` *sort* is second-precision while a bare minute date LITERAL is the INSTANT
# `MM:00` for every operator (verified on JIRA Server DC: `updated = "11:17"` matches only
# rows at exactly 11:17:00, not the whole minute). So the drain pins a minute with the
# half-open RANGE `updated >= "MM" AND updated < "MM+1"` (correct under either reading of
# `=`), and seeks within it on numeric `id` where the filter and sort agree exactly.


def build_full_scan_jql(
    project_key: str,
    *,
    after_id: int | None = None,
    extra_filter: str | None = None,
) -> str:
    """JQL that pages a whole project by issue `id` ascending — the full-scan step.

    Both the cursor filter (`id > N`) and the sort (`ORDER BY id ASC`) are numeric and
    agree exactly, so the scan advances monotonically and cannot loop.

    `extra_filter` is parenthesized, which is load-bearing rather than cosmetic: JQL binds
    `AND` tighter than `OR`, so a bare `AND status = x OR labels = y` would parse as
    `(project = ... AND status = x) OR (labels = y)` and match rows in EVERY project. That
    also unbinds `id > N`, pinning the cursor and hanging the scan.

    >>> build_full_scan_jql("PROJ", after_id=10042)
    'project = "PROJ" AND id > 10042 ORDER BY id ASC'
    """
    _check_project_key(project_key)
    clauses = [f'project = "{project_key}"']
    if after_id is not None:
        clauses.append(f"id > {int(after_id)}")
    if extra_filter:
        _check_extra_filter(extra_filter)
        clauses.append(f"({extra_filter})")
    return " AND ".join(clauses) + " ORDER BY id ASC"


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
