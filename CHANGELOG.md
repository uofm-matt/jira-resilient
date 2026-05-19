# Changelog

All notable changes will be documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.2] — 2026-05-19

### Fixed
- **Infinite cycle in `search_seek` on busy minutes.** When the cursor landed on a
  minute that contained more issues than the page size, JIRA's JQL minute-precision
  semantics caused `updated > "X:Y"` to re-match every issue in the X:Y minute on
  every page. The seek paginator's `prev_boundary == current_boundary` check only
  caught 1-page cycles; 3-page cycles (production: keys rotating between three
  values within one minute) bypassed it and ran forever. Replaced with a deque of
  recent boundaries — any repeat within the last 10 pages now forces a
  minute-advance and clears `after_key`. Regression test added.
  Production symptom: a delta-sync loader fetched 11,000+ rows of a ~600-row gap
  without ever progressing past the boundary minute.

## [0.1.1] — 2026-05-19

### Fixed
- **JQL timezone bug.** JIRA Server's JQL parser interprets bare `"YYYY-MM-DD HH:MM"`
  date strings in the JIRA server's local timezone, **not** UTC. `build_jql` and
  `build_seek_jql` previously formatted datetimes in their source TZ (typically UTC),
  so JIRA Server silently shifted the filter by its local offset. For an
  a non-UTC JIRA, every delta sync's filter was shifted ~4 hours forward —
  effectively dropping every issue updated in the past 4 hours from each cron run.

### Added
- `build_jql` and `build_seek_jql` gained a `tz: tzinfo | None` parameter. When set
  on a tz-aware datetime, the value is converted to `tz` before strftime.
- `JiraClient.server_tz` lazily probes `/rest/api/2/serverInfo` and caches the
  returned offset. `JiraClient.search_seek` now auto-passes `server_tz` to
  `build_seek_jql`, so existing callers get the fix on upgrade with no code change.
- Regression tests covering both the conversion and the legacy (no-tz) path.

### Backward compatibility
- `tz=None` preserves the pre-fix behavior. Direct callers of `build_jql`/
  `build_seek_jql` who were already working around the bug (passing JIRA-local
  strings) are unaffected.

## [0.1.0] — 2026-05-18

### Added
- `JiraClient` — main entry point, class-based wrapper holding session + base URL + config.
- `JiraClient.search_seek(...)` — seek-paginated `/search`. Uses `(updated, key)` tuple
  cursor in JQL; every request uses `startAt=0`; per-request cost bounded regardless of
  project size. Survives projects with 100K+ issues where offset pagination dies.
- **Lucene-reindex resilience** in `search_seek`: monotonic `after_ts` floor so a JIRA
  server-side reindex (which stamps many issues with a future indexed-`updated`
  diverging from `fields.updated`) doesn't cause the seek cursor to regress and loop
  forever. Combined with `after_key` preservation in the JQL-minute-precision
  workaround so same-Lucene-timestamp groups still page correctly.
- `JiraClient.get_issue_resilient(...)` — three-tier resilient fetch: full `*all` →
  `*all,-issuelinks` with separate `issuelinks` fetch (long timeout) → minimal field
  set fallback. Handles "hub" issues with thousands of links that consistently time
  out the standard issue endpoint.
- `JiraClient.get_changelog(...)` — paginated changelog via `/issue/{key}/changelog`,
  the workaround for huge histories that overflow `expand=changelog`.
- `JiraClient.list_keys(jql)` — minimal `fields=key` retrieval. Tiny payload; never
  times out even on instance-wide queries.
- `JiraClient.list_fields()` — field definitions catalog.
- `JqlBuilder` (`build_jql`, `build_seek_jql`) — JQL composition with project-key
  validation and an extra-filter SQL-injection guard.
- Exception hierarchy: `JiraResilientError` → `JiraAuthError`, `JiraParseError`,
  `JiraFetchError`.
- HTTP foundation: TLS 1.2+ enforcement, bearer-PAT auth, retry-with-exponential-backoff
  on 429/5xx with explicit 4xx fail-fast.
