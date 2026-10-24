"""Control Center timestamps render as local `YYYY-MM-DD HH:MM`, never raw ISO/UTC."""
from datetime import datetime, timezone

from supervisor.ui.deps import templates
from supervisor.ui.timefmt import local_stamp


def test_iso_utc_becomes_local_minutes():
    expected = datetime(2026, 10, 22, 19, 47, 4, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    assert local_stamp("2026-10-22T19:47:04+00:00") == expected
    assert local_stamp("2026-10-22T19:47:04Z") == expected
    assert local_stamp("2026-10-22T19:47:04") == expected  # naive store values are UTC


def test_passthrough_for_empty_or_unparseable():
    assert local_stamp("") == ""
    assert local_stamp(None) == ""
    assert local_stamp("now") == "now"


def test_the_filter_is_registered_for_templates():
    assert templates.env.filters["localtime"] is local_stamp
