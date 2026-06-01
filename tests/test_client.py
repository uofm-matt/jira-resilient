"""Unit tests for JiraClient — uses `responses` to mock HTTP, no real network."""

from __future__ import annotations

from datetime import UTC

import pytest
import responses

from jira_resilient import JiraClient, JiraFetchError, JiraParseError
from jira_resilient.exceptions import JiraJqlError


@pytest.fixture
def client(base_url):
    return JiraClient(base_url, pat="test", verify=False)


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
        "issues": [{"key": "XX-1", "id": "1", "fields": {"updated": "2026-05-18T07:30:00.000+0000"}}],
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
def test_search_seek_breaks_n_cycle_inside_same_minute(client, base_url):
    """Regression: a 3-page cycle within one minute (JQL minute-precision matching every
    issue in the minute regardless of after_key) used to spin forever. The deque-based
    cycle detector forces a minute-advance once a boundary repeats within the last N pages.
    """
    ts = "2026-05-19T11:51:07.000-0400"
    cycle_page = {
        "issues": [
            {"key": "XX-208847", "id": "208847", "fields": {"updated": ts}},
            {"key": "XX-209026", "id": "209026", "fields": {"updated": ts}},
            {"key": "XX-209073", "id": "209073", "fields": {"updated": ts}},
        ],
        "names": {},
        "schema": {},
    }
    after_page = {
        "issues": [{"key": "XX-300", "id": "300", "fields": {"updated": "2026-05-19T11:53:00.000-0400"}}],
        "names": {},
        "schema": {},
    }
    empty_page = {"issues": [], "names": {}, "schema": {}}
    # Same page returned 15 times — would loop forever pre-fix. After the deque trips,
    # post-fix the loop advances a minute, JIRA returns `after_page`, then empty → done.
    for _ in range(15):
        responses.add(responses.POST, f"{base_url}/rest/api/2/search", json=cycle_page)
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json=after_page)
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json=empty_page)

    from datetime import datetime

    pages = list(
        client.search_seek(
            "XX",
            after_ts=datetime.fromisoformat("2026-05-19T11:51:00+00:00"),
            after_key="XX-200000",
        )
    )
    # Pre-fix: spins forever → test times out. Post-fix: a bounded number of pages.
    assert any(p.issues[0]["key"] == "XX-300" for p in pages), (
        "should advance past the cycling minute to reach XX-300"
    )


@responses.activate
def test_search_seek_post_reindex_falls_back_to_key_only(client, base_url):
    """Regression: after a JIRA server-side reindex, all project issues are re-indexed
    at the reindex timestamp but fields.updated still shows original (older) values.
    The seek JQL `updated >= "X"` matches everything regardless of X, so minute-bumping
    never advances new_ts past after_ts — stale cycle detection fires after 3 consecutive
    stale minute-advances and switches to key-only pagination, which terminates by
    exhausting all keys.
    """
    from datetime import datetime

    # after_ts is in 2026; all cluster issues have original updated_at from 2024
    after_ts = datetime(2026, 5, 19, 11, 51, tzinfo=UTC)
    ts_old = "2024-07-14T19:37:16.000+0000"  # original, always behind after_ts

    # JIRA returns the same 2-issue page regardless of updated >= "X" filter
    # (Lucene sees them as recently indexed; fields.updated is unchanged/old).
    stale_page = {
        "issues": [
            {"key": "OPS-1", "id": "1", "fields": {"updated": ts_old}},
            {"key": "OPS-2", "id": "2", "fields": {"updated": ts_old}},
        ],
        "names": {},
        "schema": {},
    }
    # After switching to key-only, JIRA returns a later page by key
    key_only_page = {
        "issues": [{"key": "OPS-99", "id": "99", "fields": {"updated": ts_old}}],
        "names": {},
        "schema": {},
    }
    empty_page = {"issues": [], "names": {}, "schema": {}}

    # Stale-cycle pattern: each cycle is 2 pages (page N, then repeat detected on page N+1).
    # 3 cycles x 2 pages = 6 stale pages before the key-only switch fires.
    # Add a few extra to simulate the pre-switch pages being yielded each cycle.
    for _ in range(20):
        responses.add(responses.POST, f"{base_url}/rest/api/2/search", json=stale_page)
    # Key-only response (the fallback scan)
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json=key_only_page)
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json=empty_page)

    pages = list(client.search_seek("OPS", after_ts=after_ts, page_size=2))

    # Should terminate in a bounded number of pages, not consume all 20 stale mocks.
    all_keys = [i["key"] for p in pages for i in p.issues]
    assert "OPS-99" in all_keys, "should reach the key-only fallback page with OPS-99"
    # Stale pages are yielded during the detection window; key-only page is also included.
    assert len(pages) < 20, f"should terminate well before 20 pages, got {len(pages)}"


