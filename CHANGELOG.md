# Changelog

All notable changes will be documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.4.0] — 2026-06-04

### Added

Six new single-entity read methods on `JiraClient`, each mirroring the existing
`get_comments` / `get_worklogs` / `get_remote_links` style (retry-with-backoff,
JIRA-Server REST endpoints, graceful absence handling). All endpoint shapes were
verified live against the instance (JIRA Server x.y.z) before release.

- **`get_watchers(key) -> list[dict]`** — `GET /rest/api/2/issue/{key}/watchers`,
  returns the `watchers` array (full user objects). The watcher *count* is already
  on the issue payload (`fields.watches.watchCount`); this is the identity list,
  and requires the "View Voters and Watchers" permission. Returns `[]` on 404.
- **`get_voters(key) -> list[dict]`** — `GET /rest/api/2/issue/{key}/votes`,
  returns the `voters` array (user objects). Returns `[]` on 404.
- **`get_user(*, username=None, key=None, account_id=None, expand="groups,applicationRoles") -> dict`**
  — `GET /rest/api/2/user` resolved by the right param for the deployment
  (`username=` / `key=` on Server, `accountId=` on Cloud). Returns the user object
  (`emailAddress`, `active`, `timeZone`, `locale`, `displayName`). `expand` defaults
  to `groups,applicationRoles` because JIRA Server otherwise returns those
  collections with `size` set but `items` empty; pass `expand=None` to skip.
  Returns `{}` on 404 (unknown user).
- **`get_issue_properties(key) -> dict[str, Any]`** — lists
  `/rest/api/2/issue/{key}/properties` then dereferences each value, returning
  `{propertyKey: value}`. `?expand=properties` returns null on Server, so the
  dedicated sub-resource is required.
