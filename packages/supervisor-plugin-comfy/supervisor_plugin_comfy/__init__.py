"""comfy: a Supervisor plugin package.

The ComfyUI lane: queues a workflow JSON over ComfyUI's HTTP API, persists the `prompt_id` to the run dir BEFORE
any polling (a crash between submit and the first collect resumes tracking via recover(), never re-submits),
follows progress over the `/ws` websocket best-effort, records peak VRAM while the job runs, and refuses to
submit (CapacityDeferred, costs no attempt) when free VRAM is under the configured floor or the server is
unreachable. One GPU-lock holder at a time (`gpu = True`).

Also the generic doctor checks (the installation's pinned commits, the HTTP API, ffmpeg, a GPU probe) and one
overview panel. A project's own `profiles` and `venv` checks stay with that project (profiles imports its own
generation-profile module; venv is that project's own environment layout) - this package reads VRAM and GPU
totals only from the single-value probe `supervisor.plugin.gpu` exposes, never a project's own multi-device
topology, so a project with more than one card keeps its own role-aware checks and panels beside this one.

Ported from a project-local extension of the same shape (boundary plan phase 5, step 3). Import only
`supervisor.plugin`, and `supervisor.testing` in tests; register only inside setup()."""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import psutil   # the only trustworthy "is it really gone" evidence once HTTP contact with ComfyUI is lost is an
                # OS-level process/port probe, never a guess (see _process_probe_confirms_stopped)
import requests

from supervisor import plugin

WS_POLL_BUDGET_S_DEFAULT = 1.5     # best-effort progress read per collect() call; never blocks the loop for long
_UNSAFE_CHARS = set('/\\:*?"<>|') | {"\x00"}
_SHA = re.compile(r"[0-9a-f]{7,40}")

job_spec = plugin.job_spec          # the framework's: a copy of the task's `job:` block


def comfy_config(layout: plugin.Layout) -> dict:
    """plugins.comfy, with its defaults filled in. Public: a project's own tooling (a bench, a probe script) may
    call this directly, same as the project-local extension this package was ported from."""
    cfg = plugin.config(layout, "comfy")
    cfg.setdefault("base_url", "http://127.0.0.1:8188")
    cfg.setdefault("timeout_s", 1800)
    cfg.setdefault("defer_seconds", 30.0)
    cfg.setdefault("ws_poll_budget_s", WS_POLL_BUDGET_S_DEFAULT)
    cfg.setdefault("download_attempts", 3)
    cfg.setdefault("download_retry_s", 1.0)
    cfg.setdefault("pinned_nodes", [])
    cfg.setdefault("pins_file", "comfy_pins.json")
    gpu_cfg = plugin.resources(layout)["gpu"]
    cfg.setdefault("vram_floor_mb", gpu_cfg.get("usable_vram_floor_mb", 0))
    return cfg


def _ws_url(base_url: str, client_id: str) -> str:
    return base_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + f"/ws?clientId={client_id}"


# Only a Python process (ComfyUI's portable `python_embeded`, a venv python) or a packaged ComfyUI binary can *be*
# the server holding the card, so only those make an unreadable command line inconclusive. That distinction matters:
# Windows refuses the command line of every elevated/system process, and treating each of them as inconclusive would
# leave the probe permanently unknown and the "server is genuinely gone" release path dead code. A readable process
# *name* that cannot be a ComfyUI host is positive evidence this process is not the server; a possible host whose
# command line cannot be read is not, and inconclusive keeps the reservation.
_HOST_NAME_PREFIXES = ("python", "comfy")


def _possible_comfy_host(name) -> bool:
    """Whether a process with this name could be a ComfyUI server. `None` is psutil's value for an attribute it was
    *denied*, so such a process has to stay possible. An empty string is different: it is a name psutil actually
    read, and Windows' kernel-owned minimal processes report exactly that - they cannot be hosting a Python server."""
    if name is None or not isinstance(name, str):
        return True
    return any(name.lower().removesuffix(".exe").startswith(prefix) for prefix in _HOST_NAME_PREFIXES)


