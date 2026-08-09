# Changelog

All notable changes will be documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.8.0] — 2026-08-08

Everything here came from running the library against a real 170,000-issue JIRA Server for the
first time. Four defects that desk design had missed.

### Added

- **`project_clause(project_key, extra_filter)`**, exported. The composition primitive that was
  missing: it validates both arguments and parenthesizes the filter, so `AND a OR b` cannot
  unbind the project scope. The cursor builders stay unexported — their correctness lives in the
  paging protocol that calls them — but this one's whole contract is its return value.
  It exists because two separate downstream files hand-rolled
  `f'project = "{key}" AND {filter}'` and shipped exactly the defect 0.6.0 had just fixed. The
  library held the fix and made it unreachable; that was an encapsulation failure, not theirs.

- **`probe` now reports progress while it runs.** A heartbeat on stderr with elapsed time and the
  last completed rung, suppressed when stderr is not a terminal. Previously it printed nothing
  for 11 minutes on a real hub issue — a diagnostic about slow requests giving no way to tell
  working from hung.

- **`fast_fail_timeout` on `JiraClient`** (default 60). The tier budget is now configurable and
  the constructor's `timeout` clamps it.

### Fixed

- **The adapter no longer retries read timeouts.** `Retry(total=3)` applied to every request, so
  each app-level attempt was really four HTTP attempts and the "fast-fail" tier-1 budget cost
  **504 seconds** against production, not the 120 its parameters imply. A connection failure is
  transient and still retries; a read timeout on a 13.6 MB payload is deterministic and now
  costs one attempt. Measured: the tier-1 budget drops from ~504s to ~130s.

- **The tier budget is reachable at all.** Both ladders hardcoded 60s, so it could not be
  changed by any caller. It is now `fast_fail_timeout` (default 60), and the constructor's
  `timeout` **clamps** it — `timeout=30` gives the ladder 30s. Note that `timeout` does not
  RAISE it: `timeout=300` still leaves tier 1 at 60s, because failing fast is what lets tier 2
  run. To give tier 1 longer, set `fast_fail_timeout` explicitly.

- **`is_authenticated` and `server_tz` retry again.** They call the session directly, so the
  adapter was their only retry layer; removing read retries would have left them with none. One
  transient reset made `server_tz` cache UTC **for the life of the client**, rendering every
  delta JQL literal at the wrong offset, silently. Both now use `request_with_retry`, which also
  gives them its redirect refusal.

### Documentation

- The README's payload and timing claims were estimates ("~10 MB", "3+ minutes"). They are now
  measured: **13.6 MB and 206 seconds**, with the full cost-per-link curve and the point where
  serialization stops being linear. The subtitle no longer says hub issues "consistently" time
  out — on the instance measured, two issues in 170,000 did, and the honest number is more
  useful than the loud one.

## [0.7.0] — 2026-08-07

### Added

- **A read-only diagnostic CLI**, installed as `jira-resilient`. Two commands, deliberately.

  `jira-resilient probe ISSUE-KEY` runs the three-tier ladder against one of your own issues
  and prints what each attempt cost:

  ```text
  Full request:        Timeout after 60.0s
  Hub base request:    success in 2.1s
  Links-only request:  success in 181.4s
  Tier:   hub
  Links:  4,832
  ```

  This answers the only question that gates using this library — *do I have the hub problem?*
  — against your own data rather than in prose. A `Tier: full` in under a second is an honest
  "you do not need this package", which is why the command exists.

  `jira-resilient scan PROJECT --limit N` is the same question over a project: tier
  distribution, a loud warning if any page came back lossy, and whether the post-reindex
  recovery scan fired.

  The PAT is read from `JIRA_PAT` only, never an argument — a token in argv lands in shell
  history and the process table. Nothing in the CLI writes to JIRA.

