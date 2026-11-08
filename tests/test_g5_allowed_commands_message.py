"""2.4.0 G5: a refused command's message names the allowed list (not just the configuration key), the brief states
the project's own list verbatim, and `python -m ruff`/`python3 -m ruff`/`py -m ruff` join the default allow-list
alongside the bare `ruff` forms already there."""
import sys

from supervisor import packets, tasks, verify
from tests.conftest import make_task


def test_ruff_via_the_python_module_form_is_permitted_by_default():
    assert verify.permitted(f"{sys.executable} -m ruff check .", verify.ALLOWED_TEST_COMMANDS) is True
    assert verify.permitted("python -m ruff check .", verify.ALLOWED_TEST_COMMANDS) is True
    assert verify.permitted("python3 -m ruff check .", verify.ALLOWED_TEST_COMMANDS) is True
    assert verify.permitted("py -m ruff check .", verify.ALLOWED_TEST_COMMANDS) is True


def test_a_refused_commands_message_names_the_allowed_list(layout):
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    outcome = verify.verify(layout, task, [], declared_tests=[{"command": "make test", "exit_code": 0}])
    assert outcome.ok is False
    msg = str(outcome.report["tests"])
    assert "allowed:" in msg and "pytest" in msg and "verification.allowed_test_commands" in msg


def test_brief_lists_the_projects_own_allowed_commands_verbatim(layout):
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    text = packets.result_contract_delivery(layout, task)
    for cmd in verify.allowed_test_commands(layout):
        assert cmd in text
    assert "A helper script is never a command" in text
