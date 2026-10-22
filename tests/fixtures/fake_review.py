"""Stand-in reviewer CLI (claude_cli stream-json shape): emits one `result` event carrying a `review` JSON.
FAKE_REVIEW_VERDICT selects ACCEPT | REVISE | REJECT | BLOCKED | ACCEPT_INVALID (default ACCEPT).
ACCEPT_INVALID is schema-correct but contract-broken (an ACCEPT carrying a blocking finding) — the verdict a
confused or dishonest reviewer must never get recorded.
FAKE_REVIEW_VERDICT_<ROLE> (role upper-cased, read from the test's injected `--fake-role` flag) overrides
FAKE_REVIEW_VERDICT for that role only, so a mixed-verdict round is expressible."""
import json
import os
import sys

argv = sys.argv[1:]
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else "review-session"
role = argv[argv.index("--fake-role") + 1] if "--fake-role" in argv else ""
mode = os.environ.get(f"FAKE_REVIEW_VERDICT_{role.upper()}") or os.environ.get("FAKE_REVIEW_VERDICT", "ACCEPT")
sys.stdin.read()
findings = [] if mode in ("ACCEPT", "BLOCKED") else [
    {"category": "requirement", "blocking": True, "message": "acceptance criterion unmet", "evidence": "task.acceptance"}]
obj = {"kind": "review", "schema_version": 1, "verdict": "ACCEPT" if mode == "ACCEPT_INVALID" else mode,
      "findings": findings, "summary": f"verdict {mode}"}
text = "Reviewed the frozen inputs.\n\n```json\n" + json.dumps(obj) + "\n```"
sys.stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": mode == "BLOCKED_ERROR", "result": text,
                            "session_id": sid, "duration_ms": 5, "modelUsage": {}, "permission_denials": []}) + "\n")
sys.stdout.flush()
