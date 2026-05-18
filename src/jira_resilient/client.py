"""JiraClient — the main entry point.

Holds the requests.Session + base URL + per-instance timeout config, and exposes
methods that are direct wrappers around JIRA Server REST endpoints. The seek
paginator and the three-tier resilient fetch are the load-bearing features that
distinguish this library from the generic JIRA wrappers on PyPI.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterator

import requests

from jira_resilient._models import ResilientFetchResult, SearchPage
from jira_resilient.exceptions import JiraAuthError, JiraFetchError, JiraParseError
from jira_resilient.http import make_session, request_with_retry
from jira_resilient.jql import build_seek_jql

logger = logging.getLogger(__name__)

_LIST_KEYS_PAGE_SIZE   = 1000   # fields=key only — tiny payload, can ask for many
_SEARCH_SEEK_PAGE_SIZE = 20     # fields=*all + changelog — keep small to avoid timeouts
_SEARCH_PAGED_PAGE_SIZE = 50    # fields=*all without seek — older offset-paginated path

# Default minimal field set for the "minimal" fallback tier. Covers the JIRA core fields
# most warehouse loaders care about; explicitly excludes description, comments,
# attachments, custom fields (all of which can be huge on pathological issues).
_MINIMAL_FIELDS = (
    "summary,status,issuetype,priority,assignee,reporter,creator,"
    "labels,fixVersions,components,created,updated,resolutiondate,duedate,resolution"
)


class JiraClient:
    """Resilient JIRA Server REST client with seek pagination and reindex-aware recovery.

    Example:
        >>> from jira_resilient import JiraClient
        >>> client = JiraClient("https://jira.example.com", pat="...")
        >>> if not client.is_authenticated:
        ...     raise SystemExit("auth failed")
        >>> for page in client.search_seek("DMDHMSM"):
        ...     for issue in page.issues:
        ...         ...
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
        self.base_url     = base_url.rstrip("/")
        self.timeout      = timeout
        self.max_attempts = max_attempts
        self.session      = make_session(pat, verify)

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

    # ----- single-issue fetches -----------------------------------------------

    def get_issue(
        self,
        key: str,
        *,
        expand: str = "changelog,names,schema",
        fields: str = "*all",
        timeout: int | None = None,
        max_attempts: int | None = None,
    ) -> dict:
        """`GET /issue/{key}`. Defaults to the full payload (`fields=*all` + changelog)."""
        url = f"{self.base_url}/rest/api/2/issue/{key}"
        return request_with_retry(
            self.session, "GET", url,
            params={"fields": fields, "expand": expand},
            timeout=timeout or self.timeout,
            max_attempts=max_attempts or self.max_attempts,
        ).json()

    def get_issue_minimal(self, key: str, *, fields: str = _MINIMAL_FIELDS) -> dict:
        """Fallback fetch with a small field set + short timeout. No changelog."""
        url = f"{self.base_url}/rest/api/2/issue/{key}"
        return request_with_retry(
            self.session, "GET", url,
            params={"fields": fields, "expand": "names,schema"},
            timeout=60, max_attempts=3,
        ).json()

    def get_issuelinks(self, key: str, *, timeout: int = 600) -> list[dict]:
        """Fetch ONLY the `issuelinks` field. Long default timeout — hub issues with
        thousands of links can take minutes to serialize."""
        url = f"{self.base_url}/rest/api/2/issue/{key}"
        data = request_with_retry(
            self.session, "GET", url,
            params={"fields": "issuelinks"},
            timeout=timeout, max_attempts=2,
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
                self.session, "GET", url,
                params={"startAt": start_at, "maxResults": page_size},
                timeout=60, max_attempts=3,
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
            issue = self.get_issue(key, expand="names,schema", **fast_fail)
            return ResilientFetchResult(issue, "full")
        except requests.RequestException as exc_full:
            try:
                issue = self.get_issue(
                    key, expand="names,schema", fields="*all,-issuelinks", **fast_fail,
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
            data = request_with_retry(self.session, "POST", url, json=body,
                                      timeout=self.timeout, max_attempts=self.max_attempts).json()
            page = [i["key"] for i in data.get("issues", [])]
            keys.extend(page)
            start_at += len(page)
            if not page or start_at >= data.get("total", 0):
                break
        return keys

    # ----- pagination ---------------------------------------------------------

    def search_paged(self, jql: str, *, page_size: int = _SEARCH_PAGED_PAGE_SIZE) -> Iterator[SearchPage]:
        """Offset-paginated `/search` yielding SearchPage per page (with changelog/names/schema).

        Lower-cost than `search_seek` for ad-hoc queries against bounded result sets.
        For project-wide enumeration use `search_seek` instead — JIRA Server's offset
        pagination materializes the full sort and skips startAt rows per request, so
        the cost grows quadratically with project size.
        """
        url = f"{self.base_url}/rest/api/2/search"
        start_at = 0
        while True:
            body = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": page_size,
                "fields": ["*all"],
                "expand": ["changelog", "names", "schema"],
            }
            data = request_with_retry(self.session, "POST", url, json=body,
                                      timeout=self.timeout, max_attempts=self.max_attempts).json()
            issues = data.get("issues", [])
            if not issues:
                break
            yield SearchPage(issues, data.get("names") or {}, data.get("schema") or {})
            start_at += len(issues)
            if start_at >= data.get("total", 0):
                break

    def search_seek(
        self,
        project_key: str,
        *,
        after_ts:     datetime | None = None,
        after_key:    str | None = None,
        extra_filter: str | None = None,
        page_size:    int = _SEARCH_SEEK_PAGE_SIZE,
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
        """
        url = f"{self.base_url}/rest/api/2/search"
        prev_boundary: tuple[datetime, str] | None = None
        while True:
            jql = build_seek_jql(project_key, after_ts=after_ts, after_key=after_key,
                                 extra_filter=extra_filter)
            body = {
                "jql": jql,
                "startAt": 0,
                "maxResults": page_size,
                "fields": ["*all"],
                "expand": ["changelog", "names", "schema"],
            }
            data = request_with_retry(self.session, "POST", url, json=body,
                                      timeout=self.timeout, max_attempts=self.max_attempts).json()
            issues = data.get("issues") or []
            if not issues:
                break

            last = issues[-1]
            last_updated = (last.get("fields") or {}).get("updated")
            last_key     = last.get("key")
            try:
                new_ts = (datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                          if last_updated else None)
            except (AttributeError, ValueError) as exc:
                raise JiraParseError(
                    f"Could not parse `updated` timestamp from JIRA: {last_updated!r}"
                ) from exc
            if new_ts is None or last_key is None:
                raise JiraParseError(f"Page ended with row missing updated/key: {last!r}")

            current_boundary = (new_ts, last_key)
            if prev_boundary == current_boundary and after_ts is not None:
                # Boundary repeat → either JQL minute-precision matched the boundary row
                # twice, or a Lucene reindex is stamping many issues at one indexed-ts.
                # Bump the minute; keep after_key so once after_ts reaches the indexed-ts
                # the (updated = X AND key > Y) tiebreaker resumes paging through the
                # same-timestamp group.
                after_ts = (after_ts + timedelta(minutes=1)).replace(second=0, microsecond=0)
                continue

            yield SearchPage(issues, data.get("names") or {}, data.get("schema") or {})
            prev_boundary = current_boundary
            # Monotonic floor — see the docstring for why.
            after_ts  = new_ts if after_ts is None else max(after_ts, new_ts)
            after_key = last_key
