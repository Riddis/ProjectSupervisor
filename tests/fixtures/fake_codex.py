"""Stand-in for `codex exec --json`: emits thread.started / turn.completed events and writes --output-last-message.
FAKE_CODEX_MODE: ok | nojson | auth | fail"""
import json, os, sys
mode = os.environ.get("FAKE_CODEX_MODE", "ok")
argv = sys.argv[1:]
out = argv[argv.index("--output-last-message") + 1]
prompt = sys.stdin.read()
def emit(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
emit({"type": "thread.started", "thread_id": "thr_123"})
if mode == "auth":
    sys.stderr.write("ERROR: 401 Unauthorized — run codex login\n"); sys.exit(1)
if mode == "limit":
    emit({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again at 5pm."}}); sys.exit(1)
if mode == "fail":
    emit({"type": "turn.failed", "error": {"message": "boom"}}); sys.exit(1)
review = '```json\n{"kind":"review","schema_version":1,"verdict":"ACCEPT","findings":[]}\n```'
revise = ('```json\n{"kind":"review","schema_version":1,"verdict":"REVISE","findings":[{"category":"correctness",'
         '"blocking":true,"message":"x","evidence":"y"}]}\n```')
text = {"ok": "Looks good.\n\n" + review, "revise": "Not yet.\n\n" + revise}.get(mode, "Looks good but no JSON.")
with open(out, "w", encoding="utf-8") as f:
    f.write(text)
emit({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
emit({"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 30}})
