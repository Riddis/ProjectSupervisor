"""The server watchdog (D-013 section 1.2): starts the configured `argv` as its own child, polls the configured
health URL until ready, holds the machine-wide GPU lease for as long as it runs, and stops - killing the child,
then releasing the lease - on idle (`idle_stop_s`), on a release request from another project, when the child
dies on its own, or when told to (the owner command `llm_endpoint.stop`, a later commit). Runtime-agnostic: it
knows nothing about any particular server binary, only a configured argv and a configured health URL.

Two halves, deliberately separate:

- `ensure_started()` runs in the *loop's* process (the adapter's `submit()`): a cheap health check, and - only
  when unreachable - spawning the watchdog in the background. It never waits out a cold load; a job finding the
  endpoint still unreachable is the adapter's own `CapacityDeferred`, costing no attempt.
- `run()` is the watchdog's own loop, meant for its own detached process (`python -m
  supervisor_plugin_llm_endpoint._server <root> <config file>`, written by `ensure_started()`): acquire, start,
  wait for health, then watch until something says stop.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

from supervisor import plugin

SPAWN_GUARD_S = 30.0         # ensure_started() will not spawn a second watchdog within this long of the last spawn
POLL_S = 2.0


# ---------------------------------------------------------------- tying the child to this process (F5)
#
# The watchdog's own `finally` (below) cannot run after a hard kill, so the server child must die at the OS
# level instead of by this process's own cleanup code: a Job Object with KILL_ON_JOB_CLOSE on Windows, a new
# process group plus PR_SET_PDEATHSIG on POSIX. Each half is guarded so the other platform never touches it.

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOBOBJECTINFOCLASS_EXTENDED_LIMIT = 9
    _PROCESS_TERMINATE_AND_SET_QUOTA = 0x0001 | 0x0100   # the two rights AssignProcessToJobObject needs

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_uint64), ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64), ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64), ("OtherTransferCount", ctypes.c_uint64)]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)   # use_last_error: F22 needs a real GetLastError()
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _start_child_tied_to_this_process(argv) -> tuple[subprocess.Popen, object, bool, int]:
        """A Job Object with KILL_ON_JOB_CLOSE, the child assigned to it. Windows closes every handle this
        process holds - this one included - the instant it exits by any means, including an external kill()
        that skips `finally`, and KILL_ON_JOB_CLOSE kills every process still assigned when that happens. The
        job handle is returned so the caller keeps it referenced for the watchdog's whole life: closing it
        early is exactly what would kill the child early.

        F22 (RELEASE_REVIEW_local-lane_round2.md, advisory): `AssignProcessToJobObject`'s own BOOL used to be
        discarded, so a host where the tie fails (a watchdog already inside a job that forbids nesting or
        breakaway) left the child simply untied with nothing said. Returns whether it actually took and, when
        it did not, `GetLastError()` - the state-based backstop (the doctor, `ensure_started()`, `stop`) still
        catches the orphan on the next call either way; this only makes the gap loud instead of silent."""
        proc = subprocess.Popen(argv)
        job = _kernel32.CreateJobObjectW(None, None)
        tied, last_error = False, 0
        if not job:
            last_error = ctypes.get_last_error()
        else:
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _kernel32.SetInformationJobObject(job, _JOBOBJECTINFOCLASS_EXTENDED_LIMIT, ctypes.byref(info),
                                              ctypes.sizeof(info))
            handle = _kernel32.OpenProcess(_PROCESS_TERMINATE_AND_SET_QUOTA, False, proc.pid)
            if not handle:
                last_error = ctypes.get_last_error()
            else:
                if _kernel32.AssignProcessToJobObject(job, handle):
                    tied = True
                else:
                    last_error = ctypes.get_last_error()
                _kernel32.CloseHandle(handle)                  # the process handle, never the job handle
        return proc, job, tied, last_error
else:
    import ctypes

    def _set_pdeathsig() -> None:
        """Runs inside the child, after fork and before exec (`preexec_fn`): PR_SET_PDEATHSIG asks the kernel
        to deliver SIGKILL to this process the instant its parent dies, by any means - Linux only (the
        Windows half's POSIX counterpart), silently absent on a POSIX without `prctl` (e.g. macOS), where the
        process group below is the only guarantee."""
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl(1, signal.SIGKILL)   # PR_SET_PDEATHSIG
        except OSError:
            pass

    def _start_child_tied_to_this_process(argv) -> tuple[subprocess.Popen, None, bool, int]:
        """A new session (so the child is the leader of its own process group, `start_new_session=True`) plus
        PR_SET_PDEATHSIG (`_set_pdeathsig`, Linux). Exercised for real only by CI's ubuntu legs. No failure
        mode is exposed here the way F22 names for Windows, so this half always reports tied."""
        proc = subprocess.Popen(argv, preexec_fn=_set_pdeathsig, start_new_session=True)
        return proc, None, True, 0


def state_path(root) -> Path:
    return Path(root) / "state" / "runtime" / "llm_endpoint_server.json"


def activity_path(root) -> Path:
    return Path(root) / "state" / "runtime" / "llm_endpoint_activity"


def stop_path(root) -> Path:
    return Path(root) / "state" / "runtime" / "llm_endpoint_stop_requested"


def deferral_path(root) -> Path:
    return Path(root) / "state" / "runtime" / "llm_endpoint_deferral.json"


def read_deferral(root) -> dict:
    """The current deferral window, as the adapter's own process last wrote it (never the watchdog's): `{}`
    when nothing is deferring. Read fresh every call - never cached in memory - so the window survives a loop
    restart (D-013 R5 names this explicitly: the accounting is in the state file, not a process-lifetime
    counter)."""
    try:
        return json.loads(deferral_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_deferral(root, *, started: float, last_reason: str) -> None:
    # atomic_json (temp file + replace): read_deferral treats a truncated file as `{}`, a fresh window - a
    # crash mid-write must never look like that and silently grant a new budget.
    plugin.atomic_json(deferral_path(root), {"started": started, "last_reason": last_reason})


def clear_deferral(root) -> None:
    """A submit that reaches the server: the window just ended, so the next deferral - whenever it next
    happens - starts a fresh one and gets its own owner item rather than inheriting an old `started`."""
    try:
        deferral_path(root).unlink()
    except OSError:
        pass


def request_stop(root) -> None:
    """The owner command `llm_endpoint.stop`: ask the watchdog to let go at its next poll, however long that
    is - never a direct kill from here, so the watchdog's own cleanup (child, lease, state file) always runs."""
    path = stop_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _stop_requested(root) -> bool:
    path = stop_path(root)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
        return True
    return False


