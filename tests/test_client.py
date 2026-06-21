"""Unit tests for JiraClient — uses `responses` to mock HTTP, no real network."""

from __future__ import annotations

import json
import operator
import re
from datetime import UTC, datetime

import pytest
import requests
import responses

from jira_resilient import JiraClient, JiraFetchError, JiraParseError
from jira_resilient.exceptions import JiraJqlError


def _fake_jira_delta_search(base_url, dataset, *, calls=None):
    """Register a `responses` callback that faithfully models JIRA Server's delta
    `/search` semantics, so a test can assert the client retrieves every issue.

    Models the REAL semantics: a bare minute literal `"YYYY-MM-DD HH:MM"` is the INSTANT
    `MM:00` for every operator (verified live on JIRA Server DC). The delta scan emits the
    half-open drain `updated >= "MM" AND updated < "MM+1" [AND id > N] ORDER BY id ASC` and
    the advance probe `updated >= "MM+1" ... ORDER BY updated ASC, id ASC` (maxResults 1).
    `calls`, if a list, collects each JQL for loop-bound assertions.
    """
    rows = [(int(d["id"]), d["key"], datetime.fromisoformat(d["updated"])) for d in dataset]

    def _instant(lit: str) -> datetime:  # "2026-05-18 10:01" -> aware instant 10:01:00
        return datetime.fromisoformat(lit + ":00").replace(tzinfo=UTC)

    def _callback(request):
        body = json.loads(request.body)
        jql, max_results = body["jql"], body["maxResults"]
        if calls is not None:
            calls.append(jql)
        ops = {
            ">=": operator.ge,
            "<": operator.lt,
            "<=": operator.le,
            ">": operator.gt,
            "=": operator.eq,
        }
        hits = list(rows)
        for op, lit in re.findall(r'updated (>=|<=|<|>|=) "([\d-]{10} [\d:]{5})"', jql):
            t = _instant(lit)
            hits = [r for r in hits if ops[op](r[2], t)]
        if g := re.search(r"id > (\d+)", jql):
            hits = [r for r in hits if r[0] > int(g.group(1))]
        hits.sort(key=(lambda r: r[0]) if "ORDER BY id ASC" in jql else (lambda r: (r[2], r[0])))
        issues = [
            {"id": str(i), "key": k, "fields": {"updated": u.isoformat()}}
            for i, k, u in hits[:max_results]
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
    return JiraClient(base_url, pat="test", verify=False)


def test_pool_maxsize_threads_through_to_session(base_url):
    """pool_maxsize reaches the session's per-host pool, so concurrent callers can size it."""
    c = JiraClient(base_url, pat="test", verify=False, pool_maxsize=32)
    adapter = c.session.get_adapter("https://example.com")
    assert adapter.poolmanager.connection_pool_kw["maxsize"] == 32


@responses.activate
def test_is_authenticated_ok(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/myself",
        json={"displayName": "Test User"},
        status=200,
    )
    assert client.is_authenticated is True


@responses.activate
def test_is_authenticated_failure(client, base_url):
    responses.add(responses.GET, f"{base_url}/rest/api/2/myself", status=401)
    assert client.is_authenticated is False


@responses.activate
def test_get_issue(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1",
        json={"key": "XX-1", "id": "1", "fields": {"summary": "Test"}},
        status=200,
    )
    issue = client.get_issue("XX-1")
    assert issue["key"] == "XX-1"


@responses.activate
def test_list_keys_paginates(client, base_url):
    """Two pages of 2 keys each + an empty terminating page."""
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [{"key": "XX-1"}, {"key": "XX-2"}], "total": 4},
    )
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [{"key": "XX-3"}, {"key": "XX-4"}], "total": 4},
    )
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json={"issues": [], "total": 4})
    assert client.list_keys('project = "XX"') == ["XX-1", "XX-2", "XX-3", "XX-4"]


@responses.activate
def test_get_changelog_paginates(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/changelog",
        json={"values": [{"id": "1"}, {"id": "2"}], "total": 3},
    )
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/changelog",
        json={"values": [{"id": "3"}], "total": 3},
    )
    assert client.get_changelog("XX-1") == [{"id": "1"}, {"id": "2"}, {"id": "3"}]


