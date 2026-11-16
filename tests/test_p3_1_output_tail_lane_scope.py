"""P3-1 (docs/reviews/RELEASE_REVIEW_2.7.0.md): `worker_report()`'s output-tail fallback (R-12) must be scoped to
a plain-process lane, not merely "not a model lane". At `4d95e77` the condition keyed on
`lane_adapter not in gate.MODEL_LANES` (`gate.MODEL_LANES` lists only `claude_cli`/`codex_cli`/`claude_api`), so a
plugin model lane (e.g. `llm_endpoint`) and `human` still got the raw transcript tail, "/login" prose included."""
from __future__ import annotations

from supervisor.loop import worker_report
from supervisor.plugin import Result


def _run_dir(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "stdout.jsonl").write_text('{"type":"assistant","text":"please run /login to continue"}\n')
    (rd / "stderr.txt").write_text("Traceback: boom\n")
    return rd


def test_no_tail_for_a_plugin_model_lane_human_or_an_unknown_lane(tmp_path):
    rd = _run_dir(tmp_path)
    for lane in ("claude_cli", "codex_cli", "claude_api", "llm_endpoint", "human", None):
        text = worker_report(Result(status="FAILED"), str(rd), lane_adapter=lane)
        assert "/login" not in text, lane
        assert "stdout.jsonl" not in text, lane


def test_tail_still_reaches_process_and_python_lanes(tmp_path):
    rd = _run_dir(tmp_path)
    for lane in ("process", "python"):
        text = worker_report(Result(status="FAILED"), str(rd), lane_adapter=lane)
        assert "stdout.jsonl" in text, lane
        assert "/login" in text, "R-12's own guarantee: a plain process's own output must still reach the diagnosis"