- **Per-attempt timings on the resilient fetch.** `get_issue_resilient` now emits one
  structured log record per rung of the ladder (`jr_event="attempt"` with `jr_step`,
  `jr_elapsed`, `jr_error`). Previously the winning tier was on `ResilientFetchResult` and
  what it cost to get there was unrecoverable — an operator could see `hub` and not that the
  full attempt burned 60s first. Consumers read record attributes rather than parsing
  messages, so the human-readable wording stays free to change.

## [0.6.2] — 2026-08-07

Supersedes 0.6.1, which was never published. Everything below is relative to **0.6.0**.

### Added

- **Python 3.11 is supported and tested.** The previous `>=3.12` floor was arbitrary — the
  source contains no 3.12-only syntax. 3.11 is the real floor: `datetime.fromisoformat` was
  rewritten there to accept general ISO-8601, and 3.10 rejects every timestamp shape JIRA
  Server emits (`…+0000`, `…-0600`, `…Z`). On 3.10, `server_tz` would silently fall back to
  UTC on every non-UTC server, so support stops at 3.11 rather than shipping a shim.

### Fixed

- **`list_fields` now uses the same HTTP policy as every other read.** It called
  `session.get` directly, so a 401 raised raw `requests.HTTPError` instead of
  `JiraAuthError`, a 503 got one attempt with no retry, and a redirect was followed —
  including a cross-host SSO 302, whose login-page JSON was returned *as the field catalog*.
- **A 2xx body that is not a JSON object no longer escapes the exception hierarchy.** An
  SSO/proxy page or an error envelope decodes to a list, string or number, and reading a key
  from it raised `AttributeError` — neither a `JiraResilientError` nor a
  `requests.RequestException`, so it escaped both boundaries the README tells callers to
  catch. Now `JiraParseError` on the paths that propagate, and the documented fallback on the
  two probes that swallow.
- **The `/myself` and `/serverInfo` probes no longer follow redirects.** `is_authenticated`
  returned **True** against an SSO login page, and `server_tz` adopted the *login host's* UTC
  offset — which is rendered into every delta JQL literal, shifting the minute window the
  scan drains. 0.6.1 fixed this for `list_fields`; these were the two remaining call sites.
- **A malformed row now fails the same way in both `search_seek` branches.** The delta path
  has raised `JiraParseError` since 0.5.0; the full scan — the default branch — let a bare
  `KeyError`, `ValueError` or `TypeError` escape.

### Changed

- `list_fields` raises `JiraParseError` when a 200 body is not a JSON list rather than
  returning it, and any 3xx is surfaced rather than followed — including a benign 301 from
  context-path normalisation that previously worked.
- `list_keys` returns `[]` for the malformed body `{"issues": null}` where it previously
  raised `TypeError`. No JIRA build emits that shape, but this is a loud failure becoming a
  quiet one and is recorded as such rather than as tolerance.
- `list_keys` still raises a bare `KeyError` if `/search` returns a row without `key`.
  Deferred rather than overlooked.

### Internal

- The `startAt`/`total` paging loop is written once instead of four times, verified unchanged
  by a request-level differential rather than by the tests passing.
- **The test suite now pins behaviour, not just execution.** A mutation battery found 35 of
  107 mutants surviving a green run at 99% coverage — a 67% mutation score. Among them: the
  hub tier could be made to re-request `*all`, killing the feature this library exists for,
  with every test still green; any exception could be severed from `JiraResilientError`; the
  delta cursor could silently drop rows; and the JQL oracle guarding the 0.6.0 scope fix had
  no oracle of its own, so the real defect plus an inverted precedence in the model cancelled
  out. 62 tests added, 194 → 256, coverage unchanged at 99% — the point being that coverage
  could not see any of it.

## [0.6.1] — 2026-08-07 [unreleased]

Cleanup and contract-fidelity release. No change to the seek paginator or the tiered fetch.

### Added