- **`get_comment_properties(issue_key, comment_id) -> dict[str, Any]`** — same
  pattern under `/issue/{key}/comment/{id}/properties`. Some JIRA Server builds
  do not expose this sub-resource at all (observed returning 404 wholesale on
  the instance's Service Desk projects) — that collapses to `{}` rather than raising.
- **`get_project_properties(project_key) -> dict[str, Any]`** — same pattern under
  `/project/{key}/properties`.

  Private helper `_fetch_properties(base_path)` backs all three property methods
  (list-then-dereference; 404 on the list → `{}`, 404 on an individual value →
  skip that key).

## [0.3.2] — 2026-06-03

### Fixed
- **`get_changelog` now works on JIRA Server.** It used the paginated
  `/rest/api/2/issue/{key}/changelog` sub-resource — a JIRA Cloud / some-DC endpoint
  that JIRA **Server** returns **404** for. The method now catches the 404 and falls
  back to the inline `?expand=changelog` route (the only one Server offers), caching
  the result per client so it doesn't re-probe the missing endpoint on every issue.
  Previously *every* `get_changelog` call against JIRA Server raised `HTTPError` and the
  changelog was silently dropped — it only surfaced when a hub/minimal-tier fallback
  forced the call (large projects with slow search). Non-404 errors still propagate.
  Affects all callers (delta sub-entity fetch + changelog backfill alike).

## [0.2.2] — 2026-05-20

### Fixed
- **`search_seek` auto-recovers from two more `after_key` failure modes**
  in addition to the deleted-issue case shipped in 0.2.1:
  - `"Operator '>' cannot be applied to moved issue key 'X'."` — fires
    when the cursor's last_seen_key was reprojected to another project.
    Hit live on a project (PROJ-1234) one hour after the 0.2.1 deploy.
  - `"The issue key 'X' for field 'key' is invalid."` — defensive: fires
    if cursor data is somehow malformed (no dash, wrong shape). Doesn't
    happen organically from the seek loop but recovers if it ever does.

  All three patterns now in `_STALE_KEY_PATTERNS`. Same recovery as before
  (clear after_key, retry); same idempotent-upsert assumption.

### Discovered
- Probed JIRA Server's full 400-error surface for cursor edge-cases. Six
  patterns total — three trigger the after_key auto-recovery above; the
  other three (deleted project, schema field gone, invalid date format)
  are different bug classes that aren't recoverable in this layer and
  propagate as `JiraJqlError` for caller diagnosis.

## [0.2.1] — 2026-05-20

### Fixed
- **`search_seek` auto-recovers from stale tiebreaker keys.** If a previous
  cycle's `after_key` references a JIRA issue that has since been deleted
  (or reprojected, or otherwise no longer exists), JIRA returns HTTP 400
  `"An issue with key 'X' does not exist for field 'key'."` for *every*
  subsequent call. The seek loop now detects this, clears `after_key`,
  and retries the same window without the tiebreaker. Idempotent upserts
  on the caller side absorb the slight widening.

  Production trigger: a JIRA admin deletion of a project's most-recent
  issue silently broke that project's delta sync, with every cycle
  failing identically until an operator manually NULL'd `last_seen_key`
  in sync_state. Now the loop self-heals on the next cycle.

### Changed
- **`_search_one_page` fast-fails on HTTP 400** instead of falling through
  to hub/minimal tiers. Tier 2 and 3 use the same JQL, so they will fail
  identically — 3 round trips when 1 would suffice. The new behavior is:
  - 400 → `JiraJqlError` immediately (with JIRA's `errorMessages` for
    caller introspection)
  - timeout / 5xx / other → tier ladder as before

### Added
- **`JiraJqlError`** — new exception type for "JIRA rejected the query."
  Carries `error_messages: list[str]` from JIRA's response body verbatim,
  so callers can pattern-match (e.g. `search_seek` matches the stale-key
  message to decide whether to auto-recover). Subclass of `JiraResilientError`.

## [0.2.0] — 2026-05-19

**Breaking change.** Inverted the default for single-issue fetches: the safe
path is now `get_issue`; raw access moved to `get_issue_raw`. The library
surface is now safe-by-default end-to-end — every JIRA read endpoint either
routes through the three-tier hub fallback (`get_issue`, `search_seek`,
`search_paged`) or is structurally immune to hub-issue timeouts (`list_keys`,
`get_changelog`, `get_issue_minimal`, `get_issuelinks`).

### Changed (breaking)
- **`get_issue(key)` is now resilient.** It routes through
  `get_issue_resilient` and returns the issue dict. Tier degradation is logged
  as a warning. The old `get_issue(key, *, expand, fields, timeout,
  max_attempts)` signature is now `get_issue_raw(...)`.
- **`get_issue_raw(...)`** is the explicit escape hatch: no fallback, direct
  pass-through to `request_with_retry`. Use when you need precise control over
  fields/expand/timeout — autoheal fast-fail probes, changelog-only fetches with
  a tight budget. `get_issue_resilient` uses this internally as its tier-1
  building block.

### Added
- **`search_paged` resilient tier.** Mirrors the 0.1.3 fix in `search_seek` —
  a single hub issue landing in an offset-paginated page no longer poisons the
  whole query. `SearchPage.tier` is populated by both paginators.
- Internal `_search_one_page` helper extracted so `search_seek` and
  `search_paged` share the same three-tier logic. Returns `(data, tier)` and
  accepts a `start_at` kwarg to support both pagination styles.

### Migration

```diff
- result = client.get_issue("PROJ-123", expand="changelog", timeout=60)
+ result = client.get_issue_raw("PROJ-123", expand="changelog", timeout=60)

- # If you only need the dict:
- issue = client.get_issue("PROJ-123")
+ # Unchanged — same call, but now resilient under the hood.
+ issue = client.get_issue("PROJ-123")

- # If you need tier info:
- result = client.get_issue_resilient("PROJ-123")  # unchanged
+ result = client.get_issue_resilient("PROJ-123")
```

The most common pattern (no expand/timeout overrides) is **source-compatible**;
only callers using the kwargs need to switch to `get_issue_raw`.

## [0.1.3] — 2026-05-19

### Added
- **Resilient search tier for `search_seek`.** Mirrors `get_issue_resilient`'s
  three-tier pattern at the listing layer: when a `/search` page POST times
  out (typically because one issue in the page is a hub with thousands of
  `issuelinks` ballooning the payload), the seek paginator now falls back to:
  - Tier 2 (hub): `fields=["*all","-issuelinks"]` on the search, then
    supplements `issuelinks` per-issue via `get_issuelinks(key)`. Loses the
    changelog expansion at this tier.
  - Tier 3 (minimal): a small fixed field set (no changelog, no issuelinks,
    no custom fields). Keeps the seek cursor advancing even when one page
    contains a truly pathological issue.
- `SearchPage` grew a `tier: Tier` field (default `"full"`) so callers can
  log the tier each page hit and observe degradation. Backwards-compatible
  — the existing positional `(issues, names, schema)` destructuring still works.

### Fixed
- **PROJ-class hub timeouts no longer block delta sync.** Before 0.1.3, a
  single hub issue with 5000+ `issuelinks` falling into the delta window
  would 120s-timeout every `/search` page that contained it — and on busy
  projects that's every page until the hub's `updated` rolled past. The
  loader would fail the whole project and (if chained behind another loader
  in systemd) block downstream loaders entirely. The resilient tier above
  fixes this in jira-resilient itself.

### Raises
- `JiraFetchError("all three search tiers failed for jql=...")` if even the
  minimal tier times out. Operators should treat this as JIRA-down, not a
  per-project hub issue. Existing `JiraParseError` for malformed-row pages
  is unchanged.

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
