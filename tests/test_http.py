"""Unit tests for HTTP foundation: session config + retry logic."""
from __future__ import annotations

import time

import pytest
import requests
import responses

from jira_resilient.http import _RETRY, make_session, request_with_retry


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
    http_adapter  = sess.get_adapter("http://example.com")
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
    responses.add(responses.GET, "https://x/foo",
                  body=requests.exceptions.ConnectionError("boom"))
    responses.add(responses.GET, "https://x/foo", json={"ok": True}, status=200)
    sess = make_session(pat="x")
    assert request_with_retry(sess, "GET", "https://x/foo").json() == {"ok": True}
