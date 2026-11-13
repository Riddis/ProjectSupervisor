"""Out-of-process worker for the `llm_endpoint` adapter: one streaming chat-completions request to the configured
endpoint, so generation never blocks the supervisor loop. Invoked as
`python _worker.py <request.json> <result.json>` (the adapter's own prepare() builds that argv). Minimal
dependencies deliberately (requests, jsonschema): a subprocess worker outlives whatever the framework does
around it, the same bare shape every other GPU-locked lane's own worker keeps."""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

import jsonschema
import requests


def _touch(path_str: str | None) -> None:
    if not path_str:
        return
    path = Path(path_str)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        pass                                    # activity tracking is a courtesy to the watchdog, never fatal


def _contained(path: Path, root: Path) -> bool:
    """Whether `path` resolves inside `root` (D-013 section 2.1: "the worker writes only inside the run
    directory and output_file"). Deliberately conservative: a symlink that resolves outside is not contained."""
    try:
        resolved, root = path.resolve(), root.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _stream(base_url: str, payload: dict, timeout_s: float, state: dict):
    """Yields each delta's text as it arrives; writes the terminal `finish_reason` and any `usage` object the
    stream reports into `state` (mutated in place: a generator's own return value is awkward to retrieve from a
    plain `for` loop). Raises on a non-2xx response, an in-stream error object (every runtime's shape for a
    server-side failure after the stream has already started, so it never has `choices`), or a stream whose body
    ends without a `[DONE]` marker - a dropped connection must not read like a normal end."""
    resp = requests.post(base_url.rstrip("/") + "/v1/chat/completions", json=payload, timeout=timeout_s, stream=True)
    resp.raise_for_status()
    saw_done = False
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[len("data:"):].strip()
        if data == "[DONE]":
            saw_done = True
            break
        chunk = json.loads(data)
        if chunk.get("error"):
            err = chunk["error"]
            raise RuntimeError(err.get("message") if isinstance(err, dict) else str(err))
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        text = delta.get("content")
        if text:
            yield text
        if choice.get("finish_reason"):
            state["finish_reason"] = choice["finish_reason"]
        if chunk.get("usage"):
            state["usage"] = chunk["usage"]
    if not saw_done:
        raise RuntimeError("stream ended without a [DONE] marker")


def main(request_path: str, result_path: str) -> int:
    req = json.loads(Path(request_path).read_text(encoding="utf-8"))
    out = Path(result_path)
    text = ""
    state = {"finish_reason": None, "usage": None}
    try:
        payload = {"model": req.get("model"), "messages": [{"role": "user", "content": req.get("prompt", "")}],
                   "max_tokens": req.get("max_tokens"), "stream": True}
        if req.get("schema"):
            # F11 (RELEASE_REVIEW_local-lane.md): llama-server and Ollama tolerate a nameless json_schema;
            # the OpenAI shape vLLM and OpenAI-shaped servers require it to validate the request at all.
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "answer", "schema": req["schema"]}}
        _touch(req.get("activity_file"))
        started = time.time()
        for piece in _stream(req["base_url"], payload, req.get("timeout_s", 1800), state):
            text += piece
            print(json.dumps({"delta": piece}), flush=True)     # tailed into stdout.jsonl: real progress, not silence
            _touch(req.get("activity_file"))
        if state["finish_reason"] == "length":
            raise RuntimeError(f"truncated at max_tokens {req.get('max_tokens')}")
        answer = json.loads(text) if req.get("schema") else text
        if req.get("schema"):
            jsonschema.validate(instance=answer, schema=req["schema"])
        output_written = None
        if req.get("output_file"):
            target = Path(req["run_root"]) / req["output_file"]
            if not _contained(target, Path(req["run_root"])):
                raise ValueError(f"output_file {req['output_file']} escapes the project root")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(answer if isinstance(answer, str) else json.dumps(answer, indent=2), encoding="utf-8")
            output_written = req["output_file"]
        payload_out = {"status": "COMPLETE", "artifacts": [output_written] if output_written else [], "notes": "",
                      "answer": answer, "raw": {"elapsed_s": time.time() - started,
                                                 "finish_reason": state["finish_reason"], "usage": state["usage"]}}
    except jsonschema.ValidationError as exc:
        payload_out = {"status": "FAILED", "error": f"schema validation failed: {exc.message}", "answer": text,
                      "raw": {"finish_reason": state["finish_reason"], "usage": state["usage"]}}
    except (requests.RequestException, ValueError, KeyError, OSError, RuntimeError) as exc:
        payload_out = {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}", "answer": text,
                      "traceback": traceback.format_exc(),
                      "raw": {"finish_reason": state["finish_reason"], "usage": state["usage"]}}
    finally:
        _touch(req.get("activity_file"))
    out.write_text(json.dumps(payload_out, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
