# jira-resilient

> A Python client for **JIRA Server** that survives 100K+ issue projects, hub issues with thousands of links, and the occasional Lucene reindex. A drop-in alternative to `atlassian-python-api` for **ETL / data-warehouse workloads**.

[![PyPI](https://img.shields.io/pypi/v/jira-resilient.svg)](https://pypi.org/project/jira-resilient/)
[![Python](https://img.shields.io/pypi/pyversions/jira-resilient.svg)](https://pypi.org/project/jira-resilient/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/uofm-matt/jira-resilient/test.yml?branch=main&label=tests)](https://github.com/uofm-matt/jira-resilient/actions/workflows/test.yml)

JIRA Server's `/search` endpoint uses offset pagination — cost grows roughly quadratically with project size. On a 150K-issue project this is the difference between a 30-minute extract and one that never finishes. Existing Python clients work around this with "limit your queries," which isn't an answer when a warehouse needs every row. This library implements seek pagination + a handful of related reliability fixes that aren't in any other JIRA library on PyPI today.

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

# Seek-paginated scan — survives 100K+ issue projects.
for page in client.search_seek("PROJ"):
    for issue in page.issues:
        print(issue["key"], issue["fields"]["summary"])

# Delta scan — resume from a saved (updated, key) cursor.
from datetime import datetime, timezone
cursor_ts  = datetime(2026, 5, 18, 7, 30, tzinfo=timezone.utc)
cursor_key = "PROJ-12345"
for page in client.search_seek("PROJ", after_ts=cursor_ts, after_key=cursor_key):
    ...

# Three-tier resilient single-issue fetch (recovers from hub-issue timeouts).
result = client.get_issue_resilient("PROJ-1234")
print(result.tier)    # "full" | "hub" | "minimal" — log this; minimal is lossy

# Minimal-payload key enumeration (for reconciliation against a warehouse).
keys = client.list_keys('project = "PROJ"')

# Paginated changelog — works on issues whose `expand=changelog` payload
# overflows the 120s timeout.
history = client.get_changelog("PROJ-1234")
```

## Why this exists

Every JIRA Python client on PyPI today (`jira`, `atlassian-python-api`, `pycontribs/jira`) uses offset pagination under the hood and has no answer for these three problems:

| Problem | Other clients | `jira-resilient` |
|---|---|---|
| 100K+ issue projects (offset cost is ~ O(n²)) | "limit your queries" | `search_seek` — per-request cost bounded regardless of project size |
| Lucene-reindex monotonic-`after_ts` divergence | silently loops forever, scanning the same group | cursor floor + minute-advance fallback; documented below |
| Hub issues w/ thousands of links time out on `*all` | request fails; issue unrecoverable | three-tier fetch: `full` → `*all,-issuelinks` + separate links fetch → minimal fields |

Plus a handful of smaller fixes: paginated `/issue/{key}/changelog` for huge histories, fail-fast-on-4xx in the retry loop (so a permission error doesn't waste 15 minutes of exponential backoff), and a JQL extra-filter injection guard.

### The Lucene reindex story

The bug that took a day to find, and the reason this library exists.

JIRA Server occasionally runs a Lucene reindex. After the reindex, the **indexed** `updated` timestamp on many issues is set to the reindex time. The document's `fields.updated` is unaffected.

If you run a seek-paginated loop, advancing the cursor by `fields.updated` of the last issue on each page, you eventually hit a reindexed group: thousands of issues whose `fields.updated` is some old date (say, 2024) but whose indexed-`updated` is yesterday. Your next JQL says `updated > "old-date"`. JIRA's matcher uses the **indexed** value, so it returns the whole reindexed group — and your cursor just went *backward in time*. Next request, even broader. Infinite loop, no error, just chewing through the same group forever.

The fix: `after_ts` is kept **monotonically non-decreasing** — `after_ts = max(after_ts, new_ts)`. Combined with a minute-advance fallback that preserves `after_key`, same-Lucene-timestamp groups page through cleanly by key alone, and the cursor can't regress. This is documented in [`client.py:search_seek`](src/jira_resilient/client.py).

## API reference

`JiraClient(base_url, pat, *, verify=True, timeout=120, max_attempts=5)`

| Method | Endpoint | Notes |
|---|---|---|
| `is_authenticated` (prop) | `GET /myself` | Logs displayName on success |
| `get_issue(key, *, expand, fields, …)` | `GET /issue/{key}` | Defaults to `fields=*all` + changelog |
| `get_issue_minimal(key)` | `GET /issue/{key}` | Small-field set, short timeout |
| `get_issuelinks(key, *, timeout=600)` | `GET /issue/{key}` | Long default timeout for hub issues |
| `get_changelog(key, *, page_size=100)` | `GET /issue/{key}/changelog` | Paginated; survives huge histories |
| `get_issue_resilient(key)` | three-tier | Returns `ResilientFetchResult(issue, tier)` |
| `list_fields()` | `GET /field` | Full field catalog |
| `list_keys(jql)` | `POST /search` (fields=key) | Tiny payload; for reconciliation |
| `search_paged(jql, *, page_size=50)` | `POST /search` (offset) | Use sparingly — quadratic on large projects |
| `search_seek(project_key, *, after_ts, after_key, extra_filter, page_size=20)` | `POST /search` (seek) | **The killer feature.** Use this for project-wide enumeration. |

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
