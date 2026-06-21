"""Unit tests for HTTP foundation: session config + retry logic."""

from __future__ import annotations

import ssl
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
import requests
import responses

from jira_resilient.http import (
    _BLOCK_ALL_COOKIES,
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


def test_make_session_pool_maxsize_default_is_10():
    sess = make_session(pat="x")
    adapter = sess.get_adapter("https://example.com")
    assert adapter.poolmanager.connection_pool_kw["maxsize"] == 10


def test_make_session_pool_maxsize_sizes_the_pool():
    """A caller fanning N concurrent requests through one session must be able to size the
    per-host pool to N — else urllib3 caps live connections at 10 and churns the surplus."""
    sess = make_session(pat="x", pool_maxsize=48)
    adapter = sess.get_adapter("https://example.com")
    assert adapter.poolmanager.connection_pool_kw["maxsize"] == 48


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


# ----- WP-3: HTTP / TLS / session hardening --------------------------------


@responses.activate
def test_redirect_is_not_followed(monkeypatch):
    """A 3xx on a REST call means a proxy/SSO interposed; following it would re-issue
    POST /search as a body-less GET. It must surface as an error, not be followed."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    responses.add(
        responses.POST,
        "https://x/rest/api/2/search",
        status=302,
        headers={"Location": "https://sso/login"},
    )
    with pytest.raises(requests.exceptions.HTTPError) as ei:
        request_with_retry(
            make_session(pat="x"), "POST", "https://x/rest/api/2/search", json={"jql": "x"}
        )
    assert ei.value.response.status_code == 302  # surfaced, not silently followed


def test_verify_false_relaxes_ssl_context():
    """The self-signed escape hatch must not raise `ValueError: Cannot set verify_mode to
    CERT_NONE when check_hostname is enabled` from the custom TLS-1.2 context."""
    sess = make_session(pat="x", verify=False)
    assert sess.verify is False
    ctx = sess.get_adapter("https://example.com")._ssl_context()
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_verify_true_keeps_cert_verification():
    ctx = make_session(pat="x", verify=True).get_adapter("https://example.com")._ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


@responses.activate
def test_terminal_429_preserves_response(monkeypatch):
    """When 429s exhaust the retries, the caller gets the REAL 429 (an HTTPError with the
    response attached), not a bare RequestException with the status discarded."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    for _ in range(3):
        responses.add(responses.GET, "https://x/foo", status=429)
    with pytest.raises(requests.exceptions.HTTPError) as ei:
        request_with_retry(make_session(pat="x"), "GET", "https://x/foo", max_attempts=3)
    assert ei.value.response.status_code == 429


@responses.activate
def test_5xx_retried_by_app_then_succeeds(monkeypatch):
    """5xx is handled by request_with_retry (one authority), so its branch is live: a 503
    then a 200 sleeps the app-level 5xx backoff (30) and succeeds."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    responses.add(responses.GET, "https://x/foo", status=503)
    responses.add(responses.GET, "https://x/foo", json={"ok": True}, status=200)
    assert request_with_retry(make_session(pat="x"), "GET", "https://x/foo").json() == {"ok": True}
    assert sleeps == [30]


@responses.activate
def test_5xx_honors_capped_retry_after(monkeypatch):
    """Retry-After is honored AND capped for 5xx too — the same single authority as 429."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    responses.add(responses.GET, "https://x/foo", status=503, headers={"Retry-After": "99999"})
    responses.add(responses.GET, "https://x/foo", json={"ok": True}, status=200)
    request_with_retry(make_session(pat="x"), "GET", "https://x/foo")
    assert sleeps == [_MAX_WAIT_SECONDS]


def test_session_blocks_cookies():
    """PAT auth needs no cookies; blocking them removes the one piece of shared MUTABLE
    state on the Session, making it safe to share across the fan-out of threads."""
    sess = make_session(pat="x")
    assert sess.cookies._policy is _BLOCK_ALL_COOKIES