- **Python 3.11 is supported and tested.** The previous `>=3.12` floor was arbitrary — the
  source contains no 3.12-only syntax. 3.11 is the real floor, and the binding constraint is
  `datetime.fromisoformat`, not `datetime.UTC`: 3.11 rewrote it to accept general ISO-8601, and
  3.10 rejects every timestamp shape JIRA Server emits (`...+0000`, `...-0600`, `...Z`). 3.10
  would need a hand-rolled parser, and without one `server_tz` silently falls back to UTC on
  every non-UTC server — so support stops at 3.11.

### Fixed

- **`list_fields` now uses the same HTTP policy as every other read.** It called
  `session.get` directly, so a 401 raised raw `requests.HTTPError` instead of `JiraAuthError`,
  a 503 got one attempt with no retry, and a redirect was followed — including a cross-host
  SSO 302, whose login-page JSON was then returned *as the field catalog*. A caller wrapping a
  run in `except JiraResilientError` did not catch an auth failure here.
- **A malformed row now fails the same way in both `search_seek` branches.** The delta path has
  raised `JiraParseError` since 0.5.0; the full scan — the default branch — let a bare
  `KeyError`, `ValueError`, or `TypeError` escape, none of which is a `JiraResilientError`.

### Changed

- `list_fields` raises `JiraParseError` when a 200 body is not a JSON list, rather than
  returning it. Any 3xx is now surfaced rather than followed, including a benign 301 from
  context-path normalization that previously worked.
- `list_keys` returns `[]` for the malformed body `{"issues": null}` where it previously raised
  `TypeError`. No JIRA build emits that shape, but this is a loud failure becoming a quiet one
  and is recorded as such rather than as tolerance.
- `list_keys` still raises a bare `KeyError` if `/search` returns a row without `key`. Same
  class as the cursor fix above; deferred rather than overlooked.

### Internal

- The `startAt`/`total` paging loop is now written once instead of four times. Behavior verified
  unchanged by a request-level differential (4 methods × 18 scenarios × 2 page sizes) rather
  than by the tests passing — a mutation audit found three defects the four-copy version could
  not detect at all, including one that ships GETs with a JSON body and no query string.
- The error taxonomy's failure branches have oracles: all four `_probe_next_minute` paths and
  every non-404 re-raise. Coverage 92% → 99%, floors ratcheted to match.
- Documentation truth pass. `pool_maxsize` documented (missing since 0.4.4), thread safety
  scoped precisely, `server_tz` documented as caching a fixed offset rather than a DST-aware
  zone, `is_authenticated` documented as performing a request per access. Unverifiable claims
  removed. `tests/test_docs.py` turns the mechanical claims into oracles.

## [0.6.0] — 2026-08-06

A data-integrity release. **If you have ever passed `extra_filter` containing a top-level `OR`,
read "Who is affected" below — some extracts need re-running.**

### Fixed

- **`extra_filter` no longer escapes the project scope.** The filter was appended unparenthesized,
  and JQL binds `AND` tighter than `OR`, so `project = "P" AND status = x OR labels = y` parsed as
  `(project = "P" AND status = x) OR (labels = y)` — every row matching the right-hand branch
  qualified **regardless of project**. A project-scoped extract silently received rows from every
  other project. The filter is now parenthesized at every construction site.

- **Full scans no longer hang on such a filter.** The same mis-parse unbound the `id > N` cursor
  clause. Out-of-project rows with low ids sorted first under `ORDER BY id ASC`, filled every page,
  and re-pinned the cursor to the same value, so the scan issued requests forever without advancing.
  Measured on a synthetic universe: 26 `/search` calls, cursor stuck at `id > 2`.

- **`search_seek`'s full-scan path now validates its arguments.** It hand-built its JQL instead of
  using the `jql.py` builders, so it applied neither the project-key check nor the `extra_filter`
  guard — `search_seek('x" OR project = "OTHER')` was accepted and emitted a query matching two
  projects. The delta path always validated. Both now share one builder (`build_full_scan_jql`).

### Who is affected

