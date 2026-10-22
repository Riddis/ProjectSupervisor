"""Stand-in chat CLI (claude_cli stream-json shape): emits one `result` event carrying a `conversation` JSON reply.
FAKE_CONVERSATION_REPLY overrides the reply text (default echoes the prompt length so a test can assert the
state packet/history actually reached the model). FAKE_CONVERSATION_DEFER emits a usage-limit message instead
(supervisor.adapters.claude_cli.LIMIT_RE) so the DEFERRED path can be exercised without real capacity state."""
import json
import os
import sys

argv = sys.argv[1:]
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else "chat-session"
prompt = sys.stdin.read()

if os.environ.get("FAKE_CONVERSATION_DEFER"):
    text = "usage limit reached, resets in 2 hours"
    sys.stdout.write(json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": text,
                                 "session_id": sid, "duration_ms": 5, "modelUsage": {}, "permission_denials": []}) + "\n")
    sys.stdout.flush()
    sys.exit(0)

reply = os.environ.get("FAKE_CONVERSATION_REPLY") or f"saw a prompt of {len(prompt)} characters"
obj = {"kind": "conversation", "schema_version": 1, "reply": reply}
text = "Here is my answer.\n\n```json\n" + json.dumps(obj) + "\n```"
sys.stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                            "session_id": sid, "duration_ms": 5, "modelUsage": {}, "permission_denials": []}) + "\n")
sys.stdout.flush()
