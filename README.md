# jira-resilient

> A Python client for **JIRA Server** that reliably extracts issues with **thousands of issuelinks** — the "hub" issues that consistently time out and break other clients. Built for ETL / data-warehouse workloads where missing data isn't an option.

[![PyPI](https://img.shields.io/pypi/v/jira-resilient.svg)](https://pypi.org/project/jira-resilient/)
[![Python](https://img.shields.io/pypi/pyversions/jira-resilient.svg)](https://pypi.org/project/jira-resilient/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/uofm-matt/jira-resilient/test.yml?branch=main&label=tests)](https://github.com/uofm-matt/jira-resilient/actions/workflows/test.yml)

In any large JIRA Server install, a handful of "hub" issues accumulate enormous numbers of `Implements` / `Tests` / `Relates` links — easily into the thousands. Real example we hit: **`PROJ-1234`, several thousand issuelinks.** The standard `GET /issue/{key}?fields=*all` for that issue returns a ~10 MB payload that takes JIRA 3+ minutes to serialize — well past every existing Python client's default timeout. Result: the issue is silently absent from the warehouse, no error, no retry that would help.

This library exists to solve that. The fix is a three-tier fetch that recognizes the timeout pattern and recovers data via split requests:

```python
result = client.get_issue_resilient("PROJ-1234")
# result.tier == "hub"    → fields=*all,-issuelinks fetched fast,
#                          issuelinks fetched separately with a long timeout
# result.issue            → fully assembled issue, several thousand links and all
```

Plus a few related reliability fixes the same code path needed along the way (seek-paginated `/search`, Lucene-reindex cursor handling, paginated changelog fallback, fail-fast-on-4xx in the retry loop). Documented further down.

## Install

```bash
pip install jira-resilient        # or:  uv add jira-resilient
```

## Quickstart

```python
from jira_resilient import JiraClient

client = JiraClient(
    base_url="https://jira.example.com",
    pat="<personal-access-token>",
    verify="/path/to/ca-bundle.pem",   # or True for system CAs, False to skip
)

if not client.is_authenticated:
    raise SystemExit("auth failed")

# THE killer feature — resilient single-issue fetch that survives hub issues.
result = client.get_issue_resilient("PROJ-1234")
print(result.tier)         # "full" | "hub" | "minimal" — log this; minimal is lossy
print(len(result.issue["fields"]["issuelinks"]))   # 7944

# Seek-paginated scan — survives 100K+ issue projects.
for page in client.search_seek("PROJ"):
    for issue in page.issues:
        ...

# Delta scan — resume from a saved (updated, key) cursor.
from datetime import datetime, timezone
cursor_ts  = datetime(2026, 5, 18, 7, 30, tzinfo=timezone.utc)
cursor_key = "PROJ-12345"
for page in client.search_seek("PROJ", after_ts=cursor_ts, after_key=cursor_key):
    ...

# Paginated changelog — for issues whose `expand=changelog` payload overflows the timeout.
history = client.get_changelog("PROJ-1234")

# Minimal-payload key enumeration (for reconciliation against a warehouse).
keys = client.list_keys('project = "PROJ"')
```

## Why this exists

Every JIRA Python client on PyPI today (`jira`, `atlassian-python-api`, `pycontribs/jira`) was built for interactive use — small queries, single issues with normal-sized payloads. They all fail in predictable ways on the data-warehouse workload, and none of them have fixes:

| Problem | Other clients | `jira-resilient` |
|---|---|---|
| **Hub issues with thousands of issuelinks** (e.g. several thousand) — `fields=*all` payload exceeds 120s timeout, request fails | issue unrecoverable, silently absent from your data | three-tier fetch: `full` → `*all,-issuelinks` + separate links fetch with long timeout → minimal fields |
| 100K+ issue projects — offset pagination is ~O(n²) on JIRA Server | "limit your queries" (Atlassian's documented guidance) | `search_seek` — `(updated, key)` tuple cursor in JQL, `startAt=0` every request, bounded per-page cost |
| Lucene reindex makes seek cursors silently regress | n/a — no client implements seek | monotonic `after_ts` floor + minute-advance fallback (the war story below) |
| Huge changelogs overflow `expand=changelog` | request fails; history lost | paginated `/issue/{key}/changelog` |
| 4xx in the retry loop | exponential backoff over a permission error wastes 15 min | fail-fast on 4xx; only 429/5xx trigger backoff |

### The hub-issue problem, in detail

A "hub" issue in JIRA isn't a special type — it's any issue that ends up linked to a lot of other issues over its lifetime. Common patterns that produce them:

- A **parent requirement** with `Implements` links to every child that satisfies it
- An **integration test plan** that `Tests` every endpoint
- A **decomposition anchor** that's `Decomposed-from` for an entire feature area
- A **defect tracker** with `Relates` to every related ticket

In a project with a few thousand issues, you might have 5–10 hub issues out of the lot. In a project with 150K issues — common in long-lived enterprise installs — you can have hundreds, with link counts climbing into the low thousands.

Three reasons existing clients can't handle this:

1. **`fields=*all` returns everything inline.** A 7K-link issue is a ~10 MB JSON payload. JIRA's serializer is single-threaded per request and takes minutes; clients with a 60s or 120s timeout just see a `ReadTimeoutError`.
2. **There's no documented escape hatch.** JIRA Server has no "give me this issue but skip the slow fields" endpoint. You have to know to request `fields=*all,-issuelinks` and then fetch `issuelinks` separately with a longer timeout — and even then, the issuelinks-only request can take 3+ minutes for the largest hubs.
3. **Retrying doesn't help.** Exponential backoff over the same broken request just wastes time. You need a fundamentally different fetch pattern, not more attempts.

`get_issue_resilient` implements that pattern. The three tiers handle ~95% of issues at full fidelity (tier `full`), ~5% via the hub-split tier (tier `hub`, no data loss, ~3-4 minute total), and a small fraction via the last-resort minimal-field tier (tier `minimal`, lossy — description and custom fields are empty). The `tier` field on the result lets you log which path each issue took.

### The Lucene reindex story

The bug that took a day to find, and why `search_seek` has the monotonic-`after_ts` guard.

JIRA Server occasionally runs a Lucene reindex. After the reindex, the **indexed** `updated` timestamp on many issues is set to the reindex time. The document's `fields.updated` is unaffected.

If you run a seek-paginated loop, advancing the cursor by `fields.updated` of the last issue on each page, you eventually hit a reindexed group: thousands of issues whose `fields.updated` is some old date (say, 2024) but whose indexed-`updated` is yesterday. Your next JQL says `updated > "old-date"`. JIRA's matcher uses the **indexed** value, so it returns the whole reindexed group — and your cursor just went *backward in time*. Next request, even broader. Infinite loop, no error, just chewing through the same group forever.

The fix: `after_ts` is kept **monotonically non-decreasing** — `after_ts = max(after_ts, new_ts)`. Combined with a minute-advance fallback that preserves `after_key`, same-Lucene-timestamp groups page through cleanly by key alone, and the cursor can't regress. See [`client.py:search_seek`](src/jira_resilient/client.py).

## API reference

`JiraClient(base_url, pat, *, verify=True, timeout=120, max_attempts=5)`

| Method | Endpoint | Notes |
|---|---|---|
| `is_authenticated` (prop) | `GET /myself` | Logs displayName on success |
| **`get_issue_resilient(key)`** | three-tier | **The killer feature.** Returns `ResilientFetchResult(issue, tier)` |
| `get_issue(key, *, expand, fields, …)` | `GET /issue/{key}` | Defaults to `fields=*all` + changelog |
| `get_issue_minimal(key)` | `GET /issue/{key}` | Small-field set, short timeout |
| `get_issuelinks(key, *, timeout=600)` | `GET /issue/{key}` | Long default timeout for hub issues |
| `get_changelog(key, *, page_size=100)` | `GET /issue/{key}/changelog` | Paginated; survives huge histories |
| `list_fields()` | `GET /field` | Full field catalog |
| `list_keys(jql)` | `POST /search` (fields=key) | Tiny payload; for reconciliation |
| `search_paged(jql, *, page_size=50)` | `POST /search` (offset) | Use sparingly — quadratic on large projects |
| `search_seek(project_key, *, after_ts, after_key, extra_filter, page_size=20)` | `POST /search` (seek) | For project-wide enumeration |

### Exceptions

```python
from jira_resilient import (
    JiraResilientError,   # base of the hierarchy
    JiraAuthError,        # 401/403
    JiraParseError,       # 2xx response missing expected fields
    JiraFetchError,       # all retry attempts / fallback tiers exhausted
)
```

`requests.RequestException` may still escape on conditions the library doesn't wrap. Catch `JiraResilientError` for library-raised failures, or `Exception` for everything.

### JQL helpers

```python
from jira_resilient import build_jql, build_seek_jql
```

Pure functions, no network calls — for callers that want to compose JQL outside of any request flow.

## Non-goals

- **JIRA Cloud is not supported.** Cloud uses `/rest/api/3` and different paging semantics; this library is JIRA Server / Data Center only.
- **No async client.** `JiraClient` is synchronous. An `AsyncJiraClient` may land in a future minor version.
- **Basic auth, OAuth, and JWT are not supported.** Personal Access Token (Bearer header) only — that's what modern JIRA Server installs use.
- **No automatic field-name semantic mapping.** `customfield_10016` stays `customfield_10016` in the response. Build your own mapping at the application layer if you need one.
- **No DB / warehouse integration.** This is a JIRA client, not an ETL framework. Wire it up to your warehouse yourself.
- **Not a replacement for `atlassian-python-api`'s full surface.** This is a focused client for the data-extraction subset.

## Compatibility

| | Version |
|---|---|
| Python | 3.12+ |
| JIRA Server / Data Center | 8.6+ (for paginated `/issue/{key}/changelog`); older may work for non-changelog use |
| `requests` | 2.31+ |

## Development

```bash
git clone https://github.com/uofm-matt/jira-resilient
cd jira-resilient
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest
```

Tests run against mocked HTTP via [`responses`](https://github.com/getsentry/responses) — no real network. Run time: ~0.1s for the full suite.

## License

MIT — see [LICENSE](LICENSE).
