"""A delivery's recorded paths never include a rendered view — the built-in projections or one an extension
registered (verify.excluded_files) — so a file the supervisor rewrites every tick is never frozen as a review input."""
from supervisor import tasks, verify
from tests.conftest import make_task


def test_record_delivery_drops_extension_projections_as_well_as_builtin_views(layout, monkeypatch):
    make_task(layout, "T")
    monkeypatch.setattr(verify, "excluded_files", lambda lay=None: set(verify.EXCLUDED_FILES) | {"EXTRA_VIEW.md"})
    t = tasks.record_delivery(layout, "T", "d" * 16, actor="test",
                              paths=["out/a.txt", "STATE.md", "EXTRA_VIEW.md", "state/tasks/T.yaml"])
    assert t.paths == ["out/a.txt"]
