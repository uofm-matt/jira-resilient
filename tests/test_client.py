"""Unit tests for JiraClient — uses `responses` to mock HTTP, no real network."""

from __future__ import annotations

import pytest
import responses

from jira_resilient import JiraClient, JiraFetchError, JiraParseError


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
        json={"key": "XX-1", "fields": {"summary": "Test"}},
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
        "issues": [{"key": "XX-1", "fields": {"updated": "2026-05-18T07:30:00.000+0000"}}],
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
            {"key": "XX-208847", "fields": {"updated": ts}},
            {"key": "XX-209026", "fields": {"updated": ts}},
            {"key": "XX-209073", "fields": {"updated": ts}},
        ],
        "names": {},
        "schema": {},
    }
    after_page = {
        "issues": [{"key": "XX-300", "fields": {"updated": "2026-05-19T11:53:00.000-0400"}}],
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
def test_search_seek_raises_parse_error_on_missing_updated(client, base_url):
    responses.add(
        responses.POST,
        f"{base_url}/rest/api/2/search",
        json={"issues": [{"key": "XX-1", "fields": {}}]},
    )
    with pytest.raises(JiraParseError):
        list(client.search_seek("XX"))


@responses.activate
def test_get_issue_resilient_full_succeeds(client, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/rest/api/2/issue/XX-1",
        json={"key": "XX-1", "fields": {"summary": "test"}},
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
        json={"key": "XX-1", "fields": {"summary": "ok"}},
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
