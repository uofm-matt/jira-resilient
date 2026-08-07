"""Scoping properties that must hold for every query path that accepts an `extra_filter`.

These assert what a scan MEANS, not what string it emits. `tests/test_jql.py` already pins the
emitted text of two builders — and passed for the entire life of the defect these tests catch,
because the two construction sites that bypassed those builders were never asserted at all.

Each path is driven against `tests/jql_model.py`, a precedence-correct evaluator derived from
JIRA's grammar rather than from this library's code. Two invariants, checked per path:

  SCOPE      every returned row belongs to the requested project
  NARROWING  adding an `extra_filter` can only remove rows, never add them

A third hazard is structural rather than a property: when the project clause stops binding, the
`id` cursor stops advancing and the scan runs forever. `_CallCounter` bounds that into a failure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import jql_model
import pytest
import responses

from jira_resilient import JiraClient, build_jql

# Out-of-project rows carry LOW ids on purpose: under `ORDER BY id ASC` they sort first, so a
# query whose project clause has stopped binding will re-serve them as page one forever.
UNIVERSE = [
    {"id": 1, "key": "OTHER-1", "project": "OTHER", "status": "Open", "labels": ["urgent", "hot"]},
    {"id": 2, "key": "OTHER-2", "project": "OTHER", "status": "Done", "labels": ["urgent"]},
    {"id": 10, "key": "PROJ-1", "project": "PROJ", "status": "Done", "labels": []},
    {"id": 20, "key": "PROJ-2", "project": "PROJ", "status": "Open", "labels": ["urgent"]},
    {"id": 30, "key": "PROJ-3", "project": "PROJ", "status": "Done", "labels": ["urgent"]},
]

for _n, _row in enumerate(UNIVERSE):
    _row["updated"] = datetime(2026, 5, 19, 11, 50 + _n, tzinfo=UTC)

CURSOR = datetime(2026, 5, 19, 11, 49, tzinfo=UTC)

FILTERS = [
    None,
    'status = "Done"',
    'status = "Done" OR labels = urgent',
    "labels = urgent OR labels = hot",
    'status = "Done" AND labels = urgent',
]


class _CallCounter:
    """Turns a non-terminating scan into a failed assertion instead of a hung test run."""

    def __init__(self, cap: int = 40):
        self.n = 0
        self.cap = cap

    def tick(self, jql: str) -> None:
        self.n += 1
        if self.n > self.cap:
            raise AssertionError(
                f"scan did not terminate: {self.n} /search calls, cursor is not advancing. "
                f"Last JQL: {jql}"
            )


def _register(base_url, counter, *, stale_updated=None):
    """Serve /search from the model. `stale_updated` simulates Lucene reindex divergence:
    the INDEX matches the query (so the model selects normally) while `fields.updated` still
    reports an older value — the exact signal `_search_by_updated` falls back on."""

    def _callback(request):
        body = json.loads(request.body)
        jql = body["jql"]
        counter.tick(jql)
        # `search_paged` is offset-paginated; seek callers always send startAt=0.
        start = body.get("startAt", 0)
        hits = jql_model.select(jql, UNIVERSE)[start : start + body["maxResults"]]
        issues = [
            {
                "id": str(r["id"]),
                "key": r["key"],
                "fields": {"updated": (stale_updated or r["updated"].isoformat())},
            }
            for r in hits
        ]
        return (200, {}, json.dumps({"issues": issues, "names": {}, "schema": {}}))

    responses.add_callback(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        callback=_callback,
        content_type="application/json",
    )


@pytest.fixture
def client(base_url):
    c = JiraClient(base_url, pat="test", verify=False)
    c._server_tz = UTC  # skip the /serverInfo probe; JQL literals are already UTC here
    return c


def _keys(pages):
    return sorted(i["key"] for p in pages for i in p.issues)


def _drive(client, base_url, mode, extra_filter):
    counter = _CallCounter()
    # A reindexed row reports a `fields.updated` far behind the cursor while the index still
    # matches — the divergence that sends `_search_by_updated` into its id-scan recovery.
    _register(
        base_url,
        counter,
        stale_updated="2024-07-14T19:37:16.000+0000" if mode == "reindex" else None,
    )
    match mode:
        case "full":
            pages = client.search_seek("PROJ", extra_filter=extra_filter, page_size=2)
        case "delta" | "reindex":
            pages = client.search_seek(
                "PROJ", after_ts=CURSOR, extra_filter=extra_filter, page_size=2
            )
        case "paged":
            pages = client.search_paged(build_jql("PROJ", extra_filter=extra_filter), page_size=2)
        case _:
            raise ValueError(f"unknown mode {mode!r}")
    return _keys(list(pages))


MODES = ["full", "delta", "reindex", "paged"]


@responses.activate
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("extra_filter", FILTERS)
def test_scan_never_leaves_the_requested_project(client, base_url, mode, extra_filter):
    """SCOPE: no `extra_filter` can pull in a row from another project.

    The defect this catches: `project = "P" AND a OR b` parses as `(project = "P" AND a) OR b`,
    so any row matching `b` qualifies regardless of project.
    """
    got = _drive(client, base_url, mode, extra_filter)
    assert all(k.startswith("PROJ-") for k in got), f"{mode} leaked out-of-project rows: {got}"


@responses.activate
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("extra_filter", [f for f in FILTERS if f is not None])
def test_extra_filter_only_narrows(client, base_url, mode, extra_filter):
    """NARROWING: a filter is a restriction. It may remove rows; it may never add one."""
    unfiltered = set(_drive(client, base_url, mode, None))
    responses.reset()
    filtered = set(_drive(client, base_url, mode, extra_filter))
    assert filtered <= unfiltered, (
        f"{mode}: filter {extra_filter!r} ADDED rows {sorted(filtered - unfiltered)}"
    )


@responses.activate
@pytest.mark.parametrize("mode", MODES)
def test_filter_selects_exactly_what_the_model_says(client, base_url, mode):
    """The scan agrees with an independent reading of its own query, row for row."""
    extra_filter = 'status = "Done" OR labels = urgent'
    got = _drive(client, base_url, mode, extra_filter)
    expected = sorted(
        r["key"] for r in UNIVERSE if jql_model.matches(f'project = "PROJ" AND ({extra_filter})', r)
    )
    assert got == expected
