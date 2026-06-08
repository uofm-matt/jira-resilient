"""Unit tests for HTTP foundation: session config + retry logic."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
import requests
import responses

from jira_resilient.http import (
    _MAX_WAIT_SECONDS,
    _RETRY,
    _retry_after_seconds,
    make_session,
    request_with_retry,
)


def test_make_session_sets_bearer_auth():
    sess = make_session(pat="secret-pat")
    assert sess.headers["Authorization"] == "Bearer secret-pat"


def test_make_session_no_pat_skips_auth_header():
    sess = make_session(pat=None)
    assert "Authorization" not in sess.headers


def test_make_session_mounts_tls_adapter():
    sess = make_session(pat="x")
    # Both schemes should resolve to the same _TLSAdapter instance.
    https_adapter = sess.get_adapter("https://example.com")
    http_adapter = sess.get_adapter("http://example.com")
    assert https_adapter is http_adapter
    assert https_adapter.max_retries is _RETRY


@responses.activate
def test_request_with_retry_succeeds_first_try():
    responses.add(responses.GET, "https://x/foo", json={"ok": True}, status=200)
    sess = make_session(pat="x")
    resp = request_with_retry(sess, "GET", "https://x/foo")
    assert resp.json() == {"ok": True}


@responses.activate
def test_request_with_retry_raises_4xx_immediately(monkeypatch):
    """No exponential wait should happen for a 404 — fail-fast is the whole point."""
    responses.add(responses.GET, "https://x/foo", status=404)
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    sess = make_session(pat="x")
    with pytest.raises(requests.exceptions.HTTPError):
        request_with_retry(sess, "GET", "https://x/foo")
    assert sleeps == [], "4xx must not trigger application-level retry sleep"


@responses.activate
def test_request_with_retry_retries_on_network_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    responses.add(responses.GET, "https://x/foo", body=requests.exceptions.ConnectionError("boom"))
    responses.add(responses.GET, "https://x/foo", json={"ok": True}, status=200)
    sess = make_session(pat="x")
    assert request_with_retry(sess, "GET", "https://x/foo").json() == {"ok": True}


@responses.activate
def test_429_honors_retry_after_seconds(monkeypatch):
    """A 429 carrying `Retry-After: N` sleeps exactly N, not the blind 60*2**attempt."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    responses.add(responses.GET, "https://x/foo", status=429, headers={"Retry-After": "7"})
    responses.add(responses.GET, "https://x/foo", json={"ok": True}, status=200)
    assert request_with_retry(make_session(pat="x"), "GET", "https://x/foo").json() == {"ok": True}
    assert sleeps == [7.0]


@responses.activate
def test_429_without_retry_after_falls_back_to_backoff(monkeypatch):
    """No Retry-After header → the exponential schedule (60 on the first attempt)."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    responses.add(responses.GET, "https://x/foo", status=429)
    responses.add(responses.GET, "https://x/foo", json={"ok": True}, status=200)
    assert request_with_retry(make_session(pat="x"), "GET", "https://x/foo").json() == {"ok": True}
    assert sleeps == [60]


@responses.activate
def test_429_caps_pathological_retry_after(monkeypatch):
    """A hostile/buggy huge Retry-After is clamped to the per-sleep ceiling."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    responses.add(responses.GET, "https://x/foo", status=429, headers={"Retry-After": "99999"})
    responses.add(responses.GET, "https://x/foo", json={"ok": True}, status=200)
    request_with_retry(make_session(pat="x"), "GET", "https://x/foo")
    assert sleeps == [_MAX_WAIT_SECONDS]


def _resp_with(retry_after: str | None) -> requests.Response:
    resp = requests.Response()
    if retry_after is not None:
        resp.headers["Retry-After"] = retry_after
    return resp


def test_retry_after_seconds_parses_delta():
    assert _retry_after_seconds(_resp_with("12")) == 12.0


def test_retry_after_seconds_absent_is_none():
    assert _retry_after_seconds(_resp_with(None)) is None


def test_retry_after_seconds_garbage_is_none():
    assert _retry_after_seconds(_resp_with("soon-ish")) is None


def test_retry_after_seconds_http_date_future():
    header = format_datetime(datetime.now(UTC) + timedelta(seconds=120))
    assert _retry_after_seconds(_resp_with(header)) == pytest.approx(120, abs=5)


def test_retry_after_seconds_http_date_past_clamps_zero():
    header = format_datetime(datetime.now(UTC) - timedelta(seconds=120))
    assert _retry_after_seconds(_resp_with(header)) == 0.0
