# Changelog

All notable changes will be documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