Three call paths, all fixed. You are affected if you passed an `extra_filter` containing a
top-level `OR` (one not already wrapped in parentheses) to any of:

| path | symptom |
|---|---|
| `search_seek(...)` full scan (`after_ts=None`) | out-of-project rows, then a scan that never ends |
| `search_seek(..., after_ts=...)` **after a Lucene reindex** | same — the reindex recovery scan reused the unvalidated builder |
| `build_jql(...)` → `search_paged` / `list_keys` | out-of-project rows, **terminates normally with no error** |

The delta drain itself was correct; the delta path became affected only once a reindex was detected.
The `build_jql` path is the quiet one — it returns wrong data without hanging, and it has behaved this
way since 0.1.x.

Three failure modes, two of which look like success:

- **Contaminated** — foreign rows landed in your extract. Detect with `SELECT DISTINCT` on the
  project key or issue-key prefix in your target table.
- **Truncated** — the scan was killed after hanging, so the extract is a prefix, and any persisted
  cursor is the loop's fixed point: every later run re-reads the same page and appends nothing.
  Detect with a delta job whose new-row count has been flat since an `OR` filter was introduced.
- **Wedged** — the job never finished and never alarmed. It kept issuing HTTP requests, so it looks
  alive to any process- or network-level liveness check. Detect by runtime, not by status.

### Changed

- **`build_jql` output changed** (public API). `extra_filter` is now wrapped:
  `... AND (status = "Done") ...` rather than `... AND status = "Done" ...`. For a filter with no
  top-level `OR` this selects exactly the same rows; for one with a top-level `OR` it selects
  **fewer** rows — the ones that were never in scope. Code asserting on the exact string will need
  updating.
- **Invalid project keys and unsafe filters now raise `JiraQueryValidationError`** instead of bare
  `ValueError`. It subclasses **both** `ValueError` and `JiraResilientError`, so existing
  `except ValueError` handlers keep working and `except JiraResilientError` now catches it too. This
  matters because full-scan validation is new: a bad filter previously reached the server and came
  back as `JiraJqlError` (a `JiraResilientError`), so without the second base, callers guarding the
  family would have seen a new escape.
- **The `extra_filter` guard is less trigger-happy on ordinary data.** Its DDL-keyword check now
  ignores quoted string literals, so `status = "Update Required"` and `component = "Union Pay"` are
  accepted. Unquoted values containing a whole-word keyword (`labels = delete-me`) are still
  rejected — quote them. The comment/`--` check deliberately still runs against the raw string.
- The exception-hierarchy docstring no longer claims every escaping exception inherits from
  `JiraResilientError`; the thin endpoint wrappers let `requests.RequestException` through, as the
  README already documented.

### Added

- `jira_resilient.jql.build_full_scan_jql` — the full-scan cursor primitive. Module-level but
  deliberately **not** exported in `__all__`, matching the two delta builders: each emits one page of
  a scan and looks like a complete query when called alone.
- `tests/jql_model.py` — a precedence-correct JQL evaluator derived from JIRA's grammar rather than
  from this library, plus `tests/test_search_scope.py`, which asserts two properties across all four
  query paths: a scan never returns a row outside the requested project, and an `extra_filter` can
  only narrow a result set. A string assertion cannot catch an operator-precedence defect, which is
  why `test_extra_filter_is_parenthesized` passed throughout the life of this bug.

### Packaging

- `.python-version` and the local planning doc are gitignored, and the sdist now uses an explicit
  allowlist. Hatchling excludes gitignored files but **not** untracked ones, so both would otherwise
  have shipped to PyPI in the next release. CI re-checks the sdist contents so a later config edit
  cannot silently re-open the leak.
- Python 3.14 is now tested and declared. `requires-python` already permitted it.

### Gate (no runtime effect)

- **A dropped loop-exit condition now fails the suite instead of hanging it.** `client.py` pages
  with `while True`, and mutating the `not page` half of the termination guard made the suite run
  until killed rather than report a failure. `pytest-timeout` with a 15s per-test bound converts
  that into a normal failure; `required_plugins` makes the plugin's absence a clear error rather
  than an internal one.
