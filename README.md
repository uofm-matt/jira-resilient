# jira-resilient

Resilient JIRA Server data extraction at scale. Seek pagination, reindex-aware
recovery, and a three-tier resilient single-issue fetch — built to survive
100K+ issue projects, hub issues with thousands of links, and the occasional
server-side Lucene reindex.

## What problem this solves

Every JIRA Server Python client on PyPI today (`jira`, `atlassian-python-api`,
`pycontribs/jira`, …) uses **offset pagination** under the hood. JIRA Server's
offset pagination materializes the full sort and skips `startAt` rows per
request — cost grows roughly quadratically with project size. On a 150K issue
project this is the difference between a 30-minute load and one that never
finishes.

JIRA Server's official guidance is "limit your queries." That isn't an answer
when the warehouse needs every issue.

This library implements three things that aren't in any other JIRA client:

1. **Seek-paginated search** — `(updated, key)` tuple cursor in JQL itself; every
   request uses `startAt=0`. Per-request cost bounded regardless of project size.
2. **Lucene-reindex monotonic-`after_ts`** — survives the edge case where a
   server-side reindex stamps many issues with a future indexed-`updated` that
   diverges from `fields.updated`. Without this guard, the seek cursor regresses
   and loops forever; we've watched it happen.
3. **Three-tier resilient issue fetch** — `fields=*all` → `*all,-issuelinks`
   plus a separate `issuelinks` fetch (long timeout) → minimal field set.
   Recovers data from "hub" issues with thousands of links whose `*all` payload
   consistently exceeds 120s.

Plus a few smaller pieces of operational hardening: paginated changelog
fallback for huge histories, fail-fast-on-4xx in the retry loop, and a JQL
extra-filter injection guard.

## Install

```bash
pip install jira-resilient
```

Or with [uv](https://github.com/astral-sh/uv):

```bash
uv add jira-resilient
```

## Quickstart

```python
from jira_resilient import JiraClient

client = JiraClient(
    base_url="https://jira.example.com",
    pat="<your-personal-access-token>",
    verify=True,                # path to CA bundle, True for system CAs, False to skip
)

if not client.is_authenticated:
    raise SystemExit("auth failed")

# 1. Seek-paginated scan — survives 100K+ issue projects.
for page in client.search_seek("PROJ"):
    for issue in page.issues:
        print(issue["key"], issue["fields"]["summary"])

# 2. Delta scan — resume from a known (updated, key) boundary.
from datetime import datetime, timezone
cursor_ts  = datetime(2026, 5, 18, 7, 30, tzinfo=timezone.utc)
cursor_key = "PROJ-12345"
for page in client.search_seek("PROJ", after_ts=cursor_ts, after_key=cursor_key):
    ...

# 3. Single-issue fetch with three-tier resilience (full → hub → minimal).
result = client.get_issue_resilient("PROJ-1234")
print(result.tier)         # "full" | "hub" | "minimal" — log this; minimal == lossy
print(result.issue["key"])

# 4. Pure-key enumeration (tiny payload, for reconciliation).
keys = client.list_keys('project = "PROJ"')

# 5. Paginated changelog (works on huge histories that overflow expand=changelog).
history = client.get_changelog("PROJ-1234")
```

## The Lucene reindex story

If you only read one section of this README, read this. It's the bug that
took a day to find and is the reason this library exists.

JIRA Server occasionally runs a Lucene reindex. After a reindex, the
**indexed** `updated` timestamp on many issues is set to the reindex time. The
issue document's `fields.updated` is unaffected.

If you run a seek-paginated loop, advancing the cursor by `fields.updated` of
the last issue on each page, you eventually hit a reindexed group: thousands
of issues whose `fields.updated` is some old date (say, 2024) but whose
indexed-`updated` is yesterday. Your JQL says `updated > "old-date"`. JIRA's
matcher uses the indexed value (yesterday), so it returns the whole reindexed
group. Your seek cursor goes BACKWARD in time. Your next request says
`updated > "even-older-date"`. It loops forever, scanning the same group.

The fix: `after_ts` is kept **monotonically non-decreasing** —
`after_ts = max(after_ts, new_ts)`. Combined with a minute-advance fallback
that preserves `after_key`, same-Lucene-timestamp groups can be paged through
by key alone, and the cursor can never regress.

This library implements that fix in `search_seek`. The seek loop is documented
in the source — see `client.py`.

## Reference

### `JiraClient`

```python
JiraClient(base_url, pat, *, verify=True, timeout=120, max_attempts=5)
```

Constructor. Builds a `requests.Session` with TLS 1.2+ enforced, bearer-PAT
auth, and a retry adapter for transient 5xx at the connection layer.

| Method | Endpoint | Notes |
|---|---|---|
| `is_authenticated` (property) | `GET /myself` | Logs displayName on success |
| `get_issue(key, *, expand, fields, timeout, max_attempts)` | `GET /issue/{key}` | Defaults to `fields=*all` + changelog |
| `get_issue_minimal(key)` | `GET /issue/{key}` | Small-field set, short timeout |
| `get_issuelinks(key, *, timeout=600)` | `GET /issue/{key}` (fields=issuelinks) | Long timeout for hub issues |
| `get_changelog(key, *, page_size=100)` | `GET /issue/{key}/changelog` | Paginated; survives huge histories |
| `get_issue_resilient(key)` | three-tier | Returns `ResilientFetchResult(issue, tier)` |
| `list_fields()` | `GET /field` | Full field catalog |
| `list_keys(jql)` | `POST /search` (fields=key) | Tiny payload; for reconciliation |
| `search_paged(jql, *, page_size=50)` | `POST /search` (offset) | Use sparingly — quadratic on large projects |
| `search_seek(project_key, *, after_ts, after_key, extra_filter, page_size=20)` | `POST /search` (seek) | The killer feature; use this for project-wide enumeration |

### Exceptions

```python
from jira_resilient import (
    JiraResilientError,   # base of the hierarchy
    JiraAuthError,        # 401/403
    JiraParseError,       # 2xx response missing expected fields
    JiraFetchError,       # all retry attempts / fallback tiers exhausted
)
```

`requests.RequestException` may still escape — anything we don't explicitly
wrap. Catch `JiraResilientError` for library-raised failures or `Exception`
for everything.

### JQL helpers

Available at the top level for callers who want to compose JQL without going
through the client:

```python
from jira_resilient import build_jql, build_seek_jql
```

## Caveats

- **JIRA Server / Data Center only.** JIRA Cloud uses a different API surface
  (`/rest/api/3`) and offset semantics; this library is not tested against it.
- **Personal Access Token auth only.** Basic auth is deprecated; OAuth/JWT
  not supported here.
- **No async.** A future minor version may add an `AsyncJiraClient`; for now
  everything is synchronous.
- **No automatic field-name semantic mapping.** `customfield_10016` stays
  `customfield_10016` in the response — the library doesn't know your
  semantic naming.

## Development

```bash
git clone <repo>
cd jira-resilient
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).