@responses.activate
def test_get_changelog_falls_back_to_expand_on_404(client, base_url):
    # JIRA Server lacks the paginated sub-resource (404); fall back to expand=changelog.
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1/changelog", status=404)
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1",
        json={"key": "XX-1", "changelog": {"histories": [{"id": "10"}, {"id": "11"}]}},
    )
    assert client.get_changelog("XX-1") == [{"id": "10"}, {"id": "11"}]

    # Cached: a subsequent issue goes straight to expand — no /changelog mock is
    # registered for XX-2, so any call to the paginated route would raise.
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-2",
        json={"changelog": {"histories": [{"id": "20"}]}},
    )
    assert client.get_changelog("XX-2") == [{"id": "20"}]


@responses.activate
def test_get_changelog_non_404_http_error_propagates(client, base_url):
    # A non-404 HTTP error on the paginated route must NOT silently fall back to
    # expand (no /issue/XX-1 mock is registered, so a fallback would also fail). 403
    # is a fail-fast 4xx (unlike 5xx, which urllib3 retries).
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1/changelog", status=403)
    with pytest.raises(requests.HTTPError):
        client.get_changelog("XX-1")


@responses.activate
def test_get_issuelinks_returns_field_value(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1",
        json={"fields": {"issuelinks": [{"id": "lnk-1"}]}},
    )
    assert client.get_issuelinks("XX-1") == [{"id": "lnk-1"}]


@responses.activate
def test_search_seek_yields_pages(client, base_url):
    p1 = {
        "issues": [
            {"key": "XX-1", "id": "1", "fields": {"updated": "2026-05-18T07:30:00.000+0000"}}
        ],
        "names": {},
        "schema": {},
    }
    p2 = {"issues": [], "names": {}, "schema": {}}
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json=p1)
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json=p2)
    pages = list(client.search_seek("XX"))
    assert len(pages) == 1
    assert pages[0].issues[0]["key"] == "XX-1"


@responses.activate
def test_search_seek_delta_drains_same_minute_cluster_by_id(client, base_url):
    """A same-minute cluster larger than a page must drain fully by id — the case the
    old lexical `key >` cursor silently truncated. Keys here are deliberately
    lexical-misordered vs numeric (`PROJ-2` > `PROJ-10` as strings) and span a
    digit-width boundary (2..30); the id-ordered drain is immune to all of it.
    """
    client._server_tz = UTC
    ts = "2026-05-18T10:00:00.000+0000"
    dataset = [{"id": str(n), "key": f"PROJ-{n}", "updated": ts} for n in range(2, 31)]
    _fake_jira_delta_search(base_url, dataset)

    pages = list(
        client.search_seek("PROJ", after_ts=datetime(2026, 5, 18, 10, 0, tzinfo=UTC), page_size=10)
    )

    got = [i["key"] for p in pages for i in p.issues]
    assert sorted(got) == sorted(d["key"] for d in dataset)  # all 29, none skipped
    assert len(got) == len(set(got)) == 29


@responses.activate
def test_search_seek_delta_no_skip_across_minute_boundary(client, base_url):
    """Regression for the minute-truncation skip a `(updated, id)` tuple cursor still has:
    a row updated later in a minute but with a SMALLER id than the page boundary sorts
    past the boundary yet fails `id > boundary`, so a naive tuple cursor drops it.
    Draining each minute fully by id before advancing keeps it.
    """
    client._server_tz = UTC
    dataset = [
        {"id": "100", "key": "PROJ-100", "updated": "2026-05-18T10:00:00.000+0000"},
        {"id": "200", "key": "PROJ-200", "updated": "2026-05-18T10:01:30.000+0000"},
        # later second (10:01:45) but smaller id than PROJ-200 — the trap row.
        {"id": "150", "key": "PROJ-150", "updated": "2026-05-18T10:01:45.000+0000"},
    ]
    _fake_jira_delta_search(base_url, dataset)

    pages = list(
        client.search_seek("PROJ", after_ts=datetime(2026, 5, 18, 10, 0, tzinfo=UTC), page_size=2)
    )

    got = {i["key"] for p in pages for i in p.issues}
    assert got == {"PROJ-100", "PROJ-200", "PROJ-150"}  # the trap row is not skipped


