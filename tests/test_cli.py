"""The CLI's job is to make the tier ladder visible, so that is what these assert.

Deliberately NOT tested: that argparse parses arguments. `probe` earns its place only if it
reports what each attempt cost against a server that degrades, so every test here drives a
real `JiraClient` through mocked HTTP and reads what the user would see on stdout.
"""

from __future__ import annotations

import time

import pytest
import requests
import responses

from jira_resilient.cli import main

_ISSUE = "https://jira.example.com/rest/api/2/issue/HUB-1"
_ENV = {"JIRA_URL": "https://jira.example.com", "JIRA_PAT": "secret-token"}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(time, "sleep", lambda _: None)


@responses.activate
def test_probe_reports_every_rung_of_the_ladder_it_climbed(env, capsys):
    """The whole point: a hub issue shows the full attempt failing and the split succeeding.

    Without the per-attempt records this reads only `Tier: hub` — true, and silent about the
    timeout that preceded it, which is the cost the library exists to absorb.
    """
    # The full tier retries once (max_attempts=2), so BOTH of its attempts must time out
    # before the ladder descends — one timeout is simply retried into a success.
    for _ in range(2):
        responses.add(responses.GET, _ISSUE, body=requests.exceptions.Timeout("read timeout"))
    responses.add(responses.GET, _ISSUE, json={"key": "HUB-1", "fields": {}})
    responses.add(responses.GET, _ISSUE, json={"fields": {"issuelinks": [{"id": "1"}] * 4832}})

    assert main(["probe", "HUB-1"]) == 0
    out = capsys.readouterr().out
    assert "Full request:" in out and "Timeout" in out
    assert "Hub base request:" in out and "success" in out
    assert "Links-only request:" in out
    assert "Tier:   hub" in out
    assert "Links:  4,832" in out
    assert "degraded to the 'hub' tier" in out


@responses.activate
def test_probe_says_plainly_when_nothing_degraded(env, capsys):
    """An honest negative result. Someone whose issues all come back `full` should be told
    they do not need this library, not sold one."""
    responses.add(responses.GET, _ISSUE, json={"key": "HUB-1", "fields": {"summary": "ok"}})

    assert main(["probe", "HUB-1"]) == 0
    out = capsys.readouterr().out
    assert "Tier:   full" in out
    assert "degraded" not in out


@responses.activate
def test_probe_exits_nonzero_when_every_tier_fails(env, capsys):
    """Exit codes are the CLI's contract with a shell; a failed probe must not report success."""
    for _ in range(6):
        responses.add(responses.GET, _ISSUE, body=requests.exceptions.Timeout("read timeout"))

    assert main(["probe", "HUB-1"]) == 1
    assert "All tiers failed" in capsys.readouterr().out


@responses.activate
def test_scan_reports_the_tier_distribution_it_actually_saw(env, capsys):
    """A per-project answer rather than a per-issue one: which fraction of a project is
    pathological, and whether any of it came back lossy."""
    search = "https://jira.example.com/rest/api/2/search"
    issues = [{"id": str(i), "key": f"P-{i}", "fields": {}} for i in (1, 2)]
    responses.add(responses.POST, search, json={"issues": issues, "names": {}, "schema": {}})
    responses.add(responses.POST, search, json={"issues": [], "names": {}, "schema": {}})

    assert main(["scan", "PROJ", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    # Not pinning column widths — the claim is the count and the share, not the padding.
    assert "Issues:  2" in out
    assert "full" in out and "100.0%" in out


def test_the_pat_is_never_an_argument(env, capsys):
    """A PAT passed on the command line lands in shell history and the process table, so the
    parser must not offer anywhere to put one."""
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "--pat" not in capsys.readouterr().out


def test_a_missing_token_fails_before_any_request(monkeypatch):
    """No token means no request attempted — not an auth round-trip that leaks the URL."""
    monkeypatch.setenv("JIRA_URL", _ENV["JIRA_URL"])
    monkeypatch.delenv("JIRA_PAT", raising=False)
    with pytest.raises(SystemExit, match="JIRA_PAT"):
        main(["probe", "HUB-1"])


def test_the_collector_leaves_the_library_logger_as_it_found_it(env):
    """The handler is installed on a library logger the calling process may also be using;
    leaving it attached would duplicate every later record into a dead collector."""
    import logging

    log = logging.getLogger("jira_resilient")
    before_handlers, before_level = list(log.handlers), log.level
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, _ISSUE, json={"key": "HUB-1", "fields": {}})
        main(["probe", "HUB-1"])
    assert log.handlers == before_handlers
    assert log.level == before_level


@responses.activate
def test_probe_does_not_echo_its_own_instrumentation(env, capsys):
    """`probe` renders the ladder FROM the attempt records, so it must not also print them.

    Collecting them requires lowering the library logger to INFO, and propagation is gated by
    handler levels rather than ancestor logger levels — so without pinning the console handler
    the raw records appear above the report they were gathered to produce.
    """
    for _ in range(2):
        responses.add(responses.GET, _ISSUE, body=requests.exceptions.Timeout("read timeout"))
    responses.add(responses.GET, _ISSUE, json={"key": "HUB-1", "fields": {}})
    responses.add(responses.GET, _ISSUE, json={"fields": {"issuelinks": []}})

    main(["probe", "HUB-1"])
    captured = capsys.readouterr()
    assert "INFO" not in captured.out + captured.err
    assert "Hub base request:" in captured.out


@responses.activate
def test_scan_flags_lossy_pages_loudly(env, capsys):
    """A minimal-tier page means description and custom fields are GONE from those rows.

    A scan that reported only counts would look like a success; the whole reason `tier` is on
    SearchPage is so an operator learns their extract is incomplete.
    """
    search = "https://jira.example.com/rest/api/2/search"
    for _ in range(4):  # exhaust full and hub tiers (2 attempts each)
        responses.add(responses.POST, search, status=500)
    responses.add(
        responses.POST,
        search,
        json={"issues": [{"id": "1", "key": "P-1", "fields": {}}], "names": {}, "schema": {}},
    )
    responses.add(responses.POST, search, json={"issues": [], "names": {}, "schema": {}})

    assert main(["scan", "PROJ", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "minimal" in out
    assert "LOSSY" in out


@responses.activate
def test_scan_exits_nonzero_when_the_scan_fails(env, capsys):
    """A partial extract that exits 0 is worse than one that exits 1 — a shell loop would
    treat it as complete."""
    search = "https://jira.example.com/rest/api/2/search"
    for _ in range(6):
        responses.add(responses.POST, search, status=500)

    assert main(["scan", "PROJ"]) == 1
    assert "Scan failed" in capsys.readouterr().out