def _references_comfy_root(p, root_norm: str) -> bool | None:
    """Whether this one process references the configured ComfyUI install: True (it does - the server is alive),
    False (it demonstrably does not) or None (cannot be determined). psutil reports a command line it was denied
    as None rather than as an empty one, and an empty command line is *not* evidence of absence."""
    info = p.info if isinstance(getattr(p, "info", None), dict) else {}
    cmdline, exe, name = info.get("cmdline"), info.get("exe"), info.get("name")
    for value in (exe, *(cmdline if isinstance(cmdline, list) else ())):
        if isinstance(value, str) and root_norm in value.lower():
            return True
    if isinstance(cmdline, list):                 # command line really was read, and it does not mention the install
        return False
    return None if _possible_comfy_host(name) else False


def _process_probe_confirms_stopped(base_url: str, comfy_root: str | None) -> bool | None:
    """Independent OS-level check, used only once HTTP contact with ComfyUI is lost (`requests.ConnectionError`):
    before that silence is trusted as proof the GPU is free, confirm nothing is still listening on `base_url`'s
    port and no live process references the configured ComfyUI install. Returns True only when both checks ran and
    every process could be classified; False as soon as either finds the server; None (inconclusive - keep holding
    the reservation) on `AccessDenied`, any other psutil failure, a process whose command line could not be read,
    or no configured `comfy_root` to compare against. Never guessed from silence alone."""
    try:
        port = urlparse(base_url).port
    except ValueError:
        port = None
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError, NotImplementedError):
        return None
    if port and any(c.laddr and getattr(c.laddr, "port", None) == port and c.status == psutil.CONN_LISTEN
                     for c in conns):
        return False
    if not comfy_root:
        return None       # without a configured install path the second half of the evidence cannot be gathered
    root_norm = str(Path(comfy_root)).lower()
    inconclusive = False
    try:
        for p in psutil.process_iter(["name", "exe", "cmdline"]):
            try:
                verdict = _references_comfy_root(p, root_norm)
            except (psutil.Error, OSError):
                verdict = None
            if verdict is False:
                continue
            if verdict is True:
                return False
            inconclusive = True     # keep scanning: a positively-found server still outranks an unreadable one
    except (psutil.Error, OSError):
        return None
    return None if inconclusive else True


def _safe_dest(out_dir: Path, ftype: str, subfolder: str, filename: str) -> Path | None:
    """Local destination for one output ComfyUI reports: `<out_dir>/<type>/<subfolder>/<filename>`, or None when the
    server-supplied name cannot be contained. The name is untrusted input: `..`, an absolute path or a drive letter
    must never become a write destination. `type`/`subfolder` are part of an output's identity: two nodes may
    report the same filename in different subfolders, which collapsed into one file when only the filename was
    used."""
    raw_parts = [ftype or "output", *re.split(r"[\\/]+", subfolder or ""), filename or ""]
    parts: list[str] = []
    for raw in raw_parts:
        part = (raw or "").strip()
        if not part or part == ".":
            continue
        if part == ".." or any(ch in _UNSAFE_CHARS for ch in part) or part.rstrip(". ") == "":
            return None
        parts.append(part)
    if len(parts) < 2:                                   # always at least <type>/<filename>
        return None
    dest = out_dir.joinpath(*parts)
    try:
        base, resolved = out_dir.resolve(), dest.resolve()
    except OSError:
        return None
    if base not in resolved.parents:                     # belt and braces: the resolved path must sit under out_dir
        return None
    return dest


