"""Review 1.12.0 R2 (P2): a project's registry is published only when its load has finished. A thread that calls
`extensions.load()` or `assert_loaded()` while another thread is setting extensions up waits for the result. It
never receives the unfinished registry, whose hooks a failure may still roll back, or an empty blocking list
before a required extension's failure is known. The thread doing the load still reads its own in-progress
registry (ext.config() inside setup()).

G5's staging registry stands in for the project's only on the thread running the check. Every other thread keeps
seeing the published registry."""
from __future__ import annotations

import sys
import threading
from types import ModuleType

import pytest

from supervisor import extension_check, extensions, testing

SLOW = '''import review_sync
def setup(r):
    r.packet_screen(lambda *a, **k: None)
    review_sync.ready.set()
    review_sync.resume.wait(10)
    {end}
'''


@pytest.fixture
def sync(monkeypatch):
    module = ModuleType("review_sync")
    module.ready, module.resume = threading.Event(), threading.Event()
    monkeypatch.setitem(sys.modules, "review_sync", module)
    testing.reset()
    yield module
    module.resume.set()
    testing.reset()


def concurrent(layout, sync):
    """Load on a worker thread; once its setup is waiting, load again from a second thread. Returns (whether the
    second call returned while setup was still running, the registry it got)."""
    worker = threading.Thread(target=extensions.load, args=(layout,))
    worker.start()
    assert sync.ready.wait(10)
    got = {}
    reader = threading.Thread(target=lambda: got.setdefault("reg", extensions.load(layout)))
    reader.start()
    reader.join(0.5)
    returned_early = not reader.is_alive()
    sync.resume.set()
    worker.join(10)
    reader.join(10)
    return returned_early, got["reg"]


def test_a_reader_during_a_failing_setup_waits_and_then_sees_the_failure(tmp_path, sync):
    """Astra's probe: before, the reader got 1 screen and no blocking reason, then kept a screen that was rolled back."""
    layout = testing.project(tmp_path, modules={"slow": SLOW.format(end='raise RuntimeError("failed setup")')})
    returned_early, reg = concurrent(layout, sync)
    assert not returned_early
    assert reg.packet_screens == [] and "failed setup" in (reg.blocking_reason() or "")


def test_a_reader_during_a_good_setup_waits_and_then_sees_it_complete(tmp_path, sync):
    layout = testing.project(tmp_path, modules={"slow": SLOW.format(end="pass")})
    returned_early, reg = concurrent(layout, sync)
    assert not returned_early and reg.loaded == ["slow"] and len(reg.packet_screens) == 1


def test_g5s_staging_registry_is_invisible_to_other_threads(tmp_path, sync):
    manifest = 'identity = "slow"\nversion = "1.0.0"\nframework = ">=1.12"\n[provides]\nhooks = ["packet_screen"]\n'
    layout = testing.project(tmp_path, modules={"slow": SLOW.format(end="pass")},
                             files={"extensions/slow.toml": manifest})
    sync.resume.set()
    published = extensions.load(layout)                   # the project's real registry, loaded once
    sync.ready.clear()
    sync.resume.clear()
    worker = threading.Thread(target=extension_check.run, args=(layout, "slow"))
    worker.start()
    assert sync.ready.wait(30)                            # G5's first staged setup() is running
    seen = extensions.load(layout)
    sync.resume.set()
    worker.join(60)
    assert seen is published and len(seen.packet_screens) == 1