@responses.activate
def test_search_seek_post_reindex_long_cycle_falls_back_to_key_only(client, base_url):
    """Long-cycle Lucene-reindex regression: cycle length (12 pages) exceeds the
    deque window (maxlen=10) so stale_cycles never fires and every page is yielded.
    Instead, duplicate-key detection notices that the restarted cycle re-yields
    already-seen keys and triggers key-only fallback after 3 consecutive
    all-duplicate pages. This path is immune to JIRA's lexical key ordering, which
    makes a numeric key cursor unsound (suffixes are non-monotonic: -1, -10, -2…).
    """

    def make_page(key: str, hour: int) -> dict:
        ts = f"2024-01-01T{hour:02d}:00:00.000+0000"
        issue = {"key": key, "id": key.rsplit("-", 1)[-1], "fields": {"updated": ts}}
        return {"issues": [issue], "names": {}, "schema": {}}

    # 12 issues, each with a distinct timestamp spaced hours apart.
    # Cycle length = 12 pages (page_size=1) — exceeds deque maxlen=10 so the
    # (ts, key) boundary for page 1 is evicted before cycle 2 starts.
    cycle_pages = [make_page(f"OPS-{i}", i) for i in range(1, 13)]

    key_only_page = make_page("OPS-99", 1)
    empty_page = {"issues": [], "names": {}, "schema": {}}

    # Cycle 1: 12 pages — populates seen_keys with OPS-1..OPS-12
    for p in cycle_pages:
        responses.add(responses.POST, f"{base_url}/rest/api/2/search", json=p)
    # Cycle 2: 3 all-duplicate pages (OPS-1, OPS-2, OPS-3) — triggers fallback on page 3
    for p in cycle_pages[:3]:
        responses.add(responses.POST, f"{base_url}/rest/api/2/search", json=p)
    # Key-only scan terminates normally
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json=key_only_page)
    responses.add(responses.POST, f"{base_url}/rest/api/2/search", json=empty_page)

    pages = list(client.search_seek("OPS", page_size=1))

    all_keys = [i["key"] for p in pages for i in p.issues]
    assert "OPS-99" in all_keys, "should reach key-only fallback page with OPS-99"
    # Cycle 1 (12) + 3 regression pages (all yielded before fallback triggers) + 1 key-only = 16
    assert len(pages) == 16, f"expected 16 pages, got {len(pages)}"


@responses.activate
def test_search_seek_raises_parse_error_on_missing_updated(client, base_url):
    """The delta path cursors on `updated`, so a page missing it is unrecoverable.
    (A full scan cursors on `id` and never reads `updated`, so this guard is
    delta-only — hence the after_ts.)"""
    from datetime import datetime

    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [{"key": "XX-1", "id": "1", "fields": {}}]},
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
            "issues": [{"key": "XX-1", "id": "1", "fields": {"updated": "2026-05-18T07:30:00.000+0000"}}],
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
            "issues": [{"key": "XX-1", "id": "1", "fields": {"updated": "2026-05-18T07:30:00.000+0000"}}],
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
            "issues": [{"key": "XX-1", "id": "1", "fields": {"updated": "2026-05-18T07:30:00.000+0000"}}],
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
            "issues": [{"key": "XX-1", "id": "1", "fields": {"updated": "2026-05-18T07:30:00.000+0000"}}],
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
    # The issuelinks fetch failed, so we get an empty list, not a missing key.
    assert pages[0].issues[0]["fields"]["issuelinks"] == []


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