- **All four copies of the paging loop now have an oracle.** Only `get_worklogs` had one, so
  mutating the same guard in `get_changelog`, `get_comments`, or `list_keys` left the suite green.
  Verified: each of the four mutants is now caught.
- **The release path is no longer weaker than `main`.** A tag push matches no branch filter, so
  `publish.yml` alone gated releases — pytest and `ruff check` on 3.12, nothing else. It now calls
  `test.yml`, which adds 3.13/3.14, the format check, coverage floors, a lowest-direct dependency
  floor job, `twine check`, and a wheel/sdist install smoke test run without the source tree on
  `sys.path`. The artifact that is inspected is the artifact that ships.
- Publishing is now re-runnable: `skip-existing` on the upload, and release creation falls back to
  editing, so a failure after a successful upload no longer strands the release permanently.
- The publish trigger is `v[0-9]*`. Non-release tags such as `v1-locked-…` matched `v*` and would
  have started a publish run.
- **The shipped type annotations are now verified.** The package has advertised `py.typed` and the
  `Typing :: Typed` classifier since 0.1.x, which tells every downstream type checker to trust these
  annotations, and nothing checked them — `mypy --strict` reported 36 errors. Public signatures that
  said `dict` now say `dict[str, Any]`, so consumers get the real shape instead of `Any`. `mypy` runs
  in CI and reports clean, as do `ty` and `basedpyright` independently. No runtime behavior changed
  and no suppression was added; the repo still contains zero `type: ignore`, `noqa`, or
  `pragma: no cover`.

## [0.5.0] — 2026-06-20

A correctness + hardening release. **Breaking:** the unsound `build_seek_jql` is removed and
some error/return behaviors changed (see Removed/Changed).

### Fixed
- **Delta `search_seek` no longer silently skips issues.** The old `(updated, key)` cursor mixed
  precisions — a bare minute date literal is the instant `MM:00` to JIRA Server, while
  `ORDER BY updated` is second-precision — so same-minute clusters and later-second/smaller-id
  rows were dropped. The delta scan now drains each minute with the half-open range
  `updated >= "MM" AND updated < "MM+1"` and seeks within it on numeric issue `id` (filter and
  sort agree exactly), then advances with a one-row `updated >= "MM+1"` probe. `id` and the
  minute advance monotonically, so the scan cannot skip or loop; a reindex falls back to a full
  `id`-ordered scan. Validated against a live JIRA Server on multi-hundred-issue same-minute
  bulk clusters.
- **Hub-tier search no longer fabricates `issuelinks = []`** when a per-issue issuelinks fetch
  fails — the key is left ABSENT so callers can tell "fetch failed" from "no links."
- **Pagination treats an absent `total` as "keep paging,"** not 0 (which truncated after one
  page) — `get_changelog` / `get_worklogs` / `get_comments` / `list_keys` / `search_paged`.
- **`get_changelog` no longer disables the paginated endpoint on an issue-404** — it confirms the
  issue exists first, so one missing issue can't poison the route for all.
- **Redirects are not followed** on REST calls (a proxy/SSO 3xx would turn `POST /search` into a
  body-less GET); a 3xx surfaces as an error.
- **`verify=False` works** (the self-signed escape hatch no longer raises a TLS `ValueError`), and
  the TLS-1.2 floor now applies to proxied connections too.
- **`get_remote_links`** raises `JiraParseError` on a non-list 200 body (error envelope) instead of
  returning it as data; **`is_authenticated` / `list_fields`** guard a non-JSON 200.

### Changed
- **`request_with_retry` is the single capped Retry-After authority** for both 429 and 5xx (the
  adapter no longer retries HTTP statuses); on exhaustion it raises the real 429/5xx response, not
  a bare error.