@responses.activate
def test_search_seek_delta_advances_across_minutes_and_terminates(client, base_url):
    """Several changed minutes, each drained then advanced past via the next-minute
    probe; the scan emits every issue exactly once and terminates (bounded calls)."""
    client._server_tz = UTC
    dataset = [
        {"id": "10", "key": "PROJ-10", "updated": "2026-05-18T10:00:10.000+0000"},
        {"id": "11", "key": "PROJ-11", "updated": "2026-05-18T10:00:50.000+0000"},
        {"id": "12", "key": "PROJ-12", "updated": "2026-05-18T10:03:00.000+0000"},
        {"id": "13", "key": "PROJ-13", "updated": "2026-05-18T10:07:00.000+0000"},
    ]
    calls: list[str] = []
    _fake_jira_delta_search(base_url, dataset, calls=calls)

    pages = list(
        client.search_seek("PROJ", after_ts=datetime(2026, 5, 18, 10, 0, tzinfo=UTC), page_size=20)
    )

    got = [i["key"] for p in pages for i in p.issues]
    assert sorted(got) == ["PROJ-10", "PROJ-11", "PROJ-12", "PROJ-13"]
    assert len(got) == 4  # no duplicates
    assert len(calls) < 20  # bounded — no spin


@responses.activate
def test_search_seek_post_reindex_falls_back_to_id_scan(client, base_url):
    """Post-reindex, the index matches `updated > cursor` but `fields.updated` still
    shows older values. The next-minute probe then returns a row whose `updated` floors
    at/below the cursor minute — the single reindex signal — and the scan falls back to a
    full id scan (which never reads `updated`), tagging those pages `fallback=True`.
    """
    client._server_tz = UTC
    cursor = datetime(2026, 5, 19, 11, 51, tzinfo=UTC)
    stale = "2024-07-14T19:37:16.000+0000"  # floors to 2024 — always behind the 2026 cursor
    universe = [
        {"id": "1", "key": "OPS-1"},
        {"id": "2", "key": "OPS-2"},
        {"id": "3", "key": "OPS-3"},
    ]

    def _callback(request):
        jql = json.loads(request.body)["jql"]
        if "updated < " in jql:  # half-open drain (has both >= and <)
            issues = []  # the cursor minute itself has nothing
        elif "updated >= " in jql:  # advance probe (>= only)
            issues = [{"id": "1", "key": "OPS-1", "fields": {"updated": stale}}]  # lagging row
        else:  # id-ordered fallback scan — no `updated` clause
            after = int(g.group(1)) if (g := re.search(r"id > (\d+)", jql)) else 0
            issues = [
                {"id": u["id"], "key": u["key"], "fields": {"updated": stale}}
                for u in universe
                if int(u["id"]) > after
            ]
        return (200, {}, json.dumps({"issues": issues, "names": {}, "schema": {}}))

    responses.add_callback(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        callback=_callback,
        content_type="application/json",
    )

    pages = list(client.search_seek("OPS", after_ts=cursor, page_size=2))

    got = [i["key"] for p in pages for i in p.issues]
    assert got == ["OPS-1", "OPS-2", "OPS-3"]  # full universe via the id scan
    assert all(p.fallback for p in pages)  # every yielded page is from the fallback


@responses.activate
def test_search_seek_delta_raises_parse_error_on_missing_id(client, base_url):
    """The delta drain advances on `id`, so a drain row missing it is unrecoverable."""
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [{"key": "XX-1", "fields": {"updated": "2026-01-01T00:00:00.000+0000"}}]},
    )
    with pytest.raises(JiraParseError):
        list(client.search_seek("XX", after_ts=datetime(2026, 1, 1, tzinfo=UTC)))


@responses.activate
def test_get_issue_resilient_full_succeeds(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1",
        json={"key": "XX-1", "id": "1", "fields": {"summary": "test"}},
    )
    result = client.get_issue_resilient("XX-1")
    assert result.tier == "full"
    assert result.issue["key"] == "XX-1"


