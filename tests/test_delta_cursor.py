"""Two cursor invariants of the delta scan that a fully green suite did not constrain.

`_search_by_updated` advances with `minute, after_id = nxt, None` and detects a Lucene reindex
with `if nxt <= minute`. A mutation battery dropped the `after_id` reset and narrowed the
comparison to `<`; both mutants passed all 187 tests.

Neither survivor needs a new code path — both are hidden by the SHAPE of the existing data.
`test_search_seek_delta_advances_across_minutes_and_terminates` uses ids 10/11/12/13 rising in
lockstep with `updated`, so a stale `after_id` carried over from the previous minute is always
below every id in the next one and filters nothing out.
`test_search_seek_post_reindex_falls_back_to_id_scan` dates its lagging row 2024 against a 2026
cursor, two years BELOW it, so `nxt < minute` alone still fires. The rows here are shaped for
the cases those miss: ids DESCENDING as `updated` ascends, and a probe result exactly EQUAL to
the cursor minute.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import responses
from test_client import _fake_jira_delta_search


@responses.activate
def test_advancing_to_a_new_minute_resets_the_id_cursor(client, base_url):
    """The ordinary re-edit pattern, which the lockstep fixtures never produce: an OLD issue
    (low id) edited most recently, so `id` DESCENDS while `updated` ascends.

    Carrying `after_id` across a minute boundary then filters out everything that follows —
    `id > 900` matches neither 500 nor 100 — and the drain for each later minute comes back
    empty. The probe still advances the minute, so the scan terminates normally and reports
    one changed issue for a project where three changed. No error, no short page, nothing in
    the logs: an incremental load just silently misses the rows.
    """
    dataset = [
        {"id": "900", "key": "PROJ-900", "updated": "2026-05-18T10:00:00.000+0000"},
        {"id": "500", "key": "PROJ-500", "updated": "2026-05-18T10:03:00.000+0000"},
        {"id": "100", "key": "PROJ-100", "updated": "2026-05-18T10:07:00.000+0000"},
    ]
    _fake_jira_delta_search(base_url, dataset)

    pages = list(
        client.search_seek("PROJ", after_ts=datetime(2026, 5, 18, 10, 0, tzinfo=UTC), page_size=20)
    )

    got = sorted(i["key"] for p in pages for i in p.issues)
    assert got == ["PROJ-100", "PROJ-500", "PROJ-900"]
    assert not any(p.fallback for p in pages)  # a clean advance, not the reindex recovery


_CURSOR = datetime(2026, 5, 19, 11, 51, tzinfo=UTC)

# The probe asks for `updated >= "11:52"` and the INDEX answers it, but the row's stored
# `fields.updated` still reads 11:51:15 — 45 seconds behind the boundary, inside the cursor
# minute itself. Floored to a minute that is 11:51: the cursor exactly, neither ahead nor
# behind. This is the equality the reindex guard is written for and the only value that
# separates `nxt <= minute` from `nxt < minute`.
_LAGGING = "2026-05-19T11:51:15.000+0000"

_BUDGET = 12  # pristine needs 5: one drain, one probe, three id-scan pages


class _SpinGuard(Exception):
    """Raised by the fake server once the scan blows its call budget, so a cursor that stops
    advancing fails in milliseconds instead of running to the 15s suite timeout."""


def _register_reindexed_server(base_url) -> list[str]:
    """Serve /search as a JIRA that has just reindexed: the index matches every query, while
    `fields.updated` on the returned rows still reports the pre-reindex value."""
    universe = [("1", "OPS-1"), ("2", "OPS-2"), ("3", "OPS-3")]
    calls: list[str] = []

    def _callback(request):
        body = json.loads(request.body)
        jql, max_results = body["jql"], body["maxResults"]
        calls.append(jql)
        if len(calls) > _BUDGET:
            raise _SpinGuard(f"{len(calls)} /search calls, cursor not advancing. Last: {jql}")
        if "updated < " in jql:  # half-open drain (both >= and <): the cursor minute is empty
            issues = []
        elif "updated >= " in jql:  # advance probe (>= only): the lagging row
            issues = [{"id": "1", "key": "OPS-1", "fields": {"updated": _LAGGING}}]
        else:  # id-ordered fallback scan — no `updated` clause at all
            after = int(g.group(1)) if (g := re.search(r"id > (\d+)", jql)) else 0
            issues = [
                {"id": i, "key": k, "fields": {"updated": _LAGGING}}
                for i, k in universe
                if int(i) > after
            ]
        body = {"issues": issues[:max_results], "names": {}, "schema": {}}
        return (200, {}, json.dumps(body))

    responses.add_callback(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        callback=_callback,
        content_type="application/json",
    )
    return calls


@responses.activate
def test_a_probe_result_equal_to_the_cursor_minute_is_a_reindex(client, base_url):
    """Equality is the case the guard was written for, and nothing drove it.

    Narrowing `nxt <= minute` to `nxt < minute` reads this lagging probe as a normal advance:
    the cursor is reassigned to itself, `after_id` resets, and the identical drain-then-probe
    pair repeats forever against a server answering identically each time. Nothing in the loop
    notices, because every individual step looks like progress.

    The existing reindex test cannot reach this — its lagging row is dated two years below the
    cursor, which strict `<` still catches. Reading a reindex correctly matters twice over: it
    is what routes the scan onto `_search_by_id`, the one path that ignores `updated` entirely
    and can therefore still see the whole project while the index and the fields disagree.
    """
    calls = _register_reindexed_server(base_url)

    pages = list(client.search_seek("OPS", after_ts=_CURSOR, page_size=2))

    assert [i["key"] for p in pages for i in p.issues] == ["OPS-1", "OPS-2", "OPS-3"]
    assert all(p.fallback for p in pages)  # tagged as recovery, not as a normal delta page
    assert len(calls) <= _BUDGET