- **401/403 raise `JiraAuthError`** (was a raw `HTTPError`); a 4xx during the resilient fetch fails
  fast instead of burning all three tiers; degradation to hub/minimal is logged.
- **The session blocks cookies** (PAT auth needs none), removing shared mutable state so one
  session is safe to share across a thread fan-out.
- **`get_user` requires exactly one** of `username` / `key` / `account_id`.

### Removed
- **`build_seek_jql`** — it emitted the unsound single-shot cursor JQL. Sound delta pagination now
  lives in `JiraClient.search_seek`. `search_seek` still accepts `after_key` but ignores it
  (deprecated; the within-minute tiebreaker is internal).

### Added
- `JiraJqlError` is exported from the package root; `urllib3` is a declared dependency; the typed
  package gains the `Typing :: Typed` and Python 3.13 classifiers.

## [0.4.4] — 2026-06-07

### Added
- **`pool_maxsize` on `JiraClient` and `make_session`** (default 10, urllib3's default — no
  behavior change unless set). Sizes the per-host connection pool so a caller fanning N
  concurrent requests through one client/session can raise it to N. Without this, urllib3
  caps live connections at 10 and discards/reopens the surplus ("Connection pool is full"),
  throttling a high-concurrency fan-out to 10 regardless of the thread-pool width.

## [0.4.3] — 2026-06-07

First functional change since 0.4.0 — a focused retry-path fix.

### Fixed
- **429 rate-limit handling now honors `Retry-After`.** `request_with_retry` is the single
  authority for 429: it reads the `Retry-After` header (delta-seconds or HTTP-date) and
  sleeps for exactly that, falling back to the exponential schedule only when the header is
  absent, and capping any single sleep at 960s so a hostile or buggy header can't park a
  worker indefinitely. The connection-level retry no longer silently retries 429 (429 is
  dropped from its Retry-After status set), removing a layered double-retry; 413 and 503
  stay urllib3-handled. Previously the application-level 429 path always slept the blind
  `60 * 2**attempt` schedule, contrary to the documented behavior.

## [0.4.2] — 2026-06-04

Documentation/packaging only — **no functional change** (API and behavior identical
to 0.4.0/0.4.1).

### Changed
- Genericized remaining examples in the tests, docs, and changelog (timezone
  fixtures, illustrative scale figures, and the hub-issue patterns) so the package
  carries only neutral, example-agnostic detail.

## [0.4.1] — 2026-06-04

Documentation + packaging only — **no functional change**; the API and behavior are
identical to 0.4.0.

### Documentation
- **Completed the README API reference**, which had drifted to ~half the surface.
  Added the sub-entity reads (`get_comments` / `get_worklogs` / `get_remote_links`),
  the watchers / voters / user / entity-property methods, and the `server_tz`
  property; corrected the `get_changelog` note (paginated route 404s on JIRA Server
  → automatic `?expand=changelog` fallback) and the `search_seek` note (full scans
  page by `id`, deltas by `(updated, key)`).
- **Backfilled the missing `0.3.0` and `0.3.1` changelog entries.**

### Fixed
- **CI release-notes extraction.** The publish workflow matched the version header
  as a regex, so `## [x.y.z]` (where `[`, `]`, `.` are regex metacharacters) never
  matched and every published GitHub Release got a stub body instead of its
  changelog section. Now matched as a literal prefix; existing releases re-synced.

### Changed
- Docs, tests, and code comments now use neutral, generic example identifiers
  throughout.

## [0.4.0] — 2026-06-04

### Added

Six new single-entity read methods on `JiraClient`, each mirroring the existing
`get_comments` / `get_worklogs` / `get_remote_links` style (retry-with-backoff,
JIRA-Server REST endpoints, graceful absence handling). All endpoint shapes were
verified live against a JIRA Server instance before release.

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
  some projects) — that collapses to `{}` rather than raising.
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

## [0.3.1] — 2026-06-01

