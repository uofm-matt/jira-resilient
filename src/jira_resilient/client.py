"""JiraClient — the main entry point.

Holds the requests.Session + base URL + per-instance timeout config, and exposes
methods that are direct wrappers around JIRA Server REST endpoints. The seek
paginator and the three-tier resilient fetch are the load-bearing features that
distinguish this library from the generic JIRA wrappers on PyPI.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, tzinfo
from typing import Any, TypeGuard
from urllib.parse import quote

import requests

from jira_resilient._models import ResilientFetchResult, SearchPage, Tier
from jira_resilient.exceptions import JiraFetchError, JiraJqlError, JiraParseError
from jira_resilient.http import make_session, request_with_retry
from jira_resilient.jql import build_delta_minute_jql, build_next_minute_jql

logger = logging.getLogger(__name__)

_LIST_KEYS_PAGE_SIZE = 1000  # fields=key only — tiny payload, can ask for many
_SEARCH_SEEK_PAGE_SIZE = 20  # fields=*all + changelog — keep small to avoid timeouts
_SEARCH_PAGED_PAGE_SIZE = 50  # fields=*all without seek — older offset-paginated path


def _is_http_400(exc: BaseException) -> TypeGuard[requests.HTTPError]:
    return (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and exc.response.status_code == 400
    )


def _is_http_404(exc: BaseException) -> TypeGuard[requests.HTTPError]:
    return (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and exc.response.status_code == 404
    )


def _is_client_error(exc: BaseException) -> bool:
    """A 4xx other than 429 — the request is wrong (gone/forbidden/bad), so retrying the
    SAME key at a lower tier fails identically. 429 is excluded (rate-limit, retryable)."""
    return (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and 400 <= exc.response.status_code < 500
        and exc.response.status_code != 429
    )


def _jql_error_from(exc: requests.HTTPError, jql: str) -> JiraJqlError:
    """Preserve JIRA's `errorMessages` list verbatim for caller introspection."""
    messages: list[str] = []
    with contextlib.suppress(ValueError, AttributeError):
        messages = list(exc.response.json().get("errorMessages") or [])
    return JiraJqlError(f"JIRA rejected JQL: {jql!r}; messages={messages}", error_messages=messages)


