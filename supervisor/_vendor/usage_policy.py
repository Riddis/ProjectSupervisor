"""Provider capacity observations (Claude 5-hour/weekly, Codex primary/secondary). No model calls, no credentials.

Vendored from an earlier supervisor's `usage_policy.py`. Sources kept: Claude CLI stream `rate_limit_event`s and the documented
Codex `app-server` `account/rateLimits/read` RPC. Dropped: the undocumented Claude OAuth usage endpoint poll (it read
the CLI's token; the supervisor never touches credentials). State dir is injected with `configure()`.
"""
from __future__ import annotations

import json
import math
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .contract import atomic_json

ENGINES = ("claude", "codex")
MAX_AGE = 180
_state_dir: Path | None = None
_reserve = {"claude": 10, "codex": 10}          # short (5-hour) window floor, percent remaining
_weekly_reserve = {"claude": 0, "codex": 0}     # long (7-day) window floor; 0 = gate only when exhausted


def configure(state_dir, reserve_percent: dict | None = None, weekly_percent: dict | None = None):
    global _state_dir
    _state_dir = Path(state_dir)
    _state_dir.mkdir(parents=True, exist_ok=True)
    if reserve_percent:
        _reserve.update(reserve_percent)
    if weekly_percent:
        _weekly_reserve.update(weekly_percent)


def _path(engine) -> Path:
    if _state_dir is None:
        raise RuntimeError("usage_policy.configure(state_dir) first")
    return _state_dir / f"usage_{engine}.json"


def read_provider(engine):
    try:
        return json.loads(_path(engine).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def carry_unreported(engine, windows):
    """Unreported windows are not evidence that earlier depleted windows recovered."""
    named = {w["name"] for w in windows if number(w.get("used_percent"))}
    previous = read_provider(engine)
    now = time.time()
    return list(windows) + [dict(w, observed_epoch=w.get("observed_epoch", previous.get("observed_epoch")))
                            for w in previous.get("windows", [])
                            if w.get("name") not in named and number(w.get("reset_epoch")) and w["reset_epoch"] > now]


def record(engine, windows, source, now=None):
    now = time.time() if now is None else now
    clean = []
    for window in windows:
        used, reset = window.get("used_percent"), window.get("reset_epoch")
        if not number(used) or not 0 <= used <= 100 or not number(reset) or reset <= now:
            continue
        clean.append({"name": window["name"], "used_percent": used, "remaining_percent": 100 - used,
                      "reset_epoch": reset, "observed_epoch": window.get("observed_epoch") or now})
    if not clean:
        previous = read_provider(engine)
        if previous.get("windows"):
            previous["last_poll_state"] = "UNKNOWN"
            previous["last_poll_epoch"] = now
            atomic_json(_path(engine), previous)
            return previous
    previous = read_provider(engine)
    named = {w["name"] for w in clean}
    clean += [dict(w, observed_epoch=w.get("observed_epoch", previous.get("observed_epoch")))
              for w in previous.get("windows", []) if w["name"] not in named and w.get("reset_epoch", 0) > now]
    value = {"schema_version": 1, "engine": engine,
             "observed_epoch": max((w.get("observed_epoch") or 0 for w in clean), default=now),
             "source": source, "windows": clean, "state": "KNOWN" if clean else "UNKNOWN",
             "last_poll_state": "KNOWN" if clean else "UNKNOWN", "last_poll_epoch": now}
    if previous.get("owner_resets"):
        value["owner_resets"] = previous["owner_resets"]
    atomic_json(_path(engine), value)
    return value


WINDOW_LABELS = {("claude", "five_hour"): "5-hour", ("claude", "seven_day"): "7-day",
                 ("codex", "primary"): "5-hour", ("codex", "secondary"): "7-day",
                 ("claude", "cli_limit"): "CLI-reported limit", ("codex", "cli_limit"): "CLI-reported limit"}


def window_label(engine, name):
    return WINDOW_LABELS.get((engine, name), name)


def window_floor(engine, window):
    """(floor in percent remaining, the routing.yaml `capacity.<engine>` key that sets it, or None): the 5-hour
    window's reserve, the 7-day window's floor; any other window gates only when exhausted (floor 0, no key)."""
    label = window_label(engine, window.get("name"))
    if label == "5-hour":
        return _reserve.get(engine, 10), "reserve_percent"
    if label == "7-day":
        return _weekly_reserve.get(engine, 0), "weekly_gate_percent"
    return 0, None


def window_gates(engine, window, now):
    """True when this measured window currently blocks dispatch: its remaining share is at or below its floor
    (window_floor)."""
    if window["reset_epoch"] <= now:
        return False
    return window.get("remaining_percent", 101) <= window_floor(engine, window)[0]


def reserve_until(engine, now=None, observation=None):
    """Epoch until which dispatch is gated (latest gating reset + 60 s), or None when available."""
    now = time.time() if now is None else now
    value = read_provider(engine) if observation is None else observation
    resets = [w["reset_epoch"] for w in value.get("windows", []) if window_gates(engine, w, now)]
    return max(resets) + 60 if resets else None


def engine_available(engine, now=None):
    return reserve_until(engine, now) is None


def owner_reset(engine, message="", now=None):
    """Owner-reported reset: drop every window that currently gates; audit trail kept; next real observation wins."""
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}")
    now = time.time() if now is None else now
    value = read_provider(engine)
    windows = value.get("windows", [])
    cleared = [w for w in windows if window_gates(engine, w, now)]
    kept = [w for w in windows if not window_gates(engine, w, now)]
    audit = list(value.get("owner_resets") or [])[-9:]
    audit.append({"epoch": now, "message": message, "cleared": cleared})
    value.update({"schema_version": 1, "engine": engine, "windows": kept, "source": "owner usage reset",
                  "state": "KNOWN" if kept else "UNKNOWN", "owner_resets": audit})
    atomic_json(_path(engine), value)
    return cleared