@responses.activate
def test_get_issue_resilient_falls_back_to_minimal(client, base_url, monkeypatch):
    """Both *all and the hub-fetch fail with timeouts → minimal succeeds."""
    import time

    import requests

    monkeypatch.setattr(time, "sleep", lambda _: None)  # skip retry backoff sleeps

    # Tier 1: full + names,schema. fast_fail=max_attempts=2 → 2 timeouts then raise.
    for _ in range(2):
        responses.add(
            responses.GET,
            f"{base_url}/rest/api/2/issue/XX-1",
            body=requests.exceptions.Timeout("simulated"),
        )
    # Tier 2: hub fetch (*all,-issuelinks). max_attempts=2 → 2 timeouts then raise.
    for _ in range(2):
        responses.add(
            responses.GET,
            f"{base_url}/rest/api/2/issue/XX-1",
            body=requests.exceptions.Timeout("simulated"),
        )
    # Tier 3: minimal fields succeeds first try.
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1",
        json={"key": "XX-1", "id": "1", "fields": {"summary": "ok"}},
    )

    result = client.get_issue_resilient("XX-1")
    assert result.tier == "minimal"


@responses.activate
def test_get_issue_resilient_all_fail(client, base_url, monkeypatch):
    import time

    import requests

    monkeypatch.setattr(time, "sleep", lambda _: None)

    # 2 + 2 + 3 = 7 timeouts cover every retry across all three tiers.
    for _ in range(7):
        responses.add(
            responses.GET,
            f"{base_url}/rest/api/2/issue/XX-1",
            body=requests.exceptions.Timeout("simulated"),
        )
    with pytest.raises(JiraFetchError, match="all three fetch tiers failed"):
        client.get_issue_resilient("XX-1")


# ----- resilient search tiers ---------------------------------------------
#
# Mirror the get_issue_resilient pattern at the listing layer. When a hub
# issue with thousands of issuelinks lands in a /search page, the response
# payload can blow the 120s timeout. The seek paginator falls back through
# the same three tiers (full -> hub -> minimal) as the per-key fetch.


@responses.activate
def test_search_seek_default_tier_is_full(client, base_url):
    """Happy path: full tier succeeds, SearchPage.tier == 'full'."""
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={
            "issues": [
                {"key": "XX-1", "id": "1", "fields": {"updated": "2026-05-18T07:30:00.000+0000"}}
            ],
            "names": {},
            "schema": {},
        },
    )
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [], "names": {}, "schema": {}},
    )
    pages = list(client.search_seek("XX"))
    assert len(pages) == 1
    assert pages[0].tier == "full"


@responses.activate
def test_search_seek_falls_back_to_hub(client, base_url, monkeypatch):
    """Full search times out → hub tier succeeds → issuelinks fetched per-issue.

    The hub-tier issuelinks supplement is what makes the difference operationally:
    a 5,000-link hub no longer poisons every page that happens to contain it.
    """
    import time

    import requests

    monkeypatch.setattr(time, "sleep", lambda _: None)

    # Tier 1 (full): 2 attempts, both timeout.
    for _ in range(2):
        responses.add(
            responses.POST,
            f"{base_url}/rest/api/2/search",
            body=requests.exceptions.Timeout("simulated"),
        )
    # Tier 2 (hub): one POST succeeds with an issue lacking `issuelinks`.
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={
            "issues": [
                {"key": "XX-1", "id": "1", "fields": {"updated": "2026-05-18T07:30:00.000+0000"}}
            ],
            "names": {},
            "schema": {},
        },
    )
    # Per-issue issuelinks fetch supplements the hub-tier response.
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1",
        json={"fields": {"issuelinks": [{"id": "9999"}]}},
    )
    # Second page (empty) — at the "full" tier, since the seek loop restarts at tier 1.
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [], "names": {}, "schema": {}},
    )

    pages = list(client.search_seek("XX"))
    assert len(pages) == 1
    assert pages[0].tier == "hub"
    assert pages[0].issues[0]["fields"]["issuelinks"] == [{"id": "9999"}]