# ----- HTTP 400 (JQL error) handling — fast-fail + stale-key auto-recovery ---


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
def test_search_seek_recovers_from_stale_after_key(client, base_url, monkeypatch):
    """End-to-end: seek loop sees 400 'key X does not exist', clears its stale
    tiebreaker, retries the same window without `key > X`, and continues. This
    is the PROJ-style operator pain point — without auto-recovery the loop
    would 400 forever once an admin deleted a previous cycle's last_seen_key.
    """
    import time
    from datetime import UTC, datetime

    monkeypatch.setattr(time, "sleep", lambda _: None)
    client._server_tz = UTC  # skip the /serverInfo probe (not mocked here)

    # First call (with after_key=STALE-99): 400 "key does not exist"
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"errorMessages": ["An issue with key 'STALE-99' does not exist for field 'key'."]},
        status=400,
    )
    # Second call (after_key cleared): succeeds with one issue, then empty page terminates.
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={
            "issues": [{"key": "OK-1", "id": "1", "fields": {"updated": "2026-05-20T07:00:00.000+0000"}}],
            "names": {},
            "schema": {},
        },
    )
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [], "names": {}, "schema": {}},
    )

    pages = list(
        client.search_seek(
            "PROJ",
            after_ts=datetime.fromisoformat("2026-05-20T06:00:00+00:00"),
            after_key="STALE-99",
        )
    )
    # The loop recovered: one page yielded, three total POSTs (400, recovery success, empty).
    assert len(pages) == 1
    assert pages[0].issues[0]["key"] == "OK-1"
    assert len(responses.calls) == 3


@responses.activate
def test_search_seek_recovers_from_moved_issue_key(client, base_url, monkeypatch):
    """Variant: JIRA returns 'Operator '>' cannot be applied to moved issue
    key 'X'' when after_key references a reprojected issue. Hit live on
    a project (PROJ-1234 was moved between cycles). Same recovery as the
    deleted-key variant — clear after_key, retry."""
    import time
    from datetime import UTC, datetime

    monkeypatch.setattr(time, "sleep", lambda _: None)
    client._server_tz = UTC

    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"errorMessages": ["Operator '>' cannot be applied to moved issue key 'MOVED-42'."]},
        status=400,
    )
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={
            "issues": [{"key": "OK-1", "id": "1", "fields": {"updated": "2026-05-20T07:00:00.000+0000"}}],
            "names": {},
            "schema": {},
        },
    )
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [], "names": {}, "schema": {}},
    )

    pages = list(
        client.search_seek("PROJ", after_ts=datetime(2026, 5, 20, tzinfo=UTC), after_key="MOVED-42")
    )
    assert len(pages) == 1
    assert pages[0].issues[0]["key"] == "OK-1"


@responses.activate
def test_search_seek_recovers_from_invalid_key_format(client, base_url, monkeypatch):
    """Variant: JIRA returns 'The issue key 'X' for field 'key' is invalid'
    when after_key is malformed (no dash, etc.). Defensive — shouldn't
    happen naturally from seek pagination, but if cursor data ever gets
    corrupted, recover instead of looping forever."""
    import time
    from datetime import UTC, datetime

    monkeypatch.setattr(time, "sleep", lambda _: None)
    client._server_tz = UTC

    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"errorMessages": ["The issue key 'BROKEN' for field 'key' is invalid."]},
        status=400,
    )
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={
            "issues": [{"key": "OK-1", "id": "1", "fields": {"updated": "2026-05-20T07:00:00.000+0000"}}],
            "names": {},
            "schema": {},
        },
    )
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [], "names": {}, "schema": {}},
    )

    pages = list(
        client.search_seek("PROJ", after_ts=datetime(2026, 5, 20, tzinfo=UTC), after_key="BROKEN")
    )
    assert len(pages) == 1