# ---------------------------------------------------------------- Claude stream events

def claude_event(event):
    if event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info") or event.get("rateLimitInfo") or {}
    windows = info.get("unifiedWindows") or []
    if isinstance(windows, dict):
        windows = [dict(v, rateLimitType=k) for k, v in windows.items() if isinstance(v, dict)]
    if not windows:
        windows = [info]
    result = []
    for window in windows:
        utilization = window.get("utilization")
        if utilization is None and window.get("status") == "rejected":
            utilization = 1
        result.append({"name": window.get("rateLimitType", "subscription"),
                       "used_percent": utilization * 100 if number(utilization) else None,
                       "reset_epoch": window.get("resetsAt")})
    if any(number(w["used_percent"]) and number(w["reset_epoch"]) for w in result):
        return record("claude", carry_unreported("claude", result), "claude rate_limit_event")
    return None


def ingest_claude_stream(path, offset=0):
    """Feed rate_limit_events from a growing stdout.jsonl; returns the offset to resume from."""
    # Binary mode: offsets are byte offsets regardless of platform newline translation (Windows \r\n).
    with Path(path).open("rb") as stream:
        stream.seek(offset)
        while True:
            start = stream.tell()
            raw = stream.readline()
            if not raw or not raw.endswith(b"\n"):
                return start
            try:
                claude_event(json.loads(raw.decode("utf-8-sig", errors="replace")))
            except ValueError:
                pass


# ---------------------------------------------------------------- Codex app-server RPC

def codex_windows(result):
    buckets = result.get("rateLimitsByLimitId")
    if buckets is None:
        bucket = result.get("rateLimits") or {}
        buckets = {bucket.get("limitId") or "codex": bucket}
    bucket = buckets.get("codex") or {}
    return [{"name": name, "used_percent": w.get("usedPercent"), "reset_epoch": w.get("resetsAt")}
            for name in ("primary", "secondary") if isinstance((w := bucket.get(name)), dict)]


def poll_codex(executable, env=None, timeout=10, client_name="supervisor_usage"):
    """Documented app-server RPC; never a model turn."""
    process = subprocess.Popen([str(executable), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    messages: queue.Queue = queue.Queue()

    def read():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def send(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    def receive(identity):
        while True:
            try:
                value = messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise RuntimeError("Usage RPC timed out") from exc
            if value.get("id") == identity:
                if "error" in value:
                    raise RuntimeError("Usage RPC unavailable")
                return value["result"]

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": client_name, "version": "1.0"}}})
        receive(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        return record("codex", carry_unreported("codex", codex_windows(receive(2))), "account/rateLimits/read")
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        reader.join(timeout=1)
        process.stdin.close()
        process.stdout.close()


# ---------------------------------------------------------------- rendering helpers

def format_age(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def capacity_windows(now=None):
    now = time.time() if now is None else now
    result = {}
    for engine in ENGINES:
        value = read_provider(engine)
        observed = value.get("observed_epoch")
        windows = []
        for window in value.get("windows", []):
            stamp = window.get("observed_epoch", observed)
            expired = window["reset_epoch"] <= now
            stale = expired or not number(stamp) or now - stamp > MAX_AGE
            floor, floor_key = window_floor(engine, window)
            windows.append(dict(window, observed_epoch=stamp, stale=stale, expired=expired,
                                label=window_label(engine, window["name"]), gating=window_gates(engine, window, now),
                                floor=floor, floor_key=floor_key))
        age = now - observed if number(observed) else None
        result[engine] = {"windows": windows, "observed_epoch": observed, "age_seconds": age,
                          "stale": age is not None and age > MAX_AGE, "reserve_until": reserve_until(engine, now, value)}
    return result


def capacity_lines(now=None):
    now = time.time() if now is None else now
    lines = []
    for engine, data in capacity_windows(now).items():
        if not data["windows"]:
            lines.append(f"{engine.title()}: remaining capacity unknown.")
            continue
        for w in data["windows"]:
            observed = w.get("observed_epoch")
            age_text = format_age(now - observed) if number(observed) else "unknown"
            reset = datetime.fromtimestamp(w["reset_epoch"], timezone.utc).astimezone().isoformat(timespec="minutes")
            lines.append(f"{engine.title()} {w['label']}: {w['remaining_percent']:g}% remaining; reset {reset}; "
                         f"observed {age_text} ago" + (" (stale)." if w["stale"] else "."))
    return lines