@responses.activate
def test_search_seek_falls_back_to_minimal(client, base_url, monkeypatch):
    """Full + hub both time out → minimal tier succeeds.

    Minimal tier loses changelog + custom fields + issuelinks but keeps the seek
    cursor moving. Strictly preferable to a dark delta when the alternative is
    no progress at all.
    """
    import time

    import requests

    monkeypatch.setattr(time, "sleep", lambda _: None)

    # 2 timeouts (full) + 2 timeouts (hub) = 4 timeouts before minimal gets called.
    for _ in range(4):
        responses.add(
            responses.POST,
            f"{base_url}/rest/api/2/search",
            body=requests.exceptions.Timeout("simulated"),
        )
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={
            "issues": [
                {"key": "XX-1", "id": "1", "fields": {"updated": "2026-05-18T07:30:00.000+0000"}}
            ],
            "names": {},
            "schema": {},
        },
    )
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [], "names": {}, "schema": {}},
    )

    pages = list(client.search_seek("XX"))
    assert len(pages) == 1
    assert pages[0].tier == "minimal"


@responses.activate
def test_search_seek_all_tiers_fail_raises(client, base_url, monkeypatch):
    """When even the minimal tier times out, surface a JiraFetchError that names
    all three underlying exceptions — operators need to see the full chain to
    distinguish 'JIRA is completely down' from 'one project has a 10k-link hub'.
    """
    import time

    import requests

    monkeypatch.setattr(time, "sleep", lambda _: None)

    # 2 + 2 + 2 = 6 timeouts cover every retry across all three search tiers.
    for _ in range(6):
        responses.add(
            responses.POST,
            f"{base_url}/rest/api/2/search",
            body=requests.exceptions.Timeout("simulated"),
        )

    with pytest.raises(JiraFetchError, match="all three search tiers failed"):
        list(client.search_seek("XX"))


@responses.activate
def test_search_seek_hub_tier_tolerates_issuelinks_failure(client, base_url, monkeypatch):
    """If the per-issue issuelinks fetch fails for ONE issue, hub tier still
    progresses with `issuelinks=[]` for that issue — better partial data than
    the whole page failing because one hub couldn't serialize its own links.
    """
    import time

    import requests

    monkeypatch.setattr(time, "sleep", lambda _: None)

    # Full tier fails.
    for _ in range(2):
        responses.add(
            responses.POST,
            f"{base_url}/rest/api/2/search",
            body=requests.exceptions.Timeout("simulated"),
        )
    # Hub tier returns one issue.
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={
            "issues": [
                {"key": "XX-1", "id": "1", "fields": {"updated": "2026-05-18T07:30:00.000+0000"}}
            ],
            "names": {},
            "schema": {},
        },
    )
    # issuelinks fetch fails for that one issue (all 2 attempts).
    for _ in range(2):
        responses.add(
            responses.GET,
            f"{base_url}/rest/api/2/issue/XX-1",
            body=requests.exceptions.Timeout("simulated"),
        )
    # Next page empty (back at tier 1).
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [], "names": {}, "schema": {}},
    )

    pages = list(client.search_seek("XX"))
    assert len(pages) == 1
    assert pages[0].tier == "hub"
    # The issuelinks fetch failed, so the key is ABSENT (not a fabricated []), so a caller
    # can distinguish "fetch failed" from "genuinely no links" and not overwrite real links.
    assert "issuelinks" not in pages[0].issues[0]["fields"]


# ----- API inversion (0.2.0): get_issue is resilient by default, raw is the escape hatch ---


@responses.activate
def test_get_issue_routes_through_resilient_tier(client, base_url, monkeypatch):
    """0.2.0: get_issue() now routes through get_issue_resilient. Full tier times
    out, hub tier supplements issuelinks separately, the caller still gets a dict
    back (not a ResilientFetchResult) — the tier degradation is invisible to the
    safe-default caller, logged as a warning for operators.
    """
    import time

    import requests

    monkeypatch.setattr(time, "sleep", lambda _: None)

    # Tier 1 (full) — 2 timeouts.
    for _ in range(2):
        responses.add(
            responses.GET,
            f"{base_url}/rest/api/2/issue/XX-1",
            body=requests.exceptions.Timeout("simulated"),
        )
    # Tier 2 (hub) — succeeds, with separate issuelinks supplement.
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1",
        json={"key": "XX-1", "id": "1", "fields": {"summary": "ok"}},
    )
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1",
        json={"fields": {"issuelinks": [{"id": "1"}]}},
    )

    issue = client.get_issue("XX-1")
    # Plain dict, not ResilientFetchResult — get_issue is the simple-default path.
    assert isinstance(issue, dict)
    assert issue["key"] == "XX-1"
    assert issue["fields"]["issuelinks"] == [{"id": "1"}]


