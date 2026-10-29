"""1.13.0, a skill-trial finding (role-change, round 2): `supervisor reload` turned a paused loop back on. The reload
command replaced the decision with RELOAD, and the loop set RUN when it came back, so a project kept PAUSED for the
owner (as every maintenance window leaves it) started dispatching on a reload. The command now remembers the decision
it replaced, and the loop restores it: PAUSE stays PAUSE, DRAIN stays DRAIN, RUN stays RUN."""
from __future__ import annotations

import pytest

from supervisor import commands, runtime
from supervisor.loop import EXIT_RELOAD, Supervisor


@pytest.mark.parametrize("before", ["PAUSE", "DRAIN", "RUN"])
def test_a_reload_restores_the_decision_it_replaced(layout, before):
    runtime.set_decision(layout, before, "owner")
    commands.submit(layout, "loop.reload", {}, actor="owner")
    assert Supervisor(layout, sleep_s=0).run(max_iterations=5) == EXIT_RELOAD
    assert runtime.read(layout)["decision"] == before


def test_a_loop_starting_on_a_leftover_reload_restores_it_too(layout):
    """A loop that died between the reload request and its restart finds RELOAD at start."""
    runtime.set_decision(layout, "PAUSE", "owner")
    commands._loop_control(layout, {"_kind": "loop.reload"}, "owner")
    assert runtime.read(layout)["decision"] == "RELOAD"
    Supervisor(layout, sleep_s=0).run(max_iterations=1, reset_decision=True)
    assert runtime.read(layout)["decision"] == "PAUSE"


@pytest.mark.parametrize("before", ["PAUSE", "DRAIN"])
def test_a_second_reload_before_the_first_is_done_keeps_the_saved_decision(layout, before):
    """Review 1.13.0 R1 (P1): the second request read RELOAD as the decision it replaced, saved RUN, and the loop came
    back running. A reload that is still pending keeps the decision the first one saved."""
    runtime.set_decision(layout, before, "owner")
    commands._loop_control(layout, {"_kind": "loop.reload"}, "owner")
    commands._loop_control(layout, {"_kind": "loop.reload"}, "owner")
    assert runtime.read(layout)["after_reload"] == before
    assert Supervisor(layout, sleep_s=0).run(max_iterations=5) == EXIT_RELOAD
    assert runtime.read(layout)["decision"] == before


@pytest.mark.parametrize("before, after", [("PAUSE", "PAUSE"), ("DRAIN", "STOP")])
def test_a_loop_starting_on_a_repeated_unfinished_reload_restores_the_saved_decision(layout, before, after):
    """With nothing open, a restored DRAIN finishes at once and the loop stops; a lost one would leave RUN."""
    runtime.set_decision(layout, before, "owner")
    for _ in range(3):
        commands._loop_control(layout, {"_kind": "loop.reload"}, "owner")
    Supervisor(layout, sleep_s=0).run(max_iterations=1, reset_decision=True)
    assert runtime.read(layout)["decision"] == after
