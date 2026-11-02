"""The framework's own plugin-`MIGRATIONS` mechanism (`supervisor/migrations.py`, driven by a manifest's
`migrations` key naming a module with a `MIGRATIONS` list) carries no ledger of completed entries: a package that
registers one is expected to prove, on a disposable project, that an interrupted run can simply be re-run to
completion and a later run then reports nothing - or to discharge the obligation outright by showing its
manifest names no such module. This package's `supervisor-plugin.toml` carries no `migrations` key (asserted
below, read without importing anything, the same way `supervisor plugin check`'s own `manifest` step does) and no
`config.legacy_sections` key either: it is new under the identity `review_rounds` and was never released before
the `plugins.<identity>` naming existed, so there is no pre-cutover top-level section for the generic `supervisor
migrate` move to drive either.

What this package does carry is its own data migration, run on demand rather than by `supervisor migrate`: a
round or labelling item created before this package's own verdict-word cutover may still carry a legacy
per-image verdict word, and `migrate_legacy_verdicts()` (`supervisor review migrate-verdicts`) rewrites it to
today's word, keeping the original in `verdict_legacy`. The tests below prove that migration on a disposable
project: it reports a dry run without changing anything, applies once, and a second (and third) run reports
nothing - the idempotence `docs/PLUGIN_STANDARD.md` asks of every migration, proved directly rather than assumed."""
from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

import supervisor_plugin_review_rounds as rr
from supervisor import testing

MANIFEST = Path(rr.__file__).resolve().parent / "supervisor-plugin.toml"


def test_manifest_carries_no_migrations_key():
    """Read without importing anything, the same evidence `supervisor plugin check`'s own `manifest` step uses -
    this package discharges the generic plugin-`MIGRATIONS` obligation by having nothing for it to run."""
    doc = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "migrations" not in doc
    assert "legacy_sections" not in doc.get("config", {})


def _legacy_round(layout, kind="labelling"):
    rows = [{"id": "im0", "verdict": "MORE_LIKE"}, {"id": "im1", "verdict": "ACCEPT"}, {"id": "im2"},
            {"id": "im0r", "retest_of": "im0", "verdict": "REGENERATE"}]
    return testing.make_inbox_item(layout, kind=kind, subject="s", summary="s", allowed_responses=["SUBMIT"],
                                   items=rows)


def test_legacy_verdicts_read_as_todays_word_before_any_migration(layout):
    item = _legacy_round(layout)
    assert rr.progress(item) == (3, 4)
    stats = rr.agreement_stats(item)
    assert stats["scored"] == 1 and stats["agreed"] == 1       # MORE_LIKE vs REGENERATE agree as REDO
    assert not rr.check_legacy_verdicts(layout).ok


def test_migrate_verdicts_on_a_disposable_project_dry_run_then_apply_then_idempotent(layout):
    """The shape the framework asks of every migration: a dry run changes nothing, the real run applies once, and
    a third run (or a dry run after the second) reports nothing for this plugin - proved on a project built just
    for this test, never reused."""
    open_item = _legacy_round(layout)
    done_item = _legacy_round(layout)
    rr.set_verdict(layout, done_item["id"], "im2", "ACCEPT")
    rr.submit(layout, done_item["id"])                          # answered before the migration: a record

    dry = rr.migrate_legacy_verdicts(layout, dry_run=True)
    assert dry["items"] == {open_item["id"]: 2} and dry["answered_left_as_written"] == {done_item["id"]: 2}
    assert testing.inbox_item(layout, open_item["id"])["items"][0]["verdict"] == "MORE_LIKE"   # dry run changed nothing

    out = rr.migrate_legacy_verdicts(layout)
    assert out["items"] == {open_item["id"]: 2}
    rows = {r["id"]: r for r in testing.inbox_item(layout, open_item["id"])["items"]}
    assert rows["im0"] == {**rows["im0"], "verdict": "REDO", "verdict_legacy": "MORE_LIKE"}
    assert rows["im0r"]["verdict"] == "REDO" and rows["im0r"]["verdict_legacy"] == "REGENERATE"
    assert rows["im1"]["verdict"] == "ACCEPT" and "verdict_legacy" not in rows["im1"]

    kept = {r["id"]: r for r in testing.inbox_item(layout, done_item["id"])["items"]}
    assert kept["im0"]["verdict"] == "MORE_LIKE"                # an answered item's history is never rewritten

    # The retry: a second real run (the "interrupted, re-run `supervisor review migrate-verdicts`" case) reports
    # nothing, and a dry run after it agrees - both runs after the first find nothing left to do.
    assert rr.migrate_legacy_verdicts(layout)["items"] == {}
    assert rr.migrate_legacy_verdicts(layout, dry_run=True)["items"] == {}
    assert rr.check_legacy_verdicts(layout).ok
    assert "review_verdicts_migrated" in layout.log.read_text(encoding="utf-8")


