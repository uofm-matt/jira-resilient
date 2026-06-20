"""Unit tests for JQL composition + injection guards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from jira_resilient.jql import build_delta_minute_jql, build_jql, build_next_minute_jql


class TestBuildJql:
    def test_minimum(self):
        assert build_jql("PROJ") == 'project = "PROJ" ORDER BY updated ASC'

    def test_with_updated_after_iso(self):
        out = build_jql("PROJ", updated_after="2026-05-18T07:30:00")
        assert 'updated >= "2026-05-18 07:30"' in out
        assert out.endswith(" ORDER BY updated ASC")

    def test_with_extra_filter(self):
        out = build_jql("PROJ", extra_filter='status = "Done"')
        assert 'AND status = "Done"' in out

    def test_rejects_invalid_project_key(self):
        for bad in ("proj", "PROJ-1", "x", "ABC DEF", ""):
            with pytest.raises(ValueError, match="Invalid project key"):
                build_jql(bad)

    @pytest.mark.parametrize(
        "dangerous",
        [
            "status = X; DROP TABLE users",
            "status = X UNION select 1",
            "status = X -- comment",
            "status = X /* injection */",
            "status = X DELETE",
        ],
    )
    def test_rejects_dangerous_extra_filter(self, dangerous):
        with pytest.raises(ValueError, match="Unsafe characters"):
            build_jql("PROJ", extra_filter=dangerous)

    def test_allows_safe_extra_filter_with_order_by(self):
        # "-" inside ORDER BY context shouldn't trip the comment guard.
        build_jql("PROJ", extra_filter="created > -7d ORDER BY rank")


class TestBuildDeltaMinuteJql:
    def test_minute_only_is_half_open_range(self):
        # A bare `= "MM"` matches only the :00-second rows on JIRA Server; the drain
        # must use the half-open range to capture the whole minute.
        m = datetime(2026, 5, 18, 7, 30, tzinfo=UTC)
        out = build_delta_minute_jql("PROJ", minute=m)
        assert out == (
            'project = "PROJ" AND updated >= "2026-05-18 07:30" '
            'AND updated < "2026-05-18 07:31" ORDER BY id ASC'
        )

    def test_with_after_id(self):
        m = datetime(2026, 5, 18, 7, 30, tzinfo=UTC)
        out = build_delta_minute_jql("PROJ", minute=m, after_id=10042)
        assert (
            'updated >= "2026-05-18 07:30" AND updated < "2026-05-18 07:31" AND id > 10042' in out
        )
        assert out.endswith("ORDER BY id ASC")

    def test_seconds_dropped_to_minute(self):
        m = datetime(2026, 5, 18, 7, 30, 42, tzinfo=UTC)
        out = build_delta_minute_jql("PROJ", minute=m)
        assert '>= "2026-05-18 07:30"' in out and '< "2026-05-18 07:31"' in out
        assert ":42" not in out  # seconds stripped

    def test_tz_converts_aware_minute(self):
        # UTC cursor against a server whose local TZ is UTC-4: the rendered minute must
        # be in that offset, or JIRA reads "12:00" as 12:00 server-local = 16:00 UTC and
        # the drain silently skips the 12:00-16:00 UTC window.
        m = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
        out = build_delta_minute_jql("PROJ", minute=m, tz=timezone(timedelta(hours=-4)))
        assert '"2026-05-18 08:00"' in out  # 12:00 UTC = 08:00 at UTC-4

    def test_extra_filter_is_parenthesized(self):
        # A bare top-level OR in extra_filter would otherwise rewrite the whole query.
        m = datetime(2026, 5, 18, 7, 30, tzinfo=UTC)
        out = build_delta_minute_jql("PROJ", minute=m, extra_filter='status = "Done" OR labels = x')
        assert 'AND (status = "Done" OR labels = x)' in out

    def test_rejects_invalid_project_key(self):
        m = datetime(2026, 5, 18, 7, 30, tzinfo=UTC)
        with pytest.raises(ValueError, match="Invalid project key"):
            build_delta_minute_jql("PROJ-1", minute=m)

    def test_rejects_dangerous_extra_filter(self):
        m = datetime(2026, 5, 18, 7, 30, tzinfo=UTC)
        with pytest.raises(ValueError, match="Unsafe characters"):
            build_delta_minute_jql("PROJ", minute=m, extra_filter="x; DROP TABLE t")


class TestBuildNextMinuteJql:
    def test_basic_probes_next_minute(self):
        # Advance is `>= MM+1`, not `> MM` (which JIRA reads as `> MM:00`, re-including
        # MM's own :01-:59 rows).
        m = datetime(2026, 5, 18, 7, 30, tzinfo=UTC)
        out = build_next_minute_jql("PROJ", after_minute=m)
        assert out == (
            'project = "PROJ" AND updated >= "2026-05-18 07:31" ORDER BY updated ASC, id ASC'
        )

    def test_tz_converts_aware_minute(self):
        m = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
        out = build_next_minute_jql("PROJ", after_minute=m, tz=timezone(timedelta(hours=-4)))
        assert '"2026-05-18 08:01"' in out  # 12:00 UTC = 08:00 at UTC-4, +1 min = 08:01

    def test_extra_filter_is_parenthesized(self):
        m = datetime(2026, 5, 18, 7, 30, tzinfo=UTC)
        out = build_next_minute_jql("PROJ", after_minute=m, extra_filter='status = "Done" OR x = 1')
        assert 'AND (status = "Done" OR x = 1)' in out

    def test_tz_works_for_build_jql_too(self):
        utc_ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
        server_tz = timezone(timedelta(hours=-4))  # arbitrary UTC-4 example
        out = build_jql("PROJ", updated_after=utc_ts, tz=server_tz)
        assert '"2026-05-18 08:00"' in out
