"""Review 1.12.0 round 2, R2-3 (P2): a load that does not finish is never published as finished. load() published
its registry in a `finally`, so a load interrupted by a BaseException (SystemExit or KeyboardInterrupt in a setup(),
a worker thread ending) or broken by an error outside the per-plugin handler was cached as complete. A later
assert_loaded() then passed with nothing loaded and no blocking error, and kept a hook the interrupted setup() had
registered.

The interrupted plugin's registrations are rolled back like any failed one, the loading slot is always removed,
and the published registry is marked blocking: the loop and the packet screens refuse to proceed until the process
restarts."""
from __future__ import annotations

import threading

import pytest

from supervisor import plugins, testing


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


INTERRUPTED = "def setup(r):\n    r.packet_screen(lambda *a: None)\n    raise SystemExit('cancelled')\n"


def project(tmp_path, early: str = INTERRUPTED):
    return testing.project(tmp_path, modules={"early": early, "later": "def setup(r):\n    pass\n"},
                           plugins=["early", "later"])


def test_an_interrupted_load_blocks_instead_of_passing(tmp_path):
    """Astra's probe: the host survives the SystemExit and asks again."""
    layout = project(tmp_path)
    with pytest.raises(SystemExit):
        plugins.load(layout)
    with pytest.raises(plugins.PluginError, match="interrupted"):
        plugins.assert_loaded(layout)
    reg = plugins.load(layout)
    assert reg.packet_screens == [] and reg.loaded == []    # the interrupted setup() kept nothing
    assert not plugins._loading


def test_an_error_escaping_the_load_blocks_too(tmp_path, monkeypatch):
    layout = project(tmp_path, early="def setup(r):\n    pass\n")

    def broken(self, loaded):
        raise RuntimeError("reconcile broke")

    monkeypatch.setattr(plugins.Registry, "_reconcile", broken)
    with pytest.raises(RuntimeError, match="reconcile broke"):
        plugins.load(layout)
    monkeypatch.undo()
    with pytest.raises(plugins.PluginError, match="did not finish"):
        plugins.assert_loaded(layout)
    assert not plugins._loading


def test_a_reader_waiting_on_an_interrupted_load_gets_the_blocked_result(tmp_path):
    started, release = threading.Event(), threading.Event()
    gate = "import threading\nSTARTED = RELEASE = None\n"
    early = gate + ("def setup(r):\n    r.packet_screen(lambda *a: None)\n    STARTED.set()\n"
                    "    RELEASE.wait(10)\n    raise SystemExit('cancelled')\n")
    layout = project(tmp_path, early=early)
    package = plugins._ensure_package(layout)
    early_module = __import__(f"{package}.early", fromlist=["setup"])
    early_module.STARTED, early_module.RELEASE = started, release

    seen = {}

    def load():
        try:
            plugins.load(layout)
        except SystemExit as exc:                          # what would end a worker thread
            seen["loader"] = exc

    loader = threading.Thread(target=load)
    loader.start()
    assert started.wait(10)

    def read():
        try:
            plugins.assert_loaded(layout)
            seen["result"] = "passed"
        except plugins.PluginError as exc:
            seen["result"] = str(exc)

    reader = threading.Thread(target=read)
    reader.start()
    release.set()
    loader.join(10)
    reader.join(10)
    assert isinstance(seen["loader"], SystemExit)
    assert "interrupted" in seen["result"]
    assert plugins.load(layout).packet_screens == []
