"""JiraClient — the main entry point.

Holds the requests.Session + base URL + per-instance timeout config, and exposes
methods that are direct wrappers around JIRA Server REST endpoints. The seek
paginator and the three-tier resilient fetch are the load-bearing features that
distinguish this library from the generic JIRA wrappers on PyPI.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo

import requests

from jira_resilient._models import ResilientFetchResult, SearchPage, Tier
from jira_resilient.exceptions import JiraFetchError, JiraJqlError, JiraParseError
from jira_resilient.http import make_session, request_with_retry
from jira_resilient.jql import build_seek_jql

logger = logging.getLogger(__name__)

_LIST_KEYS_PAGE_SIZE = 1000  # fields=key only — tiny payload, can ask for many
_SEARCH_SEEK_PAGE_SIZE = 20  # fields=*all + changelog — keep small to avoid timeouts
_SEARCH_PAGED_PAGE_SIZE = 50  # fields=*all without seek — older offset-paginated path

# JIRA Server returns three distinct 400-error patterns when JQL `key > "X"`
# references a key the server can't compare against. ALL of them wedge a seek
# loop the same way (every subsequent cycle 400s forever until the cursor key
# is cleared), so all three trigger the same auto-recovery: drop after_key,
# retry. Patterns observed in prod 2026-05-20 (PROJ-1 deleted, PROJ-1234
# reprojected) plus the malformed-key variant added defensively.
_STALE_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Issue was deleted (or never existed).
    re.compile(r"An issue with key '([^']+)' does not exist for field 'key'"),
    # Issue was moved/reprojected to another project.
    re.compile(r"Operator '[^']+' cannot be applied to moved issue key '([^']+)'"),
    # Cursor data corrupted: not a valid issue-key shape (no dash, etc.).
    re.compile(r"The issue key '([^']*)' for field 'key' is invalid"),
)


def _is_stale_after_key_error(error_messages: list[str]) -> bool:
    """True iff any JIRA error message matches a known stale-after_key pattern."""
    return any(p.search(m) for m in error_messages for p in _STALE_KEY_PATTERNS)


def _is_http_400(exc: BaseException) -> bool:
    """True if exc is a requests.HTTPError carrying a 400 response."""
    return (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and exc.response.status_code == 400
    )


def _jql_error_from(exc: requests.HTTPError, jql: str) -> JiraJqlError:
    """Build a JiraJqlError from a 400 response, preserving JIRA's
    `errorMessages` list verbatim for caller introspection."""
    messages: list[str] = []
    try:
        body = exc.response.json() if exc.response is not None else {}
        messages = list(body.get("errorMessages") or [])
    except (ValueError, AttributeError):
        pass
    return JiraJqlError(f"JIRA rejected JQL: {jql!r}; messages={messages}", error_messages=messages)


# Default minimal field set for the "minimal" fallback tier. Covers the JIRA core fields
# most warehouse loaders care about; explicitly excludes description, comments,
# attachments, custom fields (all of which can be huge on pathological issues).
_MINIMAL_FIELDS = (
    "summary,status,issuetype,priority,assignee,reporter,creator,"
    "labels,fixVersions,components,created,updated,resolutiondate,duedate,resolution"
)

# Same field set, but as a list for the POST-body form used by /search.
_MINIMAL_FIELDS_LIST = _MINIMAL_FIELDS.split(",")


class JiraClient:
    """Resilient JIRA Server REST client with seek pagination and reindex-aware recovery.

    Example (illustrative only — see tests/ for live-network-free executable examples)::

        from jira_resilient import JiraClient

        client = JiraClient("https://jira.example.com", pat="...")
        if not client.is_authenticated:
            raise SystemExit("auth failed")
        for page in client.search_seek("PROJ"):
            for issue in page.issues:
                ...
    """

    def __init__(
        self,
        base_url: str,
        pat: str,
        *,
        verify: str | bool = True,
        timeout: int = 120,
        max_attempts: int = 5,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.session = make_session(pat, verify)
        self._server_tz: tzinfo | None = None

    # ----- auth ---------------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        """Probe `/rest/api/2/myself`. True iff HTTP 200. Logs the displayName on success."""
        try:
            resp = self.session.get(f"{self.base_url}/rest/api/2/myself", timeout=30)
        except requests.RequestException as exc:
            logger.error("Auth check error: %s", exc)
            return False
        if resp.status_code == 200:
            logger.info("Auth OK: %s", resp.json().get("displayName", "unknown"))
            return True
        logger.error("Auth failed: HTTP %d", resp.status_code)
        return False

    @property
    def server_tz(self) -> tzinfo:
        """JIRA server's local timezone, probed once from `/rest/api/2/serverInfo`.

        JQL date literals like `"2026-05-19 13:00"` are parsed in *this* timezone, not
        UTC. Pass to `build_jql`/`build_seek_jql` so a tz-aware cursor is rendered in
        the timezone JIRA expects. `search_seek` does this automatically.

        Falls back to UTC if `serverTime` can't be parsed — at worst that's the same
        broken behavior callers had pre-fix.
        """
        if self._server_tz is None:
            try:
                resp = self.session.get(f"{self.base_url}/rest/api/2/serverInfo", timeout=30)
                resp.raise_for_status()
                server_time = resp.json().get("serverTime")
                self._server_tz = datetime.fromisoformat(server_time).tzinfo
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                logger.warning("server_tz probe failed (%s) — falling back to UTC", exc)
                self._server_tz = datetime.now().astimezone().tzinfo  # local; harmless when None
            if self._server_tz is None:
                self._server_tz = UTC
        return self._server_tz

    # ----- single-issue fetches -----------------------------------------------

    def get_issue(self, key: str) -> dict:
        """Resilient single-issue fetch — the default safe path.

        Routes through `get_issue_resilient`'s three-tier fallback (full → hub
        → minimal) and returns just the issue dict. Tier degradation is logged
        as a warning. Use `get_issue_resilient` instead if you need to observe
        which tier each fetch landed on; use `get_issue_raw` if you need direct
        control over fields/expand/timeout (e.g. for fast-fail probes).

        Raises `JiraFetchError` only if all three tiers fail.
        """
        return self.get_issue_resilient(key).issue

    def get_issue_raw(
        self,
        key: str,
        *,
        expand: str = "changelog,names,schema",
        fields: str = "*all",
        timeout: int | None = None,
        max_attempts: int | None = None,
    ) -> dict:
        """Raw `GET /issue/{key}` with no fallback. Escape hatch for callers
        that need direct control — fast-fail probes (autoheal), changelog-only
        fetches with a tight budget, etc. Most callers should use `get_issue`
        instead, which routes through the resilient tier.

        Defaults to the full payload (`fields=*all` + changelog).
        """
        url = f"{self.base_url}/rest/api/2/issue/{key}"
        return request_with_retry(
            self.session,
            "GET",
            url,
            params={"fields": fields, "expand": expand},
            timeout=timeout or self.timeout,
            max_attempts=max_attempts or self.max_attempts,
        ).json()

    def get_issue_minimal(self, key: str, *, fields: str = _MINIMAL_FIELDS) -> dict:
        """Fallback fetch with a small field set + short timeout. No changelog."""
        url = f"{self.base_url}/rest/api/2/issue/{key}"
        return request_with_retry(
            self.session,
            "GET",
            url,
            params={"fields": fields, "expand": "names,schema"},
            timeout=60,
            max_attempts=3,
        ).json()

    def get_issuelinks(self, key: str, *, timeout: int = 600) -> list[dict]:
        """Fetch ONLY the `issuelinks` field. Long default timeout — hub issues with
        thousands of links can take minutes to serialize."""
        url = f"{self.base_url}/rest/api/2/issue/{key}"
        data = request_with_retry(
            self.session,
            "GET",
            url,
            params={"fields": "issuelinks"},
            timeout=timeout,
            max_attempts=2,
        ).json()
        return (data.get("fields") or {}).get("issuelinks") or []

    def get_changelog(self, key: str, *, page_size: int = 100) -> list[dict]:
        """Full changelog via the paginated endpoint (JIRA Server 8.6+).

        Returns the flat history-entry list. Small per-page payloads keep each
        request well under the 120s timeout even for issues with thousands of
        history entries — where the inline `expand=changelog` route would return
        everything in one shot and time out.
        """
        url = f"{self.base_url}/rest/api/2/issue/{key}/changelog"
        histories: list[dict] = []
        start_at = 0
        while True:
            data = request_with_retry(
                self.session,
                "GET",
                url,
                params={"startAt": start_at, "maxResults": page_size},
                timeout=60,
                max_attempts=3,
            ).json()
            page = data.get("values") or []
            histories.extend(page)
            start_at += len(page)
            if not page or start_at >= data.get("total", 0):
                break
        return histories

    def get_issue_resilient(self, key: str) -> ResilientFetchResult:
        """Three-tier resilient fetch.

        Tier 1: full `*all` + changelog (~60s, 2 attempts).
        Tier 2: `*all,-issuelinks` + a separate `issuelinks` fetch (long timeout).
        Tier 3: minimal field set — description + custom_fields will be empty.

        On total failure (all three tiers exhausted) raises `JiraFetchError`
        with the underlying exception chain.
        """
        fast_fail = {"timeout": 60, "max_attempts": 2}
        try:
            issue = self.get_issue_raw(key, expand="names,schema", **fast_fail)
            return ResilientFetchResult(issue, "full")
        except requests.RequestException as exc_full:
            try:
                issue = self.get_issue_raw(
                    key,
                    expand="names,schema",
                    fields="*all,-issuelinks",
                    **fast_fail,
                )
                issue.setdefault("fields", {})["issuelinks"] = self.get_issuelinks(key)
                return ResilientFetchResult(issue, "hub")
            except requests.RequestException as exc_hub:
                try:
                    issue = self.get_issue_minimal(key)
                    return ResilientFetchResult(issue, "minimal")
                except requests.RequestException as exc_min:
                    raise JiraFetchError(
                        f"all three fetch tiers failed for {key!r}: "
                        f"full={exc_full!r}; hub={exc_hub!r}; minimal={exc_min!r}"
                    ) from exc_min

    # ----- field catalog ------------------------------------------------------

    def list_fields(self) -> list[dict]:
        """`GET /rest/api/2/field`. Raises `requests.RequestException` on failure."""
        url = f"{self.base_url}/rest/api/2/field"
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # ----- listings -----------------------------------------------------------

    def list_keys(self, jql: str) -> list[str]:
        """All matching keys via `fields=key`. Tiny payload, never times out."""
        url = f"{self.base_url}/rest/api/2/search"
        keys: list[str] = []
        start_at = 0
        while True:
            body = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": _LIST_KEYS_PAGE_SIZE,
                "fields": ["key"],
            }
            data = request_with_retry(
                self.session,
                "POST",
                url,
                json=body,
                timeout=self.timeout,
                max_attempts=self.max_attempts,
            ).json()
            page = [i["key"] for i in data.get("issues", [])]
            keys.extend(page)
            start_at += len(page)
            if not page or start_at >= data.get("total", 0):
                break
        return keys

    # ----- pagination ---------------------------------------------------------

    def search_paged(
        self, jql: str, *, page_size: int = _SEARCH_PAGED_PAGE_SIZE
    ) -> Iterator[SearchPage]:
        """Offset-paginated `/search` yielding SearchPage per page (with changelog/names/schema).

        Lower-cost than `search_seek` for ad-hoc queries against bounded result sets.
        For project-wide enumeration use `search_seek` instead — JIRA Server's offset
        pagination materializes the full sort and skips startAt rows per request, so
        the cost grows quadratically with project size.

        Shares `_search_one_page` with `search_seek`, so the three-tier hub
        fallback applies here too — a single mega-hub issue landing in the page
        no longer poisons the entire query.
        """
        start_at = 0
        while True:
            data, tier = self._search_one_page(jql, page_size, start_at=start_at)
            issues = data.get("issues") or []
            if not issues:
                break
            yield SearchPage(issues, data.get("names") or {}, data.get("schema") or {}, tier=tier)
            start_at += len(issues)
            if start_at >= data.get("total", 0):
                break

    def search_seek(
        self,
        project_key: str,
        *,
        after_ts: datetime | None = None,
        after_key: str | None = None,
        extra_filter: str | None = None,
        page_size: int = _SEARCH_SEEK_PAGE_SIZE,
    ) -> Iterator[SearchPage]:
        """Seek-paginated `/search`. Every request uses `startAt=0`.

        Cursor: `(after_ts, after_key)`. Each page advances the cursor to the last issue
        returned. Per-request cost is bounded regardless of project size — JIRA Server's
        offset pagination quadratic cost is sidestepped entirely.

        Two non-obvious behaviors handled here:

        1. **JQL date minute-precision**: JQL `updated > "10:59"` actually matches anything
           from 10:59:00.001 onwards. If two consecutive pages share the same boundary
           row, the loop advances `after_ts` by one minute (keeping `after_key`) and
           tries again — see the same-Lucene-timestamp group handling below.

        2. **Lucene-reindex reconciliation**: when a JIRA server-side reindex stamps many
           issues with a future indexed-`updated`, `fields.updated` in the response still
           shows the original (older) timestamp. Setting `after_ts = fields.updated`
           directly would cause the JQL to broaden backward and re-match every reindexed
           issue from key=0 on the next request — an infinite loop. `after_ts` is kept
           monotonically non-decreasing (`max(after_ts, new_ts)`) so the search window
           can't regress. Combined with the minute-advance + `after_key` preservation
           above, a reindexed same-timestamp group is paged through cleanly by key.

        Raises `JiraParseError` if a returned issue is missing `fields.updated` or `key`.

        Self-heals from a stale `after_key` (JIRA-deleted between cycles) by
        clearing the tiebreaker on the next iteration when JIRA returns a
        400-with-"key 'X' does not exist". Without this the loop would 400
        forever — every cycle rebuilds the same broken JQL from the same
        stored cursor key.
        """
        # Last-N boundaries — detects longer cycles than the prev_boundary check.
        # 3-key cycles within a single minute were observed in production on busy
        # projects where JQL `updated > "X:Y"` (minute precision) re-matches every
        # issue in the X:Y minute regardless of after_key. A simple prev-vs-current
        # check misses these; a deque catches cycles of length up to maxlen-1.
        recent_boundaries: deque[tuple[datetime, str]] = deque(maxlen=10)
        while True:
            jql = build_seek_jql(
                project_key,
                after_ts=after_ts,
                after_key=after_key,
                extra_filter=extra_filter,
                tz=self.server_tz,
            )
            try:
                data, tier = self._search_one_page(jql, page_size)
            except JiraJqlError as exc:
                # Auto-recover from any "after_key references a key JIRA can't
                # compare against" error (deleted, moved, or malformed key).
                # Without after_key the JQL drops the `key > X` tiebreaker —
                # broader query, but valid. Idempotent upserts on the caller
                # side absorb any duplicates from the wider window.
                if after_key and _is_stale_after_key_error(exc.error_messages):
                    logger.warning(
                        "seek tiebreaker after_key=%s rejected by JIRA (%s); "
                        "clearing and retrying without it",
                        after_key,
                        exc.error_messages[0] if exc.error_messages else "?",
                    )
                    after_key = None
                    continue
                raise
            issues = data.get("issues") or []
            names = data.get("names") or {}
            schema = data.get("schema") or {}
            if not issues:
                break

            last = issues[-1]
            last_updated = (last.get("fields") or {}).get("updated")
            last_key = last.get("key")
            try:
                new_ts = (
                    datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                    if last_updated
                    else None
                )
            except (AttributeError, ValueError) as exc:
                raise JiraParseError(
                    f"Could not parse `updated` timestamp from JIRA: {last_updated!r}"
                ) from exc
            if new_ts is None or last_key is None:
                raise JiraParseError(f"Page ended with row missing updated/key: {last!r}")

            current_boundary = (new_ts, last_key)
            if current_boundary in recent_boundaries and after_ts is not None:
                # Cycle detected (1-cycle = boundary repeat, N-cycle = same boundary seen
                # within the last `maxlen` pages). Bump the minute past the current after_ts
                # and clear after_key — the next iteration starts a fresh window at the new
                # minute, no tiebreaker needed.
                after_ts = (after_ts + timedelta(minutes=1)).replace(second=0, microsecond=0)
                after_key = None
                recent_boundaries.clear()
                continue

            yield SearchPage(issues, names, schema, tier=tier)
            recent_boundaries.append(current_boundary)
            # Monotonic floor — see the docstring for why.
            after_ts = new_ts if after_ts is None else max(after_ts, new_ts)
            after_key = last_key

    def _search_one_page(self, jql: str, page_size: int, *, start_at: int = 0) -> tuple[dict, Tier]:
        """Three-tier `/search` request mirroring `get_issue_resilient`'s shape.

        Tier 1 (full):    `fields=["*all"]` + `expand=changelog,names,schema`.
        Tier 2 (hub):     `fields=["*all","-issuelinks"]` + `expand=names,schema`;
                          for each returned issue, fetch `issuelinks` separately
                          via `get_issuelinks(key)` and graft them back into
                          `fields.issuelinks`. Loses the changelog expansion at
                          this tier — callers needing per-issue history under
                          hub conditions should call `get_changelog(key)` on
                          the affected keys.
        Tier 3 (minimal): a small fixed field set (`_MINIMAL_FIELDS_LIST`); no
                          changelog, no issuelinks, no custom fields. Keeps the
                          cursor advancing even when one page contains a truly
                          pathological issue.

        Returns `(data, tier_used)` where `data` is the raw JIRA response dict
        (callers need `issues`, `names`, `schema`, and `total` from it). The
        `tier_used` lets seek/paged callers annotate their yielded SearchPage so
        operators can observe degradation in logs.

        `start_at` is used by offset-paginated callers (`search_paged`); seek
        callers always pass `0` (default).

        Raises:
          - `JiraJqlError` immediately on HTTP 400 — the QUERY is wrong (e.g.
            tiebreaker key no longer exists in JIRA), so falling through to
            hub/minimal tiers (which use the same JQL) can't help. Caller
            decides whether to clear cursor state and retry.
          - `JiraFetchError` only if all three tiers fail with non-400 errors.
        """
        url = f"{self.base_url}/rest/api/2/search"
        fast_fail = {"timeout": 60, "max_attempts": 2}
        base = {"jql": jql, "startAt": start_at, "maxResults": page_size}
        try:
            body = base | {"fields": ["*all"], "expand": ["changelog", "names", "schema"]}
            data = request_with_retry(self.session, "POST", url, json=body, **fast_fail).json()
            return data, "full"
        except requests.RequestException as exc_full:
            # 400 means JIRA rejected the JQL itself; hub/minimal use the same
            # JQL and will fail identically. Fast-fail with structured detail.
            if _is_http_400(exc_full):
                raise _jql_error_from(exc_full, jql) from exc_full
            logger.warning("search tier=full failed (%s); retrying without issuelinks", exc_full)
            try:
                body = base | {
                    "fields": ["*all", "-issuelinks"],
                    "expand": ["names", "schema"],
                }
                data = request_with_retry(self.session, "POST", url, json=body, **fast_fail).json()
                for issue in data.get("issues") or []:
                    key = issue.get("key")
                    if not key:
                        continue
                    try:
                        issue.setdefault("fields", {})["issuelinks"] = self.get_issuelinks(key)
                    except requests.RequestException as exc_links:
                        logger.warning(
                            "issuelinks fetch failed for %s in hub tier (%s); "
                            "leaving issuelinks empty for this issue",
                            key,
                            exc_links,
                        )
                        issue.setdefault("fields", {})["issuelinks"] = []
                return data, "hub"
            except requests.RequestException as exc_hub:
                logger.warning(
                    "search tier=hub failed (%s); falling back to minimal fields", exc_hub
                )
                try:
                    body = base | {
                        "fields": _MINIMAL_FIELDS_LIST,
                        "expand": ["names", "schema"],
                    }
                    data = request_with_retry(
                        self.session, "POST", url, json=body, **fast_fail
                    ).json()
                    return data, "minimal"
                except requests.RequestException as exc_min:
                    raise JiraFetchError(
                        f"all three search tiers failed for jql={jql!r}: "
                        f"full={exc_full!r}; hub={exc_hub!r}; minimal={exc_min!r}"
                    ) from exc_min