class ComfyAdapter(plugin.Adapter):
    name = "comfy"
    gpu = True

    # ------------------------------------------------------------ HTTP (thin wrappers: easy to monkeypatch in tests)

    def _get(self, base_url: str, path: str, timeout: float = 10.0, **kw):
        return requests.get(base_url.rstrip("/") + path, timeout=timeout, **kw)

    def _post(self, base_url: str, path: str, payload: dict, timeout: float = 30.0):
        return requests.post(base_url.rstrip("/") + path, json=payload, timeout=timeout)

    # ------------------------------------------------------------ adapter interface

    def prepare(self, layout: plugin.Layout, task, attempt_id: str) -> dict:
        job = job_spec(task)
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        if job.get("workflow_file"):
            workflow = json.loads((layout.root / job["workflow_file"]).read_text(encoding="utf-8"))
        else:
            workflow = job.get("workflow") or {}
        cfg = comfy_config(layout)
        plugin.atomic_json(run_dir / "workflow.json", workflow)
        # No "deadline" here: prepare() can run long before submit() actually succeeds (VRAM/server waits defer it
        # without re-preparing), so the execution budget is computed in submit() instead - otherwise a job stuck
        # waiting for capacity loses runtime it never got to spend.
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir),
                "root": str(layout.root), "comfy_root": cfg.get("root"), "base_url": job.get("base_url", cfg["base_url"]),
                "timeout_s": int(job.get("timeout_s", cfg["timeout_s"])),
                "vram_floor_mb": int(job.get("vram_mb", cfg["vram_floor_mb"])),
                "client_id": job.get("client_id") or str(uuid.uuid4()),
                "output_patterns": job.get("outputs", ["*"]),
                "defer_seconds": float(cfg["defer_seconds"]), "ws_poll_budget_s": float(cfg["ws_poll_budget_s"]),
                "download_attempts": int(cfg["download_attempts"]), "download_retry_s": float(cfg["download_retry_s"])}

    def submit(self, spec: dict) -> dict:
        run_dir = Path(spec["run_dir"])
        base_url = spec["base_url"]
        floor = spec["vram_floor_mb"]
        defer_s = spec.get("defer_seconds", 30.0)
        if not plugin.read_json(run_dir / "workflow.json"):      # checked first: no VRAM/server wait for a job that can't run
            raise RuntimeError("empty ComfyUI workflow: the task's job block names no workflow/workflow_file "
                               "(a local task without one belongs to its owner role's agent lane)")
        if floor:
            free = plugin.gpu.free_mb()
            if free is not None and free < floor:
                raise plugin.CapacityDeferred(time.time() + defer_s, f"GPU free VRAM {free}MiB below floor {floor}MiB")
        try:
            health = self._get(base_url, "/system_stats", timeout=5.0)
            health.raise_for_status()
        except requests.RequestException as exc:
            raise plugin.CapacityDeferred(time.time() + defer_s, f"ComfyUI unreachable at {base_url}: {exc}") from exc
        workflow = plugin.read_json(run_dir / "workflow.json")
        resp = self._post(base_url, "/prompt", {"prompt": workflow, "client_id": spec["client_id"]}, timeout=30.0)
        if resp.status_code >= 400:
            raise RuntimeError(f"ComfyUI /prompt rejected the workflow ({resp.status_code}): {resp.text[:500]}")
        body = resp.json()
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            errs = body.get("node_errors") or {}
            raise RuntimeError(f"ComfyUI /prompt returned no prompt_id; node_errors={json.dumps(errs)[:500]}")
        # The execution deadline starts here, at actual submission - not in prepare() - and is persisted in the
        # handle: a job deferred for VRAM/server availability keeps its full runtime budget, and recover() (a crash
        # mid-run, a fresh process) restores this same deadline rather than recomputing a new one.
        deadline = time.time() + spec.get("timeout_s", 1800)
        handle = {"kind": "comfy", "prompt_id": prompt_id, "client_id": spec["client_id"], "base_url": base_url,
                  "submitted": plugin.now_iso(), "deadline": deadline}
        plugin.atomic_json(run_dir / "prompt_id.json", handle)   # persisted BEFORE any polling
        return handle

    def recover(self, spec: dict) -> dict | None:
        p = Path(spec["run_dir"]) / "prompt_id.json"
        if not p.exists():
            return None
        return plugin.read_json(p)

    def _deadline(self, spec: dict, handle: dict, run_dir: Path) -> float:
        """The execution deadline: resolved once per job and then held constant for the rest of its life, never
        recomputed from the current clock - a job whose deadline keeps sliding forward on every poll can never
        expire. Two calls for the same (spec, handle, run_dir) return the SAME value even with the clock advanced
        between them. Resolution order, first hit wins: `handle['deadline']` (current submit()) ->
        `spec['deadline']` (a handle written before a prior review fix moved deadline-setting into submit(), still
        legacy-read here) -> `handle['submitted']` (ISO) + `timeout_s` (an even older handle with only a submission
        timestamp) -> a one-time anchor computed from `now` and persisted to `run_dir/deadline.json`, read back on
        every later call, for the case with no deadline information anywhere."""
        d = handle.get("deadline")
        if isinstance(d, (int, float)):
            return d
        d = spec.get("deadline")
        if isinstance(d, (int, float)):
            return d
        submitted = handle.get("submitted")
        if isinstance(submitted, str):
            try:
                return dt.datetime.fromisoformat(submitted).timestamp() + spec.get("timeout_s", 1800)
            except ValueError:
                pass
        anchor_path = run_dir / "deadline.json"
        if anchor_path.exists():
            anchor = plugin.read_json(anchor_path).get("deadline")
            if isinstance(anchor, (int, float)):
                return anchor
        anchor = time.time() + spec.get("timeout_s", 1800)
        plugin.atomic_json(anchor_path, {"deadline": anchor})
        return anchor

    def _queue_fetch(self, base_url: str):
        """GET /queue once; (parsed body, None) on success or (None, exception) on failure. The exception is kept
        (not swallowed) so `_remote_stopped()` can classify *why* the queue was unreadable instead of every failure
        mode collapsing to the same "unknown" or, worse, the same "gone"."""
        try:
            resp = self._get(base_url, "/queue", timeout=10.0)
            resp.raise_for_status()
            return resp.json(), None
        except (requests.RequestException, ValueError, AttributeError) as exc:
            return None, exc

    @staticmethod
    def _parse_queue(q) -> tuple[set, set] | None:
        """(running prompt_ids, pending prompt_ids) from a `/queue` body, or None when the shape cannot be trusted.
        Both `queue_running` and `queue_pending` must be present and be lists, and every entry in them must parse
        (`[number, prompt_id, ...]`) *including its prompt_id*, which must be a non-empty string: `[[0, null]]` is
        a running queue whose real contents are unknown, and reading it as a set that simply does not contain our
        prompt released the GPU reservation of a job still on the card, while `[[0, []]]` raised TypeError out of
        the caller. *Any* malformed entry makes the whole response unknown rather than a partially-trusted empty
        set - a single bad entry must never look like an empty, safe-to-release queue."""
        if not isinstance(q, dict):
            return None
        buckets = []
        for bucket in ("queue_running", "queue_pending"):
            raw = q.get(bucket)
            if bucket not in q or not isinstance(raw, list):
                return None
            ids = set()
            for entry in raw:
                if not (isinstance(entry, list) and len(entry) > 1):
                    return None
                prompt_id = entry[1]
                if not isinstance(prompt_id, str) or not prompt_id.strip():
                    return None       # validated before it is hashed: an unhashable id must not raise out of here
                ids.add(prompt_id)
            buckets.append(ids)
        return buckets[0], buckets[1]

    def _queue_state(self, base_url: str) -> tuple[set, set] | None:
        """(running prompt_ids, pending prompt_ids), or None when the queue could not be read or trusted. An
        unreadable/malformed queue is *unknown*, never "empty": treating it as empty is what released the GPU
        reservation of a job still executing on the server, letting a second GPU job start alongside it."""
        q, _ = self._queue_fetch(base_url)
        return self._parse_queue(q)

    def _cancel(self, base_url: str, prompt_id: str) -> None:
        """Ask ComfyUI to stop this prompt: drop it from the pending queue, and interrupt execution if it is the
        prompt currently running. Best effort - `_remote_stopped()` decides when the GPU slot may be released."""
        try:
            self._post(base_url, "/queue", {"delete": [prompt_id]}, timeout=10.0)
        except requests.RequestException:
            pass
        state = self._queue_state(base_url)
        if state is not None and prompt_id in state[0]:
            try:
                self._post(base_url, "/interrupt", {}, timeout=10.0)   # interrupts the executing prompt (ours: we hold the GPU lock)
            except requests.RequestException:
                pass

    def _remote_stopped(self, base_url: str, prompt_id: str, comfy_root: str | None = None) -> bool | None:
        """True when the prompt is confirmed off the server's queue (the GPU is free again), or when HTTP contact
        is lost *and* an independent OS-level probe confirms the server process itself is gone. False when it is
        still queued or running. None ("unknown, keep holding the reservation") for every ambiguous case -
        `requests.Timeout`, `requests.HTTPError`, a response that doesn't parse as JSON, a malformed queue body, or
        a `ConnectionError` the process probe cannot resolve one way or the other. Lost HTTP contact alone is never
        treated as proof: a slow or erroring-but-alive server must not look the same as a dead one."""
        q, exc = self._queue_fetch(base_url)
        state = self._parse_queue(q)
        if state is not None:
            running, pending = state
            return prompt_id not in running and prompt_id not in pending
        if isinstance(exc, requests.ConnectionError) and not isinstance(exc, requests.Timeout):
            return _process_probe_confirms_stopped(base_url, comfy_root)
        return None   # Timeout, HTTPError, unparseable body, or a malformed-but-200 queue: unknown, keep holding

    def alive(self, handle: dict) -> bool:
        return False   # authoritative liveness comes from /history in collect()

    def _record_peak_vram(self, run_dir: Path) -> None:
        used = plugin.gpu.used_mb()
        if used is None:
            return
        peak_path = run_dir / "vram_peak.json"
        prior = plugin.read_json(peak_path).get("peak_mb", 0) if peak_path.exists() else 0
        if used > prior:
            plugin.atomic_json(peak_path, {"peak_mb": used, "at": plugin.now_iso()})

    def _poll_progress(self, spec: dict) -> None:
        """Best-effort: one short websocket read per collect() call, written to progress.json. Never raises."""
        try:
            import websocket  # websocket-client; optional at import time so a missing package degrades quietly
        except ImportError:
            return
        run_dir = Path(spec["run_dir"])
        budget = spec.get("ws_poll_budget_s", WS_POLL_BUDGET_S_DEFAULT)
        try:
            ws = websocket.create_connection(_ws_url(spec["base_url"], spec["client_id"]), timeout=budget)
        except Exception:
            return
        try:
            ws.settimeout(budget)
            deadline = time.time() + budget
            last = None
            while time.time() < deadline:
                try:
                    msg = ws.recv()
                except Exception:
                    break
                if isinstance(msg, str):
                    try:
                        last = json.loads(msg)
                    except ValueError:
                        continue
            if last is not None:
                plugin.atomic_json(run_dir / "progress.json", last)
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _download(self, spec: dict, dest: Path, filename: str, subfolder: str, ftype: str) -> str | None:
        """Fetch one /view output to `dest`; None on success, else the last error. Retried: a connection that drops
        mid-collection must not silently cost the job its images. Written temp-then-replace so a half-written file is
        never mistaken for a collected output by the next collect()."""
        attempts = int(spec.get("download_attempts", 3))
        retry_s = spec.get("download_retry_s", 1.0)
        last = "not attempted"
        for attempt in range(attempts):
            try:
                resp = self._get(spec["base_url"], "/view",
                                 params={"filename": filename, "subfolder": subfolder, "type": ftype}, timeout=60.0)
                resp.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + ".part")
                tmp.write_bytes(resp.content or b"")
                tmp.replace(dest)
                return None
            except (requests.RequestException, OSError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < attempts:
                time.sleep(retry_s)
        return last

    def _collect_outputs(self, spec: dict, outputs: dict) -> tuple[list[str], list[str]]:
        """Download every image/gif/video ComfyUI reports for this prompt via /view into
        run_dir/output/<type>/<subfolder>/. Returns (artifact paths, failures): an output that will not download, or
        whose server-supplied name does not resolve inside run_dir/output, is a reported failure - never a silent
        omission that lets collect() call the job COMPLETE without it."""
        run_dir = Path(spec["run_dir"])
        out_dir = run_dir / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        root = Path(spec["root"])
        collected, failures = [], []
        for node_id, node_out in sorted(outputs.items()):
            for kind in ("images", "gifs", "videos"):
                for item in node_out.get(kind) or []:
                    filename, subfolder, ftype = item.get("filename"), item.get("subfolder", ""), item.get("type", "output")
                    if not filename:
                        continue
                    ident = f"node {node_id} {ftype}/{subfolder}/{filename}".replace("//", "/")
                    dest = _safe_dest(out_dir, ftype, subfolder, filename)
                    if dest is None:
                        failures.append(f"{ident}: refused, name does not resolve inside the run's output dir")
                        continue
                    if not (dest.exists() and dest.stat().st_size):
                        err = self._download(spec, dest, filename, subfolder, ftype)
                        if err:
                            failures.append(f"{ident}: {err}")
                            continue
                    rel = dest.relative_to(root) if dest.is_relative_to(root) else dest
                    collected.append(str(rel).replace("\\", "/"))
        return collected, failures

    def _log_cancel_state(self, spec: dict, prompt_id: str, remote_stopped, reason: str) -> None:
        """One log event per *change* in `remote_stopped` while a timed-out prompt is being cancelled, so a slot
        stuck holding the GPU reservation (a server that never confirms) is visible rather than silent until an
        operator happens to open the run dir."""
        try:
            layout = plugin.Layout(Path(spec["root"]))
            plugin.log_event(layout, "comfy_cancel_state", task_id=spec.get("task_id"),
                             attempt_id=spec.get("attempt_id"), prompt_id=prompt_id, remote_stopped=remote_stopped,
                             reason=reason)
        except (OSError, ValueError):
            pass   # logging must never be the reason a cancel/expire path raises

    def _expire(self, spec: dict, prompt_id: str, reason: str, peak=None, raw: dict | None = None) -> plugin.Result | None:
        """The deadline passed with the prompt unfinished. A terminal result here settles the job record, and the
        record is the GPU reservation the gate counts: returning FAILED while the prompt is still executing on the
        server admits a second GPU job alongside it. So cancel the prompt and keep returning None - the job stays
        RUNNING and keeps its slot - until the server confirms it is off the queue."""
        run_dir = Path(spec["run_dir"])
        base_url = spec["base_url"]
        cancel_path = run_dir / "cancel_pending.json"
        prior = plugin.read_json(cancel_path).get("remote_stopped", "__unset__") if cancel_path.exists() else "__unset__"
        self._cancel(base_url, prompt_id)            # re-asked each tick: the first delete can race a prompt about to start
        stopped = self._remote_stopped(base_url, prompt_id, spec.get("comfy_root"))
        if stopped is not True:
            if stopped != prior:
                self._log_cancel_state(spec, prompt_id, stopped, reason)
            plugin.atomic_json(cancel_path,
                               {"prompt_id": prompt_id, "reason": reason, "remote_stopped": stopped, "at": plugin.now_iso(),
                                "note": "holding the GPU reservation until ComfyUI confirms this prompt has stopped"})
            return None
        if prior is not True:
            self._log_cancel_state(spec, prompt_id, True, reason)
        cancel_path.unlink(missing_ok=True)
        return plugin.Result(status="FAILED", error=f"{reason} (prompt cancelled, ComfyUI confirms it has stopped)",
                             usage={"peak_vram_mb": peak}, raw=raw or {"prompt_id": prompt_id})

    def collect(self, spec: dict, handle: dict) -> plugin.Result | None:
        run_dir = Path(spec["run_dir"])
        base_url = spec["base_url"]
        prompt_id = handle["prompt_id"]
        self._record_peak_vram(run_dir)
        self._poll_progress(spec)
        expired = time.time() > self._deadline(spec, handle, run_dir)
        try:
            hist = self._get(base_url, f"/history/{prompt_id}", timeout=10.0).json() or {}
        except (requests.RequestException, ValueError, AttributeError):
            return self._expire(spec, prompt_id, "timeout waiting for ComfyUI history") if expired else None
        entry = hist.get(prompt_id)
        if entry is None:
            if not expired:
                return None
            state = self._queue_state(base_url)
            reason = "prompt vanished from ComfyUI queue and history (server restart?)" \
                if state is not None and prompt_id not in state[0] | state[1] else "timeout waiting for ComfyUI history"
            return self._expire(spec, prompt_id, reason)
        status = entry.get("status") or {}
        peak = plugin.read_json(run_dir / "vram_peak.json").get("peak_mb") if (run_dir / "vram_peak.json").exists() else None
        if not status.get("completed", False) and status.get("status_str") not in ("success", "error"):
            return self._expire(spec, prompt_id, "timeout waiting for ComfyUI job to complete", peak=peak, raw=entry) if expired else None
        if status.get("status_str") == "error":
            messages = status.get("messages", [])
            return plugin.Result(status="FAILED", error=f"ComfyUI execution error: {json.dumps(messages)[:500]}",
                                 usage={"peak_vram_mb": peak}, raw=entry)
        artifacts, failures = self._collect_outputs(spec, entry.get("outputs") or {})
        if failures:
            # The prompt itself finished (so the card is free), but outputs it reported are not on disk: this job is
            # not COMPLETE. Retry on the next collect() - what is already downloaded is skipped - and fail loudly at
            # the deadline rather than deliver a generation job with missing images.
            plugin.atomic_json(run_dir / "collect_failures.json", {"prompt_id": prompt_id, "failures": failures, "at": plugin.now_iso()})
            if not expired:
                return None
            return plugin.Result(status="FAILED", artifacts=artifacts, usage={"peak_vram_mb": peak}, raw=entry,
                                 error=f"could not collect {len(failures)} of {len(failures) + len(artifacts)} reported "
                                       f"output(s): " + "; ".join(failures[:5]))
        (run_dir / "collect_failures.json").unlink(missing_ok=True)
        plugin.atomic_json(run_dir / "result.json", {"status": "COMPLETE", "artifacts": artifacts, "peak_vram_mb": peak})
        return plugin.Result(status="COMPLETE", artifacts=artifacts, notes=f"peak VRAM {peak}MiB" if peak else "",
                             usage={"peak_vram_mb": peak}, raw=entry)

    def terminate(self, handle: dict) -> None:
        """Operator kill: drop the prompt from the queue and interrupt it if it is the one executing. Best effort -
        unlike _expire() this cannot wait for confirmation, the command settles the record."""
        base_url = handle.get("base_url")
        prompt_id = handle.get("prompt_id")
        if base_url and prompt_id:
            self._cancel(base_url, prompt_id)


ADAPTER = ComfyAdapter()


# ------------------------------------------------------------------ doctor checks


def _git_head(repo: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


class PinError(Exception):
    """The pin manifest cannot be used to verify the installation."""


def _pins(layout: plugin.Layout, cfg: dict) -> dict:
    """The resolved commit manifest `plugins.comfy.pins_file` names, with a valid commit for ComfyUI and for every
    node `pinned_nodes` lists. A missing, malformed or incomplete manifest raises: an installation that cannot
    prove which commits it is on is not a pinned installation, and reporting it healthy hid exactly that."""
    p = layout.root / cfg["pins_file"]
    nodes = cfg["pinned_nodes"]
    if not p.exists():
        extra = f" and every pinned node ({', '.join(nodes)})" if nodes else ""
        raise PinError(f"{p} missing; generate it with the resolved commit for ComfyUI{extra} (plugins.comfy.pins_file)")
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise PinError(f"{p} unreadable ({exc}); regenerate the pin manifest")
    if not isinstance(data, dict):
        raise PinError(f"{p} is not a commit map; regenerate the pin manifest")
    pins = {}
    for name in ["ComfyUI", *nodes]:
        sha = data.get(name)
        sha = sha.strip().lower() if isinstance(sha, str) else None
        if not sha or not _SHA.fullmatch(sha):
            raise PinError(f"{p}: no valid pinned commit for {name} (got {data.get(name)!r}); regenerate the pin manifest")
        pins[name] = sha
    return pins


def _at_pin(head: str | None, pin: str) -> bool:
    return bool(head) and head.strip().lower().startswith(pin)   # a pin may be abbreviated; a HEAD we cannot read never matches


def check_comfy_install(layout: plugin.Layout) -> plugin.Check:
    cfg = comfy_config(layout)
    root = Path(cfg.get("root") or "")
    if not root.exists() or not (root / "main.py").exists():
        return plugin.Check("comfy_install", False, f"{root} missing or has no main.py; install ComfyUI there")
    try:
        pins = _pins(layout, cfg)
    except PinError as exc:
        return plugin.Check("comfy_install", False, str(exc))
    head = _git_head(root)
    if head is None:
        return plugin.Check("comfy_install", False, f"could not read git HEAD of {root} (not a git checkout, or "
                                                      "git missing); the pin cannot be verified")
    if not _at_pin(head, pins["ComfyUI"]):
        return plugin.Check("comfy_install", False, f"ComfyUI HEAD {head} != pinned {pins['ComfyUI']}")
    missing, unreadable, mismatched = [], [], []
    for node in cfg["pinned_nodes"]:
        node_dir = root / "custom_nodes" / node
        if not node_dir.exists():
            missing.append(node)
            continue
        node_head = _git_head(node_dir)
        if node_head is None:
            unreadable.append(node)
        elif not _at_pin(node_head, pins[node]):
            mismatched.append(f"{node} ({node_head[:12]} != {pins[node][:12]})")
    if missing:
        return plugin.Check("comfy_install", False, f"missing custom nodes: {', '.join(missing)}")
    if unreadable:
        return plugin.Check("comfy_install", False, f"could not read git HEAD of custom nodes: {', '.join(unreadable)}")
    if mismatched:
        return plugin.Check("comfy_install", False, f"custom nodes not at pinned commit: {', '.join(mismatched)}")
    nodes_msg = f", nodes at pinned commits: {', '.join(cfg['pinned_nodes'])}" if cfg["pinned_nodes"] else ""
    return plugin.Check("comfy_install", True, f"{root} @ {head[:12]} (pinned){nodes_msg}")


def check_comfy_api(layout: plugin.Layout) -> plugin.Check:
    cfg = comfy_config(layout)
    try:
        resp = requests.get(cfg["base_url"].rstrip("/") + "/system_stats", timeout=5.0)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return plugin.Check("comfy_api", False, f"{cfg['base_url']} unreachable ({exc}); start ComfyUI's server")
    return plugin.Check("comfy_api", True, f"{cfg['base_url']} healthy")


def check_ffmpeg(layout: plugin.Layout) -> plugin.Check:
    cfg = comfy_config(layout).get("ffmpeg") or {}
    root = Path(cfg.get("root") or "")
    exe = root / "bin" / "ffmpeg.exe"
    if not exe.exists():
        return plugin.Check("ffmpeg", False, f"{exe} missing; install ffmpeg there (plugins.comfy.ffmpeg.root)")
    try:
        out = subprocess.run([str(exe), "-version"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return plugin.Check("ffmpeg", False, f"{exe} did not run: {exc}")
    if out.returncode != 0:
        return plugin.Check("ffmpeg", False, f"{exe} -version exited {out.returncode}")
    return plugin.Check("ffmpeg", True, out.stdout.splitlines()[0] if out.stdout else "ok")


def check_gpu(layout: plugin.Layout) -> plugin.Check:
    """A single-card probe (`supervisor.plugin.gpu`, nvidia-smi): free/total VRAM against the configured floor. A
    project with more than one card (several roles to resolve, an index to pick) keeps its own role-aware check
    beside this one - this package has no notion of which physical card is which role."""
    if not plugin.gpu.available():
        return plugin.Check("gpu", False, "nvidia-smi unavailable (no NVIDIA driver on PATH)")
    free, total = plugin.gpu.free_mb(), plugin.gpu.total_mb()
    if free is None or total is None:
        return plugin.Check("gpu", False, "nvidia-smi did not report free/total memory")
    gpu_cfg = plugin.resources(layout)["gpu"]
    floor = int(gpu_cfg.get("usable_vram_floor_mb", 0))
    return plugin.Check("gpu", True, f"{free}MiB free / {total}MiB total (floor {floor}MiB)")


def gpu_panel(layout: plugin.Layout) -> dict:
    """Overview resource block (registry.overview_panel): the single card `supervisor.plugin.gpu` reads."""
    if not plugin.gpu.available():
        return {"title": "GPU", "rows": [("VRAM", "nvidia-smi unavailable")], "progress": None}
    free, total = plugin.gpu.free_mb(), plugin.gpu.total_mb()
    if free is None or total is None:
        return {"title": "GPU", "rows": [("VRAM", "unknown")], "progress": None}
    used = total - free
    return {"title": "GPU", "rows": [("VRAM used", f"{used / 1024:.1f} / {total / 1024:.1f} GB"), ("free", f"{free} MiB")],
            "progress": (used, total)}


def setup(registry) -> None:
    """The ComfyUI lane (holds the GPU slot), its generic doctor checks and overview panel. The `profiles` and
    `venv` checks stay with the project that owns them (boundary plan phase 5, decision D4)."""
    registry.adapter(ADAPTER)
    for fn in (check_comfy_install, check_comfy_api, check_ffmpeg, check_gpu):
        registry.doctor_check(fn)
    registry.overview_panel(gpu_panel)