### Fixed
- **`server_tz` falls back to UTC on probe failure, not the host's local timezone.**
  When the `/rest/api/2/serverInfo` probe failed (or returned an unparseable
  `serverTime`), the property previously fell through to the *machine's* local
  timezone — silently wrong on any non-UTC host (a cloud runner defaulting to UTC
  vs a developer's local box), shifting every delta sync's JQL window by the host
  offset. Now the fallback is always UTC, which is at worst the same behavior
  callers had before any TZ handling existed. Regression tests added for both the
  probe-success (server offset honored) and probe-failure (UTC) paths.

### Changed
- **Behavior-preserving tightening pass.** Trimmed docstrings/comments that merely
  restate names; hoisted the stale-`after_key` detection limits to module
  constants; `datetime.fromisoformat` now parses the `Z`/offset suffix natively
  (dropped the manual `.replace("Z", "+00:00")`); de-duplicated the `after_key`
  assignment; switched duplicate detection to set subtraction; `contextlib.suppress`
  in the JQL-error path. No surface or behavior change beyond the `server_tz` fix.

## [0.3.0] — 2026-05-30

### Changed
- **`search_seek` now dispatches by intent on `after_ts` — full scans page by issue
  `id`, deltas by `(updated, key)`.** A full load (`after_ts is None`) pages the
  whole project by issue **`id` ascending** (`_search_by_id`): one numeric ordering
  drives both the `id > N` filter and the `ORDER BY id ASC` sort, so the cursor
  advances monotonically and the scan **cannot loop**. It needs none of the cycle
  detection, minute-bump, or duplicate-tracking the `updated` cursor requires, and
  is structurally immune to the JQL minute-precision and Lucene-reindex pitfalls
  those guards exist for. A delta load (`after_ts` set) keeps the `(updated, key)`
  cursor (`_search_by_updated`) — the only ordering that expresses "changed since
  X" — and its reindex recovery now *delegates* to the same `_search_by_id`, so the
  id-scan logic lives in exactly one place; on detecting a reindex loop the delta
  path falls back to a full `id` scan. The `updated`-parse and stale-`after_key`
  guards are now delta-only (a full scan never reads `updated` or uses `after_key`).
- Validated by a full re-load against a large project, which reproduced the live
  source exactly with **zero** reindex-recovery machinery firing — the full path
  scans purely by `id`.

## [0.2.2] — 2026-05-20

### Fixed
- **`search_seek` auto-recovers from two more `after_key` failure modes**
  in addition to the deleted-issue case shipped in 0.2.1:
  - `"Operator '>' cannot be applied to moved issue key 'X'."` — fires
    when the cursor's `after_key` references an issue reprojected to another
    project. Observed in the field shortly after 0.2.1.
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

  Trigger: a JIRA admin deleting a project's most-recent issue breaks that
  project's delta sync — every subsequent cycle 400s identically until the
  stale `after_key` is cleared. The loop now self-heals on the next cycle.

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
- **Large-hub timeouts no longer block delta sync.** Before 0.1.3, a
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
  caught 1-page cycles; longer cycles (keys rotating between several
  values within one minute) bypassed it and ran forever. Replaced with a deque of
  recent boundaries — any repeat within the last 10 pages now forces a
  minute-advance and clears `after_key`. Regression test added.
  Symptom: a delta loop re-fetched a small same-minute gap many times over
  without ever progressing past the boundary minute.

## [0.1.1] — 2026-05-19

### Fixed
- **JQL timezone bug.** JIRA Server's JQL parser interprets bare `"YYYY-MM-DD HH:MM"`
  date strings in the JIRA server's local timezone, **not** UTC. `build_jql` and
  `build_seek_jql` previously formatted datetimes in their source TZ (typically UTC),
  so JIRA Server silently shifted the filter by its local offset. For a JIRA
  whose server TZ is N hours behind UTC, every delta sync's filter was shifted
  N hours forward — effectively dropping every recently-updated issue from each run.

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
