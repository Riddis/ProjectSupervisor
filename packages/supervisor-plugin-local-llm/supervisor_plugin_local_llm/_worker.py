"""Out-of-process worker for the `local_llm` adapter: makes one (possibly slow) HTTP call to Ollama and
writes result.json, so inference latency never blocks the supervisor loop. Invoked as
`python _worker.py <request.json> <result.json>` (the adapter's own prepare() builds that argv)."""
from __future__ import annotations

import base64
import json
import sys
import traceback
from pathlib import Path

import jsonschema
import requests


def main(request_path: str, result_path: str) -> int:
    req = json.loads(Path(request_path).read_text(encoding="utf-8"))
    out = Path(result_path)
    try:
        payload = {"model": req["model"], "prompt": req["prompt"], "stream": False}
        if req.get("schema"):
            payload["format"] = req["schema"]
        if req.get("options"):
            payload["options"] = req["options"]
        if req.get("images"):
            payload["images"] = [base64.b64encode(Path(p).read_bytes()).decode("ascii") for p in req["images"]]
        resp = requests.post(req["base_url"].rstrip("/") + "/api/generate", json=payload, timeout=req.get("timeout_s", 300))
        resp.raise_for_status()
        body = resp.json()
        text = body.get("response", "")
        answer = json.loads(text) if req.get("schema") else text
        if req.get("schema"):
            jsonschema.validate(instance=answer, schema=req["schema"])
        payload_out = {"status": "COMPLETE", "artifacts": [], "notes": "", "answer": answer,
                      "raw": {k: v for k, v in body.items() if k != "context"}}
    except jsonschema.ValidationError as exc:
        payload_out = {"status": "FAILED", "error": f"schema validation failed: {exc.message}", "answer": text}
    except (requests.RequestException, ValueError, KeyError, OSError) as exc:
        payload_out = {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
    out.write_text(json.dumps(payload_out, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