@responses.activate
def test_get_issue_raw_is_unguarded(client, base_url, monkeypatch):
    """get_issue_raw is the explicit escape hatch — no fallback. A single
    Timeout raises directly. Use it when you need precise timeout/field
    control (e.g. autoheal fast-fail probes).
    """
    import time

    import requests

    monkeypatch.setattr(time, "sleep", lambda _: None)

    for _ in range(2):
        responses.add(
            responses.GET,
            f"{base_url}/rest/api/2/issue/XX-1",
            body=requests.exceptions.Timeout("simulated"),
        )

    with pytest.raises(requests.exceptions.Timeout):
        client.get_issue_raw("XX-1", timeout=60, max_attempts=2)


# ----- search_paged tier fallback (0.2.0: now uses the same _search_one_page helper) ---


@responses.activate
def test_search_paged_falls_back_to_hub(client, base_url, monkeypatch):
    """Mirror of test_search_seek_falls_back_to_hub — search_paged now shares
    the same three-tier helper, so a single hub issue in the page no longer
    poisons the offset-paginated path either.
    """
    import time

    import requests

    monkeypatch.setattr(time, "sleep", lambda _: None)

    # Tier 1: full times out.
    for _ in range(2):
        responses.add(
            responses.POST,
            f"{base_url}/rest/api/2/search",
            body=requests.exceptions.Timeout("simulated"),
        )
    # Tier 2: hub succeeds with one issue + a total of 1 (terminates the loop).
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={
            "issues": [{"key": "XX-1", "id": "1", "fields": {}}],
            "names": {},
            "schema": {},
            "total": 1,
        },
    )
    # issuelinks supplement.
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1",
        json={"fields": {"issuelinks": [{"id": "9999"}]}},
    )

    pages = list(client.search_paged("project = XX"))
    assert len(pages) == 1
    assert pages[0].tier == "hub"
    assert pages[0].issues[0]["fields"]["issuelinks"] == [{"id": "9999"}]


# ----- HTTP 400 (JQL error) handling — fast-fail, no cursor recovery -----


@responses.activate
def test_search_400_raises_jql_error_immediately(client, base_url, monkeypatch):
    """JIRA's 400 means the QUERY is bad — same JQL on hub/minimal will fail
    identically. Don't waste round-trips; raise JiraJqlError with the
    `errorMessages` array verbatim so callers can pattern-match.
    """
    import time

    monkeypatch.setattr(time, "sleep", lambda _: None)

    body = {
        "errorMessages": ["An issue with key 'STALE-99' does not exist for field 'key'."],
        "errors": {},
    }
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json=body,
        status=400,
    )

    with pytest.raises(JiraJqlError) as ei:
        client._search_one_page('project = "X" AND key > "STALE-99"', page_size=20)
    assert "STALE-99" in ei.value.error_messages[0]
    # Only ONE request issued — no hub/minimal fall-through.
    assert len(responses.calls) == 1


@responses.activate
def test_search_seek_delta_propagates_jql_400(client, base_url):
    """A 400 during the delta drain (e.g. a malformed extra_filter) is the caller's
    bug — there is no cursor state to clear, so it propagates as JiraJqlError rather
    than looping or being swallowed. The id-keyed cursor cannot itself 400, so the old
    stale-`after_key` recovery path is gone."""
    client._server_tz = UTC
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"errorMessages": ["JQL parse error: malformed clause"]},
        status=400,
    )
    with pytest.raises(JiraJqlError):
        list(client.search_seek("PROJ", after_ts=datetime(2026, 5, 20, tzinfo=UTC)))


