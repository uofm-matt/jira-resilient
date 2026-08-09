"""The CLI's job is to make the tier ladder visible, so that is what these assert.

Deliberately NOT tested: that argparse parses arguments. `probe` earns its place only if it
reports what each attempt cost against a server that degrades, so every test here drives a
real `JiraClient` through mocked HTTP and reads what the user would see on stdout.

The heartbeat tests need requests that actually take wall-clock time, so they use a
`responses` callback that blocks on an `Event` — `time.sleep` is stubbed out for the whole
module (the retry backoff would otherwise cost minutes) and would make a slow attempt fast.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
import requests
import responses

from jira_resilient import cli
from jira_resilient.cli import _STEP_LABEL, main

_ISSUE = "https://jira.example.com/rest/api/2/issue/HUB-1"
_ENV = {"JIRA_URL": "https://jira.example.com", "JIRA_PAT": "secret-token"}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(time, "sleep", lambda _: None)


class _Tty(io.StringIO):
    """A stderr that claims to be a terminal, which is what switches the heartbeat on."""

    def isatty(self) -> bool:
        return True


def _terminal_stderr(monkeypatch: pytest.MonkeyPatch) -> _Tty:
    """Give `probe` a terminal-shaped stderr, and a tick fast enough for a 0.15s request.

    Called from the test body rather than supplied as a fixture on purpose: pytest re-installs
    its own `sys.stdout`/`sys.stderr` when it resumes capture for the call phase, so the same
    patch applied during fixture setup is silently undone before the test runs.
    """
    stream = _Tty()
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(cli, "_TICK_SECONDS", 0.01)
    return stream


def _slow(delay: float, result: Any) -> Callable[[Any], Any]:
    """A `responses` callback that blocks for `delay` and then succeeds — or, when handed an
    exception, raises it (which is how `responses` models a transport failure)."""

    def callback(_request: Any) -> Any:
        threading.Event().wait(delay)
        if isinstance(result, Exception):
            return result
        return (200, {}, json.dumps(result))

    return callback


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


@responses.activate
def test_the_heartbeat_ticks_while_a_request_is_still_in_flight(env, monkeypatch):
    """The measured failure this exists for: 504s inside one attempt, nothing on the terminal.

    The attempt records fire at COMPLETION, so during the slowest thing `probe` does there is
    nothing to report but the clock — and a clock that moves is the whole difference between
    "working" and "hung".
    """
    tty = _terminal_stderr(monkeypatch)
    responses.add_callback(
        responses.GET,
        _ISSUE,
        callback=_slow(0.15, {"key": "HUB-1", "fields": {}}),
        content_type="application/json",
    )

    assert main(["probe", "HUB-1"]) == 0
    ticks = tty.getvalue()
    assert ticks.count("\r") > 2  # redrawn in place, repeatedly, during one request
    assert "elapsed" in ticks and "in flight" in ticks


@responses.activate
def test_the_heartbeat_reports_only_what_it_can_know(env, monkeypatch):
    """Before a rung lands it says so, and after one lands it names that rung — never the one
    currently running, which the ladder does not announce until it is over."""
    tty = _terminal_stderr(monkeypatch)
    responses.add_callback(
        responses.GET,
        _ISSUE,
        callback=_slow(0.15, requests.exceptions.Timeout("read timeout")),
        content_type="application/json",
    )
    responses.add(responses.GET, _ISSUE, body=requests.exceptions.Timeout("read timeout"))
    responses.add(responses.GET, _ISSUE, json={"key": "HUB-1", "fields": {}})
    responses.add_callback(
        responses.GET,
        _ISSUE,
        callback=_slow(0.15, {"fields": {"issuelinks": []}}),
        content_type="application/json",
    )

    assert main(["probe", "HUB-1"]) == 0
    ticks = tty.getvalue()
    assert "nothing has finished yet" in ticks  # drawn during the tier-1 attempt
    assert "last rung: Hub base request" in ticks  # drawn during the links-only fetch
    # Guard the guard: assert the label EXISTS before asserting the heartbeat omits it,
    # or a rename turns this into a test that passes while checking nothing.
    in_flight = _STEP_LABEL["hub-links"]
    assert in_flight not in ticks, "heartbeat claimed a rung that had not finished"


@responses.activate
def test_each_rung_prints_before_the_next_request_is_issued(env, capsys):
    """Streaming, asserted from inside the ladder rather than after it.

    Reading stdout from a mid-ladder request is the only way to tell "printed as it happened"
    from "printed at the end in the same order".
    """
    mid_ladder: list[str] = []
    for _ in range(2):
        responses.add(responses.GET, _ISSUE, body=requests.exceptions.Timeout("read timeout"))

    def watch(_request):
        mid_ladder.append(capsys.readouterr().out)
        return (200, {}, json.dumps({"key": "HUB-1", "fields": {}}))

    responses.add_callback(responses.GET, _ISSUE, callback=watch, content_type="application/json")
    responses.add(responses.GET, _ISSUE, json={"fields": {"issuelinks": []}})

    assert main(["probe", "HUB-1"]) == 0
    assert "Full request:" in mid_ladder[0]
    assert "Timeout" in mid_ladder[0]


@responses.activate
def test_the_heartbeat_stays_off_when_stderr_is_not_a_terminal(env, capsys):
    """A carriage-returned line is noise in a log file. The report still streams to stdout —
    that is the durable record, and it does not depend on anyone watching."""
    responses.add_callback(
        responses.GET,
        _ISSUE,
        callback=_slow(0.05, {"key": "HUB-1", "fields": {}}),
        content_type="application/json",
    )

    assert main(["probe", "HUB-1"]) == 0
    captured = capsys.readouterr()
    assert "\r" not in captured.err
    assert "Full request:" in captured.out


@responses.activate
def test_a_rung_that_succeeded_is_not_reported_as_failed_when_a_later_one_fails(env, capsys):
    """The all-tiers-failed path used to render every record as `failed`, so a hub base that
    came back in 15s read as a failure. One renderer for both paths is why it cannot now."""
    for _ in range(2):  # tier 1: two attempts, both time out
        responses.add(responses.GET, _ISSUE, body=requests.exceptions.Timeout("read timeout"))
    responses.add(responses.GET, _ISSUE, json={"key": "HUB-1", "fields": {}})  # hub base: OK
    for _ in range(5):  # links-only (2) then minimal (3), all timing out
        responses.add(responses.GET, _ISSUE, body=requests.exceptions.Timeout("read timeout"))

    assert main(["probe", "HUB-1"]) == 1
    out = capsys.readouterr().out
    assert "Hub base request:" in out and "success in" in out
    assert "All tiers failed" in out


def test_the_http_layer_s_retry_warnings_still_reach_the_console(env):
    """Collecting the attempt records means lowering the library logger to INFO, and the fix
    for the INFO records then leaking is to pin the console handler at WARNING. Both halves
    are asserted here: the retry notices — the only output that explains WHY a request is
    slow — must survive that pinning, and the raw INFO records must not.
    """
    console = io.StringIO()
    handler = logging.StreamHandler(console)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, _ISSUE, status=500)
            rsps.add(responses.GET, _ISSUE, json={"key": "HUB-1", "fields": {}})
            assert main(["probe", "HUB-1"]) == 0
    finally:
        root.removeHandler(handler)

    printed = console.getvalue()
    # Asserts the LEVEL reaches the console, not the wording — http.py is free to
    # rephrase. Pinning the message text is the coupling _AttemptLog exists to avoid.
    assert "WARNING" in printed
    assert "INFO" not in printed
