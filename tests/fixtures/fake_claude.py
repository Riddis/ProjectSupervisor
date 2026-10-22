"""A stand-in for the `claude` binary: emits stream-json like Claude Code 2.1.x (R01 §7). Behaviour via FAKE_CLAUDE_MODE:
  ok           → rate_limit_event + result with a fenced delivery JSON
  nojson       → result without JSON; on --resume returns the JSON (follow-up path)
  auth         → authentication error text, exit 1
  limit        → rejected rate_limit_event + limit text
"""
import json, os, sys, time

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
argv = sys.argv[1:]
prompt = sys.stdin.read()
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else "resumed-session"
resumed = "--resume" in argv
now = time.time()


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()


emit({"type": "system", "subtype": "init", "session_id": sid, "tools": argv[argv.index("--tools") + 1].split(",") if "--tools" in argv else []})
if mode == "auth":
    sys.stderr.write("Error: failed to authenticate. Please run /login\n"); sys.exit(1)
if mode == "limit":      # what the CLI really does at the limit: flat rate_limit_info + an is_error result (review #3/#16)
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": now + 1800, "utilization": 1.0}})
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "You've hit your limit · resets 5pm",
          "session_id": sid, "duration_ms": 10, "permission_denials": []})
    sys.exit(1)
if mode == "review":     # a reviewer answer whose prose mentions /login (must not be misclassified) with a REVISE verdict
    text = ("The runbook says: please run /login when you see 'invalid API key'.\n\n```json\n"
            '{"kind":"review","schema_version":1,"verdict":"REVISE","findings":[{"category":"correctness",'
            '"blocking":true,"message":"x","evidence":"y"}]}\n```')
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid, "duration_ms": 5,
          "modelUsage": {}, "permission_denials": []})
    sys.exit(0)
emit({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": [
    {"rateLimitType": "five_hour", "utilization": 0.42, "resetsAt": now + 3600},
    {"rateLimitType": "seven_day", "utilization": 0.10, "resetsAt": now + 86400}]}})
delivery = '```json\n{"kind":"delivery","schema_version":1,"status":"COMPLETE","artifacts":["out/fake.txt"],"notes":"done by fake claude"}\n```'
if mode in ("ok", "denied") or (mode == "nojson" and resumed):
    os.makedirs("out", exist_ok=True)
    with open(os.path.join("out", "fake.txt"), "w") as f:
        f.write("hello " + ("resumed" if resumed else "first"))
    text = "I did the work.\n\n" + delivery
else:
    text = "I did the work but forgot the JSON."
denials = [{"tool_name": "Write", "tool_use_id": "t1", "tool_input": {}}] if mode == "denied" else []
emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid, "duration_ms": 1234,
      "modelUsage": {"claude-sonnet-5": {"inputTokens": 100, "outputTokens": 50}}, "permission_denials": denials})
