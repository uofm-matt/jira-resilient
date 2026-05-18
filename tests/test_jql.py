"""Unit tests for JQL composition + injection guards."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jira_resilient.jql import build_jql, build_seek_jql


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
        for bad in ("PROJ", "PROJ-1", "x", "ABC DEF", ""):
            with pytest.raises(ValueError, match="Invalid project key"):
                build_jql(bad)

    @pytest.mark.parametrize("dangerous", [
        "status = X; DROP TABLE users",
        "status = X UNION select 1",
        "status = X -- comment",
        "status = X /* injection */",
        "status = X DELETE",
    ])
    def test_rejects_dangerous_extra_filter(self, dangerous):
        with pytest.raises(ValueError, match="Unsafe characters"):
            build_jql("PROJ", extra_filter=dangerous)

    def test_allows_safe_extra_filter_with_order_by(self):
        # "-" inside ORDER BY context shouldn't trip the comment guard.
        build_jql("PROJ", extra_filter="created > -7d ORDER BY rank")


class TestBuildSeekJql:
    def test_no_cursor(self):
        out = build_seek_jql("PROJ")
        assert out == 'project = "PROJ" ORDER BY updated ASC, key ASC'

    def test_with_ts_only(self):
        ts = datetime(2026, 5, 18, 7, 30, tzinfo=timezone.utc)
        out = build_seek_jql("PROJ", after_ts=ts)
        assert 'updated >= "2026-05-18 07:30"' in out

    def test_with_ts_and_key_uses_tuple_form(self):
        ts = datetime(2026, 5, 18, 7, 30, tzinfo=timezone.utc)
        out = build_seek_jql("PROJ", after_ts=ts, after_key="PROJ-100")
        assert ('AND (updated > "2026-05-18 07:30" '
                'OR (updated = "2026-05-18 07:30" AND key > "PROJ-100"))') in out

    def test_with_string_ts_is_truncated_to_minute(self):
        out = build_seek_jql("PROJ", after_ts="2026-05-18T07:30:42.123Z",
                             after_key="PROJ-100")
        assert '"2026-05-18 07:30"' in out
        assert "42" not in out  # seconds stripped