# Excludes description, comments, attachments, custom fields — all huge on
# pathological issues. Kept to the JIRA core fields a data-warehouse loader needs.
_MINIMAL_FIELDS = (
    "summary,status,issuetype,priority,assignee,reporter,creator,"
    "labels,fixVersions,components,created,updated,resolutiondate,duedate,resolution"
)

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
        pool_maxsize: int = 10,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_attempts = max_attempts
        # pool_maxsize: raise above the default 10 when fanning concurrent requests through this
        # client (a thread pool of N parallel GETs needs pool_maxsize >= N, else urllib3 caps live
        # connections at 10 and churns the surplus — "Connection pool is full").
        self.session = make_session(pat, verify, pool_maxsize=pool_maxsize)
        self._server_tz: tzinfo | None = None
        # JIRA Cloud / some DC builds expose a paginated /issue/{key}/changelog
        # sub-resource; JIRA Server returns 404 for it. Probe once, then cache so we
        # use the inline ?expand=changelog route directly on Server (see get_changelog).
        self._changelog_paginated = True
        self._changelog_lock = threading.Lock()  # guards the one-way flip under fan-out

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
            try:
                name = resp.json().get("displayName", "unknown")
            except ValueError:
                # A 200 with a non-JSON body is an SSO/proxy login page, not an authed myself.
                logger.error("Auth check: HTTP 200 but a non-JSON body (SSO/proxy login page?)")
                return False
            logger.info("Auth OK: %s", name)
            return True
        logger.error("Auth failed: HTTP %d", resp.status_code)
        return False

    @property
    def server_tz(self) -> tzinfo:
        """JIRA server's local timezone, probed once from `/rest/api/2/serverInfo`.

        JQL date literals like `"2026-05-19 13:00"` are parsed in *this* timezone, not
        UTC. Pass to `build_jql` so a tz-aware cursor is rendered in the timezone JIRA
        expects. `search_seek` does this automatically.

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
            # UTC if the probe failed, or if serverTime parsed but was naive.
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
        """Full changelog as a flat history-entry list.

        Prefers the paginated `/issue/{key}/changelog` sub-resource (small per-page
        payloads stay under the timeout even for thousands of entries). That route is
        a JIRA Cloud / some-DC feature, though — JIRA **Server** returns 404 for it,
        in which case we fall back to the inline `?expand=changelog` route (the only
        one Server offers). The endpoint-missing result is cached on the client so we
        don't re-probe on every issue.

        A 404 is ambiguous: the ENDPOINT is absent (disable it for all issues) OR this
        ISSUE doesn't exist (re-raise, don't poison the route). We only disable the
        endpoint after confirming the issue itself exists.
        """
        if self._changelog_paginated:
            try:
                return self._changelog_paged(key, page_size)
            except requests.exceptions.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise
                if not self._issue_exists(key):
                    raise  # issue-404, not endpoint-404 — don't disable the route for everyone
                with self._changelog_lock:
                    self._changelog_paginated = False
                logger.info(
                    "paginated changelog endpoint not available (404) — using ?expand=changelog"
                )
        return self._changelog_expand(key)

    def _issue_exists(self, key: str) -> bool:
        """Cheap existence probe (`fields=key`) to disambiguate an endpoint-404 from an
        issue-404. Raises on non-404 errors (don't swallow a transient failure)."""
        try:
            self.get_issue_raw(key, fields="key", expand="", timeout=30, max_attempts=2)
        except requests.exceptions.HTTPError as exc:
            if _is_http_404(exc):
                return False
            raise
        return True

    def _changelog_paged(self, key: str, page_size: int) -> list[dict]:
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
            total = data.get("total")
            if not page or (total is not None and start_at >= total):
                break
        return histories

    def _changelog_expand(self, key: str) -> list[dict]:
        """JIRA Server changelog: the issue resource with `expand=changelog`. Returns
        the full history in one response (`changelog.histories`)."""
        data = request_with_retry(
            self.session,
            "GET",
            f"{self.base_url}/rest/api/2/issue/{key}",
            params={"expand": "changelog", "fields": "summary"},
            timeout=self.timeout,
            max_attempts=self.max_attempts,
        ).json()
        return data.get("changelog", {}).get("histories", [])

    def get_worklogs(self, key: str, *, page_size: int = 100) -> list[dict]:
        """Full worklog list via paginated `/rest/api/2/issue/{key}/worklog`.

        JIRA search responses include at most 20 worklogs inline. This method
        fetches the complete history for issues where the inline response was
        truncated (`fields.worklog.total > len(fields.worklog.worklogs)`).
        """
        url = f"{self.base_url}/rest/api/2/issue/{key}/worklog"
        worklogs: list[dict] = []
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
            page = data.get("worklogs") or []
            worklogs.extend(page)
            start_at += len(page)
            total = data.get("total")
            if not page or (total is not None and start_at >= total):
                break
        return worklogs

    def get_comments(self, key: str, *, page_size: int = 50) -> list[dict]:
        """Full comment list via paginated `/rest/api/2/issue/{key}/comment`.

        JIRA search responses cap inline comments (the exact limit is
        configurable per-instance via `jira.search.max.comments`, defaulting
        to 10 on older JIRA Server versions). Use when
        `fields.comment.total > len(fields.comment.comments)`.
        """
        url = f"{self.base_url}/rest/api/2/issue/{key}/comment"
        comments: list[dict] = []
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
            page = data.get("comments") or []
            comments.extend(page)
            start_at += len(page)
            total = data.get("total")
            if not page or (total is not None and start_at >= total):
                break
        return comments

    def get_remote_links(self, key: str) -> list[dict]:
        """All remote links for an issue via `/rest/api/2/issue/{key}/remotelink`.

        Remote links (Confluence pages, GitHub PRs, external URLs) are not
        included in search responses — they require this dedicated endpoint.
        JIRA returns all remote links in a single non-paginated response.

        Raises `JiraParseError` if a 200 body is a non-list (e.g. an SSO/proxy error
        envelope) rather than masking it as "no remote links".
        """
        url = f"{self.base_url}/rest/api/2/issue/{key}/remotelink"
        data = request_with_retry(self.session, "GET", url, timeout=30, max_attempts=3).json()
        if isinstance(data, list):
            return data
        if data:  # a non-list, non-empty 200 body is an error envelope, not data
            raise JiraParseError(f"remotelink for {key!r} returned a non-list body: {data!r}")
        return []

    def get_watchers(self, key: str) -> list[dict]:
        """Watcher identity list via `/rest/api/2/issue/{key}/watchers`.

        Returns the `watchers` array (full user objects: `name`, `key`,
        `emailAddress`, `displayName`, `active`, `timeZone`). The watcher *count*
        is already on the issue payload (`fields.watches.watchCount`); this
        endpoint is the only way to get the identities, and only if the caller
        has the "View Voters and Watchers" permission. Returns `[]` on a 404
        (sub-resource absent / no permission / issue gone). (`get_remote_links`
        does NOT do this — it raises on 404 — so don't assume parity.)
        """
        url = f"{self.base_url}/rest/api/2/issue/{key}/watchers"
        try:
            data = request_with_retry(self.session, "GET", url, timeout=30, max_attempts=3).json()
        except requests.exceptions.HTTPError as exc:
            if _is_http_404(exc):
                return []
            raise
        return data.get("watchers") or []

    def get_voters(self, key: str) -> list[dict]:
        """Voter identity list via `/rest/api/2/issue/{key}/votes`.

        Returns the `voters` array (user objects). As with watchers, the vote
        *count* is on the issue payload (`fields.votes.votes`); the voter
        identities require this endpoint and the "View Voters and Watchers"
        permission. Returns `[]` when the sub-resource is absent (404).
        """
        url = f"{self.base_url}/rest/api/2/issue/{key}/votes"
        try:
            data = request_with_retry(self.session, "GET", url, timeout=30, max_attempts=3).json()
        except requests.exceptions.HTTPError as exc:
            if _is_http_404(exc):
                return []
            raise
        return data.get("voters") or []

    def get_user(
        self,
        *,
        username: str | None = None,
        key: str | None = None,
        account_id: str | None = None,
        expand: str | None = "groups,applicationRoles",
    ) -> dict:
        """Single user via `/rest/api/2/user`, resolved by the right param for the deployment.

        JIRA **Server** identifies users by `username=` or `key=` (the opaque
        `JIRAUSER#####` form); JIRA **Cloud** uses `accountId=`. Pass exactly one
        of `username` / `key` / `account_id`. Returns the user object
        (`name`, `key`, `emailAddress`, `active`, `timeZone`, `locale`,
        `displayName`). `expand` defaults to `"groups,applicationRoles"` so the
        nested `groups.items` / `applicationRoles.items` lists are populated —
        without it Server returns those collections with `size` set but `items`
        empty. Pass `expand=None` to skip the expansion.

        Returns `{}` when the user does not exist (404).
        """
        params = {
            k: v
            for k, v in (
                ("username", username),
                ("key", key),
                ("accountId", account_id),
                ("expand", expand),
            )
            if v is not None
        }
        if sum(v is not None for v in (username, key, account_id)) != 1:
            raise ValueError("get_user requires exactly one of username, key, or account_id")
        url = f"{self.base_url}/rest/api/2/user"
        try:
            return request_with_retry(
                self.session, "GET", url, params=params, timeout=30, max_attempts=3
            ).json()
        except requests.exceptions.HTTPError as exc:
            if _is_http_404(exc):
                return {}
            raise

    def _fetch_properties(self, base_path: str) -> dict[str, Any]:
        """List then dereference every entity property under `base_path`.

        `GET {base_path}/properties` returns `{"keys": [{"key": ...}, ...]}`; each
        property's value lives at `{base_path}/properties/{propertyKey}` as
        `{"key": ..., "value": ...}`. (`?expand=properties` on the parent resource
        returns null on JIRA Server, so the dedicated sub-resource is the only way
        to read these.) Returns `{propertyKey: value}`. A 404 on the *list* — the
        sub-resource isn't supported on this build, or the parent entity is gone —
        yields `{}`; a 404 on an individual value (raced deletion) skips that key.
        """
        list_url = f"{self.base_url}{base_path}/properties"
        try:
            listing = request_with_retry(
                self.session, "GET", list_url, timeout=30, max_attempts=3
            ).json()
        except requests.exceptions.HTTPError as exc:
            if _is_http_404(exc):
                return {}
            raise
        result: dict[str, Any] = {}
        for entry in listing.get("keys") or []:
            prop_key = entry.get("key")
            if not prop_key:
                continue
            try:
                value = request_with_retry(
                    self.session,
                    "GET",
                    f"{list_url}/{quote(prop_key, safe='')}",
                    timeout=30,
                    max_attempts=3,
                ).json()
            except requests.exceptions.HTTPError as exc:
                if _is_http_404(exc):
                    continue
                raise
            result[prop_key] = value.get("value")
        return result

    def get_issue_properties(self, key: str) -> dict[str, Any]:
        """All entity properties for an issue, as `{propertyKey: value}`.

        Lists `/rest/api/2/issue/{key}/properties` then dereferences each value.
        Returns `{}` when the issue has no properties or the parent is missing.
        """
        return self._fetch_properties(f"/rest/api/2/issue/{key}")

    def get_comment_properties(self, issue_key: str, comment_id: str) -> dict[str, Any]:
        """All entity properties for a single comment, as `{propertyKey: value}`.

        Lists `/rest/api/2/issue/{key}/comment/{id}/properties` then dereferences
        each value. The comment-properties sub-resource is not exposed on every
        JIRA Server build (some return 404 for it wholesale) — that, an empty
        property set, and a missing comment all collapse to `{}`.
        """
        return self._fetch_properties(f"/rest/api/2/issue/{issue_key}/comment/{comment_id}")

    def get_project_properties(self, project_key: str) -> dict[str, Any]:
        """All entity properties for a project, as `{propertyKey: value}`.

        Lists `/rest/api/2/project/{key}/properties` then dereferences each value.
        Returns `{}` when the project has no properties or is missing.
        """
        return self._fetch_properties(f"/rest/api/2/project/{project_key}")

    def get_issue_resilient(self, key: str) -> ResilientFetchResult:
        """Three-tier resilient fetch.

        Tier 1: full `*all` (`expand=names,schema`; NOT changelog — fetch that
                separately via `get_changelog`, which paginates and stays under timeout).
        Tier 2: `*all,-issuelinks` + a separate `issuelinks` fetch (long timeout).
        Tier 3: minimal field set — description + custom_fields will be empty.

        A 4xx client error fails fast (lower tiers fetch the SAME key and would
        fail identically, so they are not attempted), but the exception TYPE
        depends on the status: a 404/410 (gone/deleted) raises `JiraFetchError`;
        a 401/403 (unauthorized/forbidden) raises `JiraAuthError` — it is raised
        by `request_with_retry` and propagates uncaught through the
        `requests.RequestException` handler (JiraAuthError is not a
        RequestException). A degradation to hub/minimal is logged as a warning
        (the result is partial: minimal drops description + custom_fields). On
        total failure with non-4xx errors raises `JiraFetchError` with the
        underlying exception chain.
        """
        fast_fail = {"timeout": 60, "max_attempts": 2}
        try:
            issue = self.get_issue_raw(key, expand="names,schema", **fast_fail)
            return ResilientFetchResult(issue, "full")
        except requests.RequestException as exc_full:
            if _is_client_error(exc_full):
                raise JiraFetchError(
                    f"fetch failed for {key!r} (client error, no tier retry): {exc_full!r}"
                ) from exc_full
            try:
                issue = self.get_issue_raw(
                    key,
                    expand="names,schema",
                    fields="*all,-issuelinks",
                    **fast_fail,
                )
                issue.setdefault("fields", {})["issuelinks"] = self.get_issuelinks(key)
                logger.warning("get_issue %s degraded to hub tier (%s)", key, exc_full)
                return ResilientFetchResult(issue, "hub")
            except requests.RequestException as exc_hub:
                if _is_client_error(exc_hub):
                    raise JiraFetchError(
                        f"fetch failed for {key!r} (client error, no tier retry): {exc_hub!r}"
                    ) from exc_hub
                try:
                    issue = self.get_issue_minimal(key)
                    logger.warning(
                        "get_issue %s degraded to MINIMAL tier (%s) — description + "
                        "custom_fields are empty in this result",
                        key,
                        exc_hub,
                    )
                    return ResilientFetchResult(issue, "minimal")
                except requests.RequestException as exc_min:
                    raise JiraFetchError(
                        f"all three fetch tiers failed for {key!r}: "
                        f"full={exc_full!r}; hub={exc_hub!r}; minimal={exc_min!r}"
                    ) from exc_min

    # ----- field catalog ------------------------------------------------------

    def list_fields(self) -> list[dict]:
        """`GET /rest/api/2/field`. Raises `requests.RequestException` on a failed request,
        or `JiraParseError` if a 200 body is not JSON (SSO/proxy HTML)."""
        url = f"{self.base_url}/rest/api/2/field"
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as exc:
            raise JiraParseError(f"list_fields: 200 but non-JSON body from {url}") from exc

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
            total = data.get("total")
            if not page or (total is not None and start_at >= total):
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
            total = data.get("total")
            if total is not None and start_at >= total:
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
        """Seek-paginated `/search`, dispatched by intent on `after_ts`.

        - **Full scan** (`after_ts is None`): page the whole project by issue
          `id` ascending (`_search_by_id`). `id` is a stable, exact, numeric,
          monotonic cursor — immune to the minute-precision, reindex divergence,
          and lexical-vs-numeric key pitfalls an `updated` cursor must work
          around. It cannot loop, so it needs none of that machinery.

        - **Delta** (`after_ts` set): page issues changed since `after_ts` one
          `updated` minute at a time, draining each minute by `id`
          (`_search_by_updated`). Both the within-minute filter and sort are on
          `id`, so the cursor is monotonic and self-consistent — it cannot skip a
          row or loop, even across a digit-width key boundary or a same-minute
          cluster larger than a page.

        `after_key`: **Deprecated and ignored since 0.5.0.** The delta cursor is
        now `(updated, id)` and its within-minute tiebreaker is derived from issue
        `id` internally, so no caller-supplied key is needed. Resumption across
        calls relies on `after_ts` plus the caller's overlap buffer and idempotent
        upserts. Accepted for backward compatibility; will be removed in a future
        release.

        Every request uses `startAt=0`, so per-request cost stays bounded
        regardless of project size — JIRA Server's offset-pagination quadratic
        cost is sidestepped in both modes.
        """
        del after_key  # deprecated no-op; see docstring
        if after_ts is None:
            yield from self._search_by_id(
                project_key, extra_filter=extra_filter, page_size=page_size
            )
        else:
            yield from self._search_by_updated(
                project_key,
                after_ts=after_ts,
                extra_filter=extra_filter,
                page_size=page_size,
            )

    def _search_by_id(
        self,
        project_key: str,
        *,
        extra_filter: str | None = None,
        page_size: int = _SEARCH_SEEK_PAGE_SIZE,
        after_id: int | None = None,
        fallback: bool = False,
    ) -> Iterator[SearchPage]:
        """Page the whole project by issue `id` ascending until id exhaustion.

        `id` uses one numeric ordering for both the `id > N` filter and the
        `ORDER BY id ASC` sort, so the cursor advances monotonically and the scan
        cannot loop — no cycle detection, minute-bump, or duplicate tracking
        needed. Used for full loads and as `_search_by_updated`'s post-reindex
        recovery (`fallback=True` tags those pages). Idempotent upserts on the
        caller side absorb any overlap with earlier pages.
        """
        while True:
            jql = f'project = "{project_key}"'
            if after_id is not None:
                jql += f" AND id > {after_id}"
            if extra_filter:
                jql += f" AND {extra_filter}"
            jql += " ORDER BY id ASC"
            data, tier = self._search_one_page(jql, page_size)
            issues = data.get("issues") or []
            if not issues:
                break
            yield SearchPage(
                issues,
                data.get("names") or {},
                data.get("schema") or {},
                tier=tier,
                fallback=fallback,
            )
            after_id = int(issues[-1]["id"])

    def _search_by_updated(
        self,
        project_key: str,
        *,
        after_ts: datetime,
        extra_filter: str | None,
        page_size: int,
    ) -> Iterator[SearchPage]:
        """Delta scan: issues changed since `after_ts`, one `updated` minute at a time.

        A bare minute date literal is the instant `MM:00` to JIRA, and `ORDER BY
        updated` is second-precision, so a single `(updated, key)` tuple-cursor query
        silently skips a row updated later in a minute than the page boundary but with
        a smaller key/id. The fix is to never paginate across a minute on `updated`:

        1. **Drain** the cursor minute with the half-open range
           `updated >= "MM" AND updated < "MM+1" AND id > after_id` ordered by `id`.
           The range captures the whole minute (a bare `= "MM"` would match only the
           `:00`-second rows); within it, filter and sort are both numeric `id`, so a
           same-minute cluster of any size pages cleanly, one id-ascending page at a
           time, with no skip and no looping (keys are never compared).
        2. **Advance** to the next minute that has changes via a cheap one-row probe
           (`_probe_next_minute`, `updated >= "MM+1"`), then drain that.

        Because `id` and the minute both advance monotonically, the scan terminates
        with no cycle/duplicate detection. The one residual hazard is a Lucene
        reindex, after which `fields.updated` can lag the index: the advance probe
        then returns a row whose `updated` is not actually past the cursor minute.
        That single signal triggers a fallback to a full `id` scan (`_search_by_id`,
        `fallback=True`), which is divergence-immune because it never reads `updated`.
        """
        if after_ts.tzinfo is None:
            after_ts = after_ts.replace(tzinfo=UTC)
        minute = after_ts.replace(second=0, microsecond=0)
        after_id: int | None = None
        while True:
            jql = build_delta_minute_jql(
                project_key,
                minute=minute,
                after_id=after_id,
                extra_filter=extra_filter,
                tz=self.server_tz,
            )
            data, tier = self._search_one_page(jql, page_size)
            issues = data.get("issues") or []
            if issues:
                yield SearchPage(
                    issues, data.get("names") or {}, data.get("schema") or {}, tier=tier
                )
                try:
                    after_id = int(issues[-1]["id"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise JiraParseError(
                        f"Delta page ended with row missing/invalid id: {issues[-1]!r}"
                    ) from exc
                if len(issues) >= page_size:
                    continue  # same minute, more ids to drain
            # Minute drained (empty or short page). Find the next minute with changes.
            nxt = self._probe_next_minute(
                project_key, after_minute=minute, extra_filter=extra_filter
            )
            if nxt is None:
                return
            if nxt <= minute:
                # The probe asked for `updated >= minute+1` yet returned a row whose
                # `updated` is not past `minute`: fields.updated lags the index, i.e. a
                # reindex. Fall back to an id scan, which ignores `updated` entirely.
                logger.warning(
                    "seek project=%s: next-minute probe returned %s <= cursor %s "
                    "(fields.updated lags index) — reindex detected; switching to id scan",
                    project_key,
                    nxt,
                    minute,
                )
                yield from self._search_by_id(
                    project_key, extra_filter=extra_filter, page_size=page_size, fallback=True
                )
                return
            minute, after_id = nxt, None

    def _probe_next_minute(
        self, project_key: str, *, after_minute: datetime, extra_filter: str | None
    ) -> datetime | None:
        """Floored `updated` minute of the first issue changed after `after_minute`.

        A one-row, `updated`-only `/search` — the cheap advance step of the delta
        drain. Returns `None` when nothing remains. Raises `JiraJqlError` on a 400
        (e.g. a malformed `extra_filter`); `JiraParseError` if the row lacks `updated`.
        """
        jql = build_next_minute_jql(
            project_key, after_minute=after_minute, extra_filter=extra_filter, tz=self.server_tz
        )
        url = f"{self.base_url}/rest/api/2/search"
        body = {"jql": jql, "startAt": 0, "maxResults": 1, "fields": ["updated"]}
        try:
            data = request_with_retry(
                self.session, "POST", url, json=body, timeout=60, max_attempts=2
            ).json()
        except requests.RequestException as exc:
            if _is_http_400(exc):
                raise _jql_error_from(exc, jql) from exc
            raise JiraFetchError(f"next-minute probe failed for jql={jql!r}: {exc!r}") from exc
        issues = data.get("issues") or []
        if not issues:
            return None
        updated = (issues[0].get("fields") or {}).get("updated")
        if not isinstance(updated, str):
            raise JiraParseError(f"next-minute probe row missing `updated`: {issues[0]!r}")
        try:
            return datetime.fromisoformat(updated).replace(second=0, microsecond=0)
        except ValueError as exc:
            raise JiraParseError(
                f"next-minute probe row has unparseable `updated`={updated!r}"
            ) from exc

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
          - `JiraJqlError` immediately on HTTP 400 — the QUERY is wrong (e.g. an
            `issuekey in (...)` batch naming a deleted/moved key, or an unknown
            field), so falling through to hub/minimal tiers (which use the same
            JQL) can't help. Caller decides whether to bisect the batch or retry.
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
                            "issuelinks fetch failed for %s in hub tier (%s); leaving "
                            "issuelinks ABSENT (not []) so callers can tell 'fetch failed' "
                            "from 'no links'",
                            key,
                            exc_links,
                        )
                        # Do NOT fabricate `issuelinks = []`: a `[]` reads as authoritative
                        # "no links" and would let a consumer overwrite real links. Absence
                        # is the per-issue degradation signal (the page tier is already "hub").
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