def _write_state(root, data: dict) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def touch_activity(root) -> None:
    path = activity_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _activity_mtime(root) -> float | None:
    try:
        return activity_path(root).stat().st_mtime
    except OSError:
        return None                      # no activity ever recorded


def is_idle(root, idle_stop_s: float, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        age = now - activity_path(root).stat().st_mtime
    except OSError:
        return True                                   # no activity ever recorded: idle, not kept alive by its absence
    return age > idle_stop_s


def _hand_over_timeout_s(layout) -> float:
    """resources.gpu.lease.hand_over_timeout_s, read straight from routing.yaml the same way
    `supervisor.leases._lease_config` does: `ResourceLease` keeps its own copy on a private `_cfg`, and this
    package already reads the same section directly rather than reach into it (`_preflight_devices`,
    `_raise_owner_deferral_item` in `__init__.py`). F18 (RELEASE_REVIEW_local-lane_round2.md): the bound the
    hand-over grace below is measured against, so a wedged generation cannot hold the lease past the window
    R4 itself names (LOCAL_MODEL_LANE.md:270-271)."""
    gpu_cfg = plugin.resources(layout).get("gpu") or {}
    lease_cfg = gpu_cfg.get("lease") or {}
    return float(lease_cfg.get("hand_over_timeout_s", 900.0))


def _health_ok(base_url: str, health_path: str, timeout: float = 5.0) -> bool:
    try:
        resp = requests.get(base_url.rstrip("/") + health_path, timeout=timeout)
        # F15 (RELEASE_REVIEW_local-lane.md): a 404 from a health_path the server does not serve (Ollama has
        # no /health) is a misconfiguration, not a server that may still be loading (5xx) - only 2xx is ready.
        return 200 <= resp.status_code < 300
    except requests.RequestException:
        return False


def _spawned_recently(root) -> bool:
    try:
        doc = json.loads(state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return time.time() - float(doc.get("spawned", 0)) < SPAWN_GUARD_S


def _process_alive(pid, create_time) -> bool:
    """Same check and the same 2s tolerance as the core's own resource lease (`supervisor/leases.py`,
    `_pid_alive`): `psutil.Process(pid).is_running()` alone trusts whatever the OS later reuses that pid for
    (F14); comparing `create_time` is what tells this process from that one. On POSIX, `is_running()` also
    reads a zombie (exited, not yet reaped by its parent) as present - excluded here so a pid this process's own
    `_kill_process()` just signalled is never read as alive for the brief window before its parent reaps it."""
    import psutil
    if not isinstance(pid, int) or create_time is None:
        return False
    try:
        p = psutil.Process(pid)
        if p.status() == psutil.STATUS_ZOMBIE:
            return False
        return abs(p.create_time() - float(create_time)) < 2.0 and p.is_running()
    except (psutil.Error, ValueError, OSError):
        return False


def _kill_process(pid: int) -> None:
    import psutil
    try:
        psutil.Process(pid).kill()
    except psutil.Error:
        pass


def _process_create_time(pid: int) -> float | None:
    import psutil
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def dead_watchdog_with_live_server(root) -> int | None:
    """None when the state file names no server, or names one whose watchdog is still alive; otherwise the
    server's own pid. This is F5's leak: the lease entry is pruned because the watchdog's pid is dead, so the
    cards look free; a bare health check or a bare `psutil.Process(pid).is_running()` both read this as a
    normal, healthy server, because nothing about either one asks whether anything is left to stop it."""
    try:
        state = json.loads(state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = state.get("pid")
    if pid is None or not _process_alive(pid, state.get("create_time")):
        return None
    if _process_alive(state.get("watchdog_pid"), state.get("watchdog_create_time")):
        return None
    return pid


def ensure_started(cfg: dict, root: str) -> bool:
    """True once the endpoint answers healthy. Never waits out a cold load: when it is not reachable yet, this
    starts the watchdog in the background (unless one was already started inside `SPAWN_GUARD_S`) and returns
    False immediately - the adapter's own `CapacityDeferred` is what makes that cost no attempt.

    When the state file instead names a dead watchdog whose server is still alive (F5), reporting that server
    healthy would leak it forever: nothing renews its lease, nothing idle-stops it, nothing re-adopts it. This
    kills it and falls into the normal cold-start path below, which spawns a fresh, correctly-configured
    watchdog - smaller and safer than teaching a new watchdog to adopt a child whose argv it never recorded."""
    orphan_pid = dead_watchdog_with_live_server(root)
    if orphan_pid is not None:
        _kill_process(orphan_pid)
        _write_state(root, {"pid": None, "stopped": time.time(), "why": "orphan"})
    elif _health_ok(cfg["base_url"], cfg.get("health_path", "/health")):
        return True
    if not cfg.get("argv"):
        raise RuntimeError(f"{cfg['base_url']} is unreachable and plugins.llm_endpoint.argv is not configured: "
                           "nothing to start")
    if _spawned_recently(root):
        return False
    _write_state(root, {"spawned": time.time(), "pid": None})
    config_path = state_path(root).with_suffix(".config.json")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([sys.executable, "-m", "supervisor_plugin_llm_endpoint._server", str(root), str(config_path)],
                     creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return False


def run(root: str, cfg: dict, *, poll_s: float = POLL_S) -> str:
    """The watchdog's own loop. Returns why it stopped: `lease_unavailable` (never started: another project
    holds the machine-wide GPU lease and would not yield within `wait_s`), `start_timeout`, `child_exited`,
    `idle`, `release_requested` or `stop_requested` (the owner command `llm_endpoint.stop`). Blocking; meant to
    be the whole of the watchdog process's work."""
    _stop_requested(root)                       # clear any stale request left from a previous run before this one
    layout = plugin.Layout(root)
    lease = plugin.resource_lease(layout, "gpu")
    if not lease.acquire(wait_s=cfg.get("wait_s", 60)):
        # acquire() itself already recorded this project's release request against the holder (F2/F8); refusing
        # to start honours it instead of discarding it, the inversion F4 named ("this lane always yields").
        _write_state(root, {"pid": None, "stopped": time.time(), "why": "lease"})
        return "lease_unavailable"
    touch_activity(root)
    proc, _job, tied, tie_error = _start_child_tied_to_this_process(cfg["argv"])   # F5: dies even on a hard kill
    try:
        # F22 (round 2 review, regression): this used to call plugin.log_event() below BEFORE this try and before
        # anything had loaded supervisor.plugins' registry for this layout - harmless in a test that primes it by
        # hand, but the real watchdog runs detached (`python -m supervisor_plugin_llm_endpoint._server`,
        # ensure_started(), stderr to DEVNULL) with nothing else to publish it, so log_event()'s calling_plugin()
        # (visible_registry() alone does not load) resolved no one and raised PluginError - on the very host F22
        # targets, the watchdog died before the health wait, with the one log line meant to make the gap loud
        # being the thing that killed it, finally never running, the lease never released. _hand_over_timeout_s()
        # below is this process's own first call into plugin.resources(), which loads that registry (plugins.load,
        # plugin/_config.py:177) as a side effect - computing it first, inside the try, is what makes the
        # log_event call after it safe, and finally still runs if either one does raise for some other reason.
        hand_over_timeout_s = _hand_over_timeout_s(layout)
        if not tied:
            # F22 (RELEASE_REVIEW_local-lane_round2.md, advisory): say so instead of serving an untied child
            # silently - the state-based backstop below still catches the orphan on the next call either way.
            plugin.log_event(layout, "llm_endpoint_server_job_object_tie_failed", pid=proc.pid, win32_error=tie_error)
        deadline = time.time() + cfg.get("start_timeout_s", 300)
        health_path = cfg.get("health_path", "/health")
        while not _health_ok(cfg["base_url"], health_path):
            if proc.poll() is not None:
                return "child_exited"
            if _stop_requested(root):
                return "stop_requested"
            if time.time() > deadline:
                return "start_timeout"
            time.sleep(poll_s)
        own_pid = os.getpid()
        _write_state(root, {"pid": proc.pid, "create_time": _process_create_time(proc.pid),
                            "watchdog_pid": own_pid, "watchdog_create_time": _process_create_time(own_pid),
                            "base_url": cfg["base_url"], "started": time.time(), "tied": tied})
        idle_stop_s = cfg.get("idle_stop_s", 900)
        hand_over_grace_s = cfg.get("hand_over_grace_s", 3.0)
        release_requested_since = None
        # F18 (round 2 review): the activity file already carries a second meaning before any request ever
        # lands - `touch_activity(root)` above, before the child starts, is idle_stop_s's own floor (a freshly
        # started server with nothing in flight yet must not immediately read as idle). Snapshotting its mtime
        # here, once, right before this loop starts (necessarily before any request can possibly have landed)
        # and requiring a *newer* mtime below is what tells "nothing has happened since this server went live"
        # from "a request is streaming" - without it, that start-up touch itself reads as in-flight for the
        # first hand_over_grace_s, so a release landing in that window would wait out the whole grace instead of
        # yielding within one poll like an actually idle server does.
        quiescent_since_start = _activity_mtime(root)
        while True:
            time.sleep(poll_s)
            lease.renew()
            if proc.poll() is not None:
                return "child_exited"
            if _stop_requested(root):
                return "stop_requested"
            if is_idle(root, idle_stop_s):
                return "idle"
            if lease.release_requested() and release_requested_since is None:
                release_requested_since = time.time()
            if release_requested_since is None:
                continue
            mtime = _activity_mtime(root)
            in_flight = (mtime is not None and quiescent_since_start is not None and mtime > quiescent_since_start
                         and not is_idle(root, hand_over_grace_s))
            # R4's fairness clause ("this lane's watchdog finishes the in-flight request", LOCAL_MODEL_LANE.md:
            # 267-270) - honour the request only once a genuinely in-flight request (the check above) has gone
            # quiet for hand_over_grace_s, bounded by the request's own hand_over_timeout_s so a wedged
            # generation cannot hold the cards past the window either.
            if not in_flight or time.time() - release_requested_since >= hand_over_timeout_s:
                return "release_requested"
    finally:
        if proc.poll() is None:
            # The clean paths (idle, release_requested, stop_requested) used to call proc.kill() alone; tying
            # the child to a new session (`_start_child_tied_to_this_process`, POSIX) makes it the leader of
            # its own process group, so this now kills that whole group - any grandchild the configured argv
            # spawned goes with it, not just the direct child. Windows has no such group; proc.kill() plus the
            # Job Object's own KILL_ON_JOB_CLOSE (already in effect, not re-triggered here) cover it instead.
            if sys.platform != "win32":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
            else:
                proc.kill()
            proc.wait(timeout=10)
        lease.release()
        _write_state(root, {"pid": None, "stopped": time.time()})


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root, config_path = argv[0], argv[1]
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    run(root, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