@responses.activate
def test_search_seek_full_scans_by_id_ignoring_updated(client, base_url):
    """A full load (no after_ts) pages by issue id ascending and never reads
    `updated` — so it is structurally immune to the minute-precision and
    Lucene-reindex pitfalls that the delta path must work around. The universe
    issues deliberately omit `fields.updated` to prove it isn't consulted."""
    import json
    import re

    seen_jql: list[str] = []
    universe = [{"key": f"P-{i}", "id": str(i), "fields": {}} for i in range(1, 6)]

    def cb(request):
        jql = json.loads(request.body)["jql"]
        seen_jql.append(jql)
        m = re.search(r"id > (\d+)", jql)
        after = int(m.group(1)) if m else 0
        batch = [i for i in universe if int(i["id"]) > after][:2]
        return (200, {}, json.dumps({"issues": batch, "names": {}, "schema": {}}))

    responses.add_callback(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        callback=cb,
        content_type="application/json",
    )
    pages = list(client.search_seek("P"))
    keys = [i["key"] for p in pages for i in p.issues]
    assert keys == ["P-1", "P-2", "P-3", "P-4", "P-5"]
    assert all("ORDER BY id ASC" in j for j in seen_jql)
    assert all("updated" not in j for j in seen_jql)


@responses.activate
def test_server_tz_falls_back_to_utc_on_probe_failure(client, base_url):
    """When the /serverInfo probe fails, server_tz must fall back to UTC — not the
    machine's local timezone. (Regression: the old code used the local TZ, which is
    silently wrong anywhere the host clock isn't UTC, e.g. a non-UTC cloud region.)"""
    from datetime import UTC

    responses.add(responses.GET, f"{base_url}/rest/api/2/serverInfo", status=500)
    assert client.server_tz is UTC


@responses.activate
def test_server_tz_uses_probed_offset(client, base_url):
    """A successful probe adopts the JIRA server's reported offset."""
    from datetime import timedelta

    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/serverInfo",
        json={"serverTime": "2026-05-19T13:00:00.000+0530"},
    )
    assert client.server_tz.utcoffset(None) == timedelta(hours=5, minutes=30)


# ----- watchers / voters --------------------------------------------------------


@responses.activate
def test_get_watchers_returns_watchers_array(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/watchers",
        json={
            "watchCount": 2,
            "isWatching": False,
            "watchers": [
                {"name": "alice", "key": "JIRAUSER1", "active": True},
                {"name": "bob", "key": "JIRAUSER2", "active": True},
            ],
        },
    )
    watchers = client.get_watchers("XX-1")
    assert [w["name"] for w in watchers] == ["alice", "bob"]


@responses.activate
def test_get_watchers_returns_empty_on_404(client, base_url):
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1/watchers", status=404)
    assert client.get_watchers("XX-1") == []


@responses.activate
def test_get_voters_returns_voters_array(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/votes",
        json={"votes": 1, "hasVoted": False, "voters": [{"name": "carol", "key": "JIRAUSER3"}]},
    )
    assert client.get_voters("XX-1") == [{"name": "carol", "key": "JIRAUSER3"}]


@responses.activate
def test_get_voters_empty_when_no_votes(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/votes",
        json={"votes": 0, "voters": [], "hasVoted": False},
    )
    assert client.get_voters("XX-1") == []


@responses.activate
def test_get_voters_returns_empty_on_404(client, base_url):
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1/votes", status=404)
    assert client.get_voters("XX-1") == []


# ----- user ---------------------------------------------------------------------


@responses.activate
def test_get_user_by_username(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/user",
        json={"name": "alice", "key": "JIRAUSER1", "emailAddress": "a@x", "active": True},
    )
    user = client.get_user(username="alice")
    assert user["key"] == "JIRAUSER1"
    assert responses.calls[0].request.params["username"] == "alice"
    # expand defaults to groups,applicationRoles
    assert responses.calls[0].request.params["expand"] == "groups,applicationRoles"


@responses.activate
def test_get_user_by_key_with_expand_none(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/user",
        json={"name": "alice", "key": "JIRAUSER1"},
    )
    client.get_user(key="JIRAUSER1", expand=None)
    assert responses.calls[0].request.params["key"] == "JIRAUSER1"
    assert "expand" not in responses.calls[0].request.params


@responses.activate
def test_get_user_returns_empty_on_404(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/user",
        json={"errorMessages": ["The user named 'nope' does not exist"]},
        status=404,
    )
    assert client.get_user(username="nope") == {}


