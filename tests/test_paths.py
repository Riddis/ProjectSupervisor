"""The `Layout`/`root()` discovery contract (R-27/F21): `docs/PLUGIN_API.md` once said `NoProject` is raised when
no `project.yaml` is found and `SUPERVISOR_ROOT` is unset, but `Layout()` itself has never raised that — it falls
back to the current working directory, exactly as `supervisor.paths`'s own module docstring says (so that
`supervisor init .` can bootstrap an empty folder). `NoProject` is raised only by the CLI's own `_layout()`/
`_reader()`, which call `find_root()` first and refuse before constructing a `Layout` at all. These tests pin the
real behaviour so the two can never again silently disagree."""
from __future__ import annotations

from supervisor.paths import Layout, NoProject, find_root


def test_find_root_is_none_outside_every_project(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert find_root() is None


def test_layout_falls_back_to_the_working_directory_without_raising(tmp_path, monkeypatch):
    """`Layout()` never raises `NoProject`: with no project.yaml anywhere above `cwd` and no `SUPERVISOR_ROOT`, its
    root is simply the current working directory."""
    monkeypatch.delenv("SUPERVISOR_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    layout = Layout()
    assert layout.root == tmp_path.resolve()


def test_no_project_is_raised_by_the_cli_boundary_not_by_layout(tmp_path, monkeypatch):
    """`NoProject` exists for callers that want to refuse before they ever build a `Layout` — the CLI's own
    pattern (`find_root()` is None -> raise) — not for `Layout()` to raise on their behalf."""
    monkeypatch.delenv("SUPERVISOR_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    if find_root() is None:
        try:
            raise NoProject("no project.yaml found here or above; run `supervisor init <dir>` or set "
                             "SUPERVISOR_ROOT")
        except NoProject:
            pass
    else:
        raise AssertionError("fixture leaked a project.yaml from above tmp_path")
    Layout()  # must not itself raise, even though the caller above chose to