def test_migrate_verdicts_survives_a_crash_between_entries(layout, monkeypatch):
    """A literal interruption, not just an idempotence argument: force the migration to die after rewriting the
    first item's rows but before the second's (`plugin.log_event` is the last call the loop makes per item, so
    raising there leaves that item's own write already on disk but the loop dead before it reaches the next
    item) - a disposable project's own crash-and-retry, on real files, not a mock standing in for one."""
    first = _legacy_round(layout)
    second = _legacy_round(layout)
    real_log_event = rr.plugin.log_event
    calls = []

    def flaky_log_event(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("simulated crash between entries")
        return real_log_event(*args, **kwargs)
    monkeypatch.setattr(rr.plugin, "log_event", flaky_log_event)

    with pytest.raises(RuntimeError, match="simulated crash"):
        rr.migrate_legacy_verdicts(layout)
    # The first item's own write landed (store.update() committed before log_event ever ran); the crash happened
    # on the way to the second, which is therefore untouched.
    migrated = [testing.inbox_item(layout, i["id"])["items"][0]["verdict"] == "REDO" for i in (first, second)]
    assert migrated.count(True) == 1 and migrated.count(False) == 1

    monkeypatch.setattr(rr.plugin, "log_event", real_log_event)   # the retry: no crash, finishes the rest
    out = rr.migrate_legacy_verdicts(layout)
    assert out["items"] == {second["id"]: 2} or out["items"] == {first["id"]: 2}   # whichever was left
    assert testing.inbox_item(layout, first["id"])["items"][0]["verdict"] == "REDO"
    assert testing.inbox_item(layout, second["id"])["items"][0]["verdict"] == "REDO"
    assert rr.migrate_legacy_verdicts(layout)["items"] == {}        # a third run: nothing left to do


def test_a_new_owner_verdict_supersedes_the_migrated_word(layout):
    item = _legacy_round(layout)
    rr.migrate_legacy_verdicts(layout)
    rr.set_verdict(layout, item["id"], "im0", "ACCEPT")          # the owner looked again and decided anew
    row = {r["id"]: r for r in testing.inbox_item(layout, item["id"])["items"]}["im0"]
    assert row["verdict"] == "ACCEPT" and "verdict_legacy" not in row


@pytest.mark.parametrize("dry_run", [True, False])
def test_migrate_verdicts_cli_runs_against_a_disposable_project(layout, dry_run, monkeypatch):
    """The same migration, through its own CLI entry point (`supervisor review migrate-verdicts`), on the
    project's real on-disk layout rather than calling the Python function directly. Mounted under a parent app
    exactly as `registry.cli(_cli(), "review")` makes the framework mount it (`supervisor review
    migrate-verdicts`) - invoking `_cli()` on its own collapses the subcommand name away (it has only one
    command), which is not how a real `supervisor` invocation reaches it."""
    import typer
    from typer.testing import CliRunner

    item = _legacy_round(layout)
    monkeypatch.setenv("SUPERVISOR_ROOT", str(layout.root))
    main = typer.Typer()
    main.add_typer(rr._cli(), name="review")
    runner = CliRunner()
    result = runner.invoke(main, ["review", "migrate-verdicts", *(["--dry-run"] if dry_run else [])])
    assert result.exit_code == 0, result.output
    verdict = testing.inbox_item(layout, item["id"])["items"][0]["verdict"]
    assert verdict == ("MORE_LIKE" if dry_run else "REDO")