def test_get_user_requires_an_identifier(client):
    with pytest.raises(ValueError, match="username, key, or account_id"):
        client.get_user()


# ----- entity properties --------------------------------------------------------


@responses.activate
def test_get_issue_properties_lists_then_dereferences(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/properties",
        json={"keys": [{"key": "p.one"}, {"key": "p.two"}]},
    )
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/properties/p.one",
        json={"key": "p.one", "value": {"a": 1}},
    )
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/properties/p.two",
        json={"key": "p.two", "value": "literal"},
    )
    assert client.get_issue_properties("XX-1") == {"p.one": {"a": 1}, "p.two": "literal"}


@responses.activate
def test_get_issue_properties_empty_list(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/properties",
        json={"keys": []},
    )
    assert client.get_issue_properties("XX-1") == {}


@responses.activate
def test_get_issue_properties_404_list_returns_empty(client, base_url):
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1/properties", status=404)
    assert client.get_issue_properties("XX-1") == {}


@responses.activate
def test_get_properties_skips_individual_404(client, base_url):
    """A value that 404s between listing and dereferencing is skipped, not fatal."""
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/properties",
        json={"keys": [{"key": "gone"}, {"key": "here"}]},
    )
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1/properties/gone", status=404)
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/properties/here",
        json={"key": "here", "value": 42},
    )
    assert client.get_issue_properties("XX-1") == {"here": 42}


@responses.activate
def test_get_comment_properties_404_endpoint_returns_empty(client, base_url):
    """Some JIRA Server builds don't expose the comment-properties sub-resource —
    a wholesale 404 must collapse to {} (observed live in production)."""
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/comment/99/properties",
        status=404,
    )
    assert client.get_comment_properties("XX-1", "99") == {}


@responses.activate
def test_get_project_properties_lists_then_dereferences(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/project/PROJ/properties",
        json={"keys": [{"key": "searchRequests"}]},
    )
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/project/PROJ/properties/searchRequests",
        json={"key": "searchRequests", "value": {"ids": []}},
    )
    assert client.get_project_properties("PROJ") == {"searchRequests": {"ids": []}}


@responses.activate
def test_get_comment_properties_lists_then_dereferences(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/comment/99/properties",
        json={"keys": [{"key": "sd.public.comment"}]},
    )
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/comment/99/properties/sd.public.comment",
        json={"key": "sd.public.comment", "value": {"internal": False}},
    )
    assert client.get_comment_properties("XX-1", "99") == {"sd.public.comment": {"internal": False}}


# ----- WP-2: degraded-data observability ----------------------------------


@responses.activate
def test_get_remote_links_raises_on_non_list_body(client, base_url):
    # A 200 whose body is a dict (SSO/proxy error envelope) must NOT be masked as data.
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/remotelink",
        json={"errorMessages": ["not authenticated"]},
    )
    with pytest.raises(JiraParseError):
        client.get_remote_links("XX-1")


@responses.activate
def test_get_remote_links_empty_list_ok(client, base_url):
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1/remotelink", json=[])
    assert client.get_remote_links("XX-1") == []


@responses.activate
def test_get_issue_resilient_fast_fails_on_4xx(client, base_url):
    # A deleted/forbidden issue (404) must fail fast — lower tiers fetch the same key and
    # would 404 identically, so they are NOT attempted.
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/GONE-1", status=404)
    with pytest.raises(JiraFetchError):
        client.get_issue_resilient("GONE-1")
    assert len(responses.calls) == 1  # no hub/minimal retry


@responses.activate
def test_get_worklogs_keeps_paging_when_total_absent(client, base_url):
    # JIRA variants that omit `total`: a FULL first page must not stop the loop (the old
    # `data.get("total", 0)` read absent total as 0 → silent one-page truncation).
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/worklog",
        json={"worklogs": [{"id": "1"}, {"id": "2"}]},  # full page, no total
    )
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1/worklog",
        json={"worklogs": [{"id": "3"}]},  # more, no total
    )
    responses.add(responses.GET, f"{base_url}/rest/api/2/issue/XX-1/worklog", json={"worklogs": []})
    assert client.get_worklogs("XX-1", page_size=2) == [{"id": "1"}, {"id": "2"}, {"id": "3"}]