@responses.activate
def test_search_seek_propagates_400_when_after_key_is_none(client, base_url, monkeypatch):
    """If the stale-key error fires when there's no after_key to clear, the
    auto-recovery has nothing to do — propagate the error rather than looping.
    Otherwise a JQL bug elsewhere (e.g. malformed extra_filter) would silently
    hang the loop forever."""
    import time
    from datetime import UTC

    monkeypatch.setattr(time, "sleep", lambda _: None)
    client._server_tz = UTC

    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"errorMessages": ["JQL parse error: malformed clause"]},
        status=400,
    )
    with pytest.raises(JiraJqlError):
        list(client.search_seek("PROJ"))  # no after_key set


@responses.activate
def test_search_seek_fallback_scans_by_id_not_key(client, base_url):
    """The post-reindex fallback must page by issue id, not key.

    JIRA evaluates `key > "X"` numerically (by issue number) but `ORDER BY key ASC`
    lexically; mixing them makes a key cursor re-fetch the same page forever
    (PROJ-1, PROJ-10, PROJ-100 sort before PROJ-2 lexically, yet a numeric
    `key >` excludes them). This simulates that split semantics with a callback and
    asserts the fallback advances by id and surfaces every issue. A key cursor would
    loop on PROJ-1/PROJ-10 and never reach PROJ-2 or PROJ-20.
    """
    import json
    import re
    from datetime import UTC, datetime

    # Lexically-scrambled keys with monotonic ids — the shape a key cursor breaks on.
    universe = [
        {"key": "PROJ-1", "id": "100", "fields": {"updated": "2024-01-01T00:00:00.000+0000"}},
        {"key": "PROJ-10", "id": "101", "fields": {"updated": "2024-01-01T01:00:00.000+0000"}},
        {"key": "PROJ-100", "id": "102", "fields": {"updated": "2024-01-01T02:00:00.000+0000"}},
        {"key": "PROJ-2", "id": "103", "fields": {"updated": "2024-01-01T03:00:00.000+0000"}},
        {"key": "PROJ-20", "id": "104", "fields": {"updated": "2024-01-01T04:00:00.000+0000"}},
    ]

    def cb(request):
        jql = json.loads(request.body)["jql"]
        if "ORDER BY id ASC" in jql:  # fallback phase: honour the numeric id cursor
            assert "ORDER BY key" not in jql, "fallback must not order by key"
            m = re.search(r"id > (\d+)", jql)
            after = int(m.group(1)) if m else 0
            batch = [i for i in universe if int(i["id"]) > after][:2]
            return (200, {}, json.dumps({"issues": batch, "names": {}, "schema": {}}))
        # Time-cursor phase: simulate the reindex loop — same two issues every call.
        return (200, {}, json.dumps({"issues": universe[:2], "names": {}, "schema": {}}))

    responses.add_callback(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        callback=cb,
        content_type="application/json",
    )

    pages = list(client.search_seek("PROJ", after_ts=datetime(2026, 1, 1, tzinfo=UTC), page_size=2))
    keys = {i["key"] for p in pages for i in p.issues}
    assert keys == {"PROJ-1", "PROJ-10", "PROJ-100", "PROJ-2", "PROJ-20"}
    # The flag lets incremental callers tell recovery pages apart from the time
    # cursor: the first page is time-cursor (False), the recovery scan is True.
    assert pages[0].fallback is False
    assert pages[-1].fallback is True


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
        responses.POST, f"{base_url}/rest/api/2/search", callback=cb, content_type="application/json"
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
        json={"serverTime": "2026-05-19T13:00:00.000-0400"},
    )
    assert client.server_tz.utcoffset(None) == timedelta(hours=-4)
