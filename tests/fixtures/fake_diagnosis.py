"""Stand-in manager/architect CLI (claude_cli stream-json shape): emits one `result` event carrying a `diagnosis`
JSON. FAKE_DIAGNOSIS_ACTION selects requeue | defer | architect | owner | GARBAGE (no structured result);
FAKE_DIAGNOSIS_ACTION_<ROLE> (from the injected --fake-role flag) overrides it per role, so a manager → architect
hand-off is expressible. FAKE_DIAGNOSIS_HOLD=<path> keeps the "session" running until that file exists (60 s at
most), so a test can look at a diagnosis while it is certainly still in flight."""
import json
import os
import sys
import time

argv = sys.argv[1:]
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else "diag-session"
role = argv[argv.index("--fake-role") + 1] if "--fake-role" in argv else ""
action = os.environ.get(f"FAKE_DIAGNOSIS_ACTION_{role.upper()}") or os.environ.get("FAKE_DIAGNOSIS_ACTION", "requeue")
sys.stdin.read()
hold = os.environ.get("FAKE_DIAGNOSIS_HOLD")
if hold:
    deadline = time.monotonic() + 60
    while not os.path.exists(hold) and time.monotonic() < deadline:
        time.sleep(0.05)
if action == "GARBAGE":
    text = "I looked at it and I am not sure."
else:
    obj = {"kind": "diagnosis", "schema_version": 1, "action": action, "summary": f"{role} says {action}",
           "guidance": "write the artifact to the declared path before reporting COMPLETE",
           "blocked_on": "the owner must install the missing runtime", "question": "spend on the GPU or accept SDXL?"}
    text = "Read the run directories.\n\n```json\n" + json.dumps(obj) + "\n```"
sys.stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                            "session_id": sid, "duration_ms": 5, "modelUsage": {}, "permission_denials": []}) + "\n")
sys.stdout.flush()
