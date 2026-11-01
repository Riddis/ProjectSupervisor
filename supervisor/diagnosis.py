"""The exception route: manager → architect → owner.

When supervisor.escalation stops retrying (same failure signature twice, or the revision budget spent) it used to open an
issue saying "owner decision needed" and stop. Now it also dispatches ONE bounded diagnosis job to the manager role
(`senior_pm` by default) on a read-only lane, with the failure evidence frozen in the brief. The diagnosis returns a
structured decision:

    requeue   — the task goes back to READY with written guidance that the next delivery brief carries; the retry
                budget is reset once (DIAGNOSIS_BUDGET diagnoses per task, then straight to the owner);
    defer     — the task stays BLOCKED with what must happen first written on it and on the issue;
    architect — the question is design, not execution: one more diagnosis by the architect role (never a third level);
    owner     — a product choice or an action only the owner can perform: one inbox item, the only way this route
                reaches the owner's attention.

A failed or malformed diagnosis never loops: it falls through to `owner`. One diagnosis per (task, signature, level),
checked against the job records, so a replayed escalation cannot spend a second one. The job goes through
Supervisor.dispatch → launch → supervisor.gate like every other job (decision, capacity floor, agent limit)."""
from __future__ import annotations

import re

import yaml

from . import inbox as inbox_mod, jobs as jobs_mod, scheduler, store, tasks as tasks_mod
from ._vendor.contract import CapacityDeferred, contained
from .adapters import REGISTRY
from .models.task import Task, TaskStatus
from .paths import Layout

KIND = "diagnosis"
ACTIONS = ("requeue", "defer", "architect", "owner")
LEVELS = ("manager", "architect")
DEFAULT_ROLES = {"manager": "senior_pm", "architect": "chief_architect"}
DIAGNOSIS_BUDGET = 2            # requeues granted by diagnosis per task; after that the owner decides

# A `requeue` on a non-agent (process) job may also carry a corrected command — the one thing a bare requeue cannot
# repair, because escalation.dispatch_revision rebuilds the job from scheduler.job_spec() unchanged. A diagnosis
# never supplies `argv` itself: `job_fix` may only ever name `params`, mapping a NAME the job already declared
# (in its own `fix_params`, written only by task.set_job/commands.apply_job, never by a diagnosis) to a new value.
# `fix_params[name]` fixes which argv slot that name corrects (`index`) and how its value is checked (`pattern`, a
# regex the value must fullmatch, or `kind: "path"`, which instead requires the value to resolve inside the
# project). Every other field of the job (its adapter, every limit, owned_paths, ...) is untouched, whatever a
# diagnosis proposes — task.set_job (via commands.apply_job) stays the single writer, replacing only `argv`, and
# every application or refusal is logged (diagnosis_job_fixed).
#
# Which indices are even eligible is not PARAM_MIN_INDEX alone: that is only the absolute floor, never 0 or 1 for
# any command. The real boundary is _command_prefix(layout, argv) — this argv's own layout, read fresh from the
# CURRENT job every time, never from fix_params — the length of the trusted, immutable executable+script prefix
# a diagnosis can never move into, or None when no index at all is eligible: a layout this cannot read (argv[1]
# is not an actual file, so it cannot even confirm that is the script), an executable whose child re-parses its
# own command line in every invocation this interface recognises (SHELL_REPARSING — cmd.exe /c, powershell
# -Command, sh/bash -c; POWERSHELL_EXES — PowerShell's own flagless default does the same thing its -Command
# option does), or an argv[0] the OS itself hands to a secondary command processor regardless of argv
# (REPARSED_EXECUTABLE_SUFFIXES — a `.bat`/`.cmd` target, re-invoked through cmd.exe even with shell=False) —
# where no index past the wrapper is provably safe, because the child's own parser, not an index, decides where
# one argument ends. The old check assumed argv[1] was the script whenever it did not start with "-", which is
# what let `cmd.exe /c <script>` move the real script to argv[2] while looking, to that check, exactly like a
# trusted argv[1] script (round-2 review F1); registering that wrapper's argv[2] as a 3-slot "trusted prefix"
# with a correctable index past it was a second, narrower version of the same mistake — cmd.exe re-parses
# whatever it is handed, so a `kind: path` value with no spaces and an "&" in it still runs a second program. A
# later check assumed every OTHER argv[0]/argv[1] pair not on that short list was safe once argv[1] resolved to a
# file on disk, which is what let PowerShell's flagless default and a native `.bat`/`.cmd` executable at argv[0]
# both re-parse a value past that same prefix the same way (round-4 review F8). That suffix test still read the
# literal argv[0] text a job wrote, not the name Win32 CreateProcess actually resolves — a trailing space or dot
# is stripped when Windows resolves the target and still routed through cmd.exe, and a launcher (env.exe) whose
# own argv[1] is the batch file re-opens the same reparse one slot to the right, since the suffix test only ever
# inspected argv[0]. _resolved_name now reads what Windows actually resolves (trailing spaces/dots stripped, an
# `::$DATA` alternate-data-stream spelling folded to the plain name) for both argv[0] and argv[1], and
# _reparses_regardless_of_argv applies the SAME re-parsing-family test (REPARSED_EXECUTABLE_SUFFIXES,
# POWERSHELL_EXES, and now WSL_EXES — wsl.exe's own default hands its whole tail to a Linux shell, unobservable
# on a host whose default distribution never starts but not thereby exempt) to whichever slot the reparsing
# executable occupies (round-5 review F10).
JOB_FIX_FIELDS = {"process": ("params",)}
PARAM_MIN_INDEX = 2             # absolute floor for every layout: argv[0]/argv[1] are never a declared parameter

# A wrapper whose child re-parses its own command line (cmd.exe /c|/k, powershell/pwsh -Command, sh/bash/zsh -c)
# has NO slot that is provably safe to mutate, however far past argv[1] it sits: the child's own parser, not this
# interface's index arithmetic, decides where one argument ends and the next command begins, and a value that is
# otherwise a perfectly ordinary in-project path (one with "&", "|", ";", a space, ...) can make the wrapper run
# a second, different program (round-2 review F1, architecture note: a `kind: path` value containing "&" passes
# `contained()` — that only resolves and compares roots — and an argument list2cmdline leaves unquoted because it
# has no spaces is exactly what cmd.exe then splits on). SHELL_REPARSING is therefore not a registry of layouts
# with a usable prefix at all: matching it means "no eligible index", full stop — the whole command is refused
# for a job_fix, the same as a layout this interface cannot read.
SHELL_REPARSING = {("cmd.exe", "/c"), ("cmd.exe", "/k"), ("cmd", "/c"), ("cmd", "/k"),
                   ("powershell.exe", "-command"), ("powershell", "-command"),
                   ("pwsh.exe", "-command"), ("pwsh", "-command"),
                   ("sh", "-c"), ("sh.exe", "-c"), ("bash", "-c"), ("bash.exe", "-c"), ("zsh", "-c")}

# Round-4 review F8: SHELL_REPARSING keys a wrapper's re-parsing by its EXACT flag, which missed the one invocation
# none of its own entries name — PowerShell's own default, flagless mode. Launched with neither -Command nor -File,
# PowerShell still binds everything past its own name to the same re-parsed script-text parameter -Command uses;
# there is no flagless or otherwise-flagged PowerShell invocation this interface has established safe, so the whole
# executable family is refused regardless of argv[1] — not only the one flag SHELL_REPARSING happens to list.
POWERSHELL_EXES = {"powershell.exe", "powershell", "pwsh.exe", "pwsh"}

# Round-4 review F8: Windows resolves a `.bat`/`.cmd` target by re-invoking it through its own command processor
# even when the caller never asks for a shell (`subprocess.Popen(argv, ...)`, no `shell=True`) — the "executable"
# position at argv[0] is itself a reparsing wrapper selected by file extension, not by name. No index past argv[0]
# is provably safe for such a command, the same as for any SHELL_REPARSING/POWERSHELL_EXES entry.
REPARSED_EXECUTABLE_SUFFIXES = (".bat", ".cmd")

# Round-5 review F10: wsl.exe, launched without --exec, hands its entire tail — not only argv[1] — to the
# default WSL distribution's own login shell as one re-parsed command line, the same invariant the
# SHELL_REPARSING sh/bash "-c" entries are refused for, reached through a first-party Windows launcher instead
# of an explicit shell. This host's default distribution fails to attach its disk, so the form could not be
# observed directly (review lines 72-74); an unobservable form is not an exempt one, so the whole executable is
# refused regardless of argv[1], the same way POWERSHELL_EXES is refused regardless of its own flag.
WSL_EXES = {"wsl.exe", "wsl"}


def _resolved_name(token) -> str:
    """The basename Win32 CreateProcess actually resolves for one argv token, not the literal text a job wrote
    (round-5 review F10). Win32 path resolution strips trailing spaces and dots from a file name before it
    looks the file up, so a `plain.cmd ` or `plain.cmd.` argv[0] still resolves to, and Windows still
    re-invokes through cmd.exe as, `plain.cmd` — a suffix test that reads the literal, unstripped text
    disagrees with the OS it is reasoning about. An explicit `::$DATA` alternate-data-stream spelling names the
    same (default, unnamed) stream as the plain file, so `plain.cmd::$DATA` resolves the same way too."""
    name = str(token).replace("\\", "/").rsplit("/", 1)[-1]
    name = name.split("::", 1)[0]
    return name.rstrip(" .").lower()


def _reparses_regardless_of_argv(name: str) -> bool:
    """Whether the already-resolved basename `name` is an executable the OS hands off to a secondary command
    processor, or an executable family with no invocation this interface has established safe, whatever the
    rest of argv is: a `.bat`/`.cmd` target (REPARSED_EXECUTABLE_SUFFIXES), the whole PowerShell family
    (POWERSHELL_EXES), or wsl.exe without --exec (WSL_EXES)."""
    return name.endswith(REPARSED_EXECUTABLE_SUFFIXES) or name in POWERSHELL_EXES or name in WSL_EXES


def _command_prefix(layout: Layout, argv: list) -> int | None:
    """The length of the trusted, immutable executable+script prefix for argv's own layout, or None when no index
    is eligible at all: too few tokens to have a script, an argv[0] the OS itself hands to a secondary command
    processor regardless of argv (REPARSED_EXECUTABLE_SUFFIXES — a `.bat`/`.cmd` target), an executable whose
    every invocation this interface recognises re-parses its own command line (SHELL_REPARSING's exact flag, or
    an executable family with no safe invocation at all — POWERSHELL_EXES, whose own flagless default re-parses
    just as its explicit -Command form does, or WSL_EXES, whose own default hands its whole tail to a Linux
    shell — no slot is safe, not even one), a launcher whose argv[1] is itself one of those same re-parsing
    forms (round-5 review F10: env.exe execs argv[1] as the real program, re-opening the same reparse one slot
    to the right — the F8 mechanism moved one slot to the right, read on the executable Windows actually
    resolves there, not on what argv[0] happens to be), an argv[1] that the command's own
    grammar never treats as a script at all (anything "-"-prefixed is an option/flag/inline-snippet introducer —
    "-c", "-m", "-e", ..., for every interpreter, not only the ones named above), or an argv[1] that is not an
    actual file (so this cannot even confirm it is looking at a script rather than a subcommand or bare word).
    Checking "is this an existing file" only AFTER ruling out every re-parsing form is what makes the one plain
    layout this interface does understand (argv[0] executable, argv[1] script) read the same way on every platform
    without a filesystem collision turning an option into a script: an absolute POSIX script path starts with "/"
    too, and is not an option, but a project that happens to contain a file literally named "-c" must not make
    `-c` one either (round-3 review F5) — the grammar, and what the OS itself resolves for argv[0] and argv[1],
    not what happens to exist on disk or the spelling a job happened to write, decide which token is the
    script (round-5 review F10)."""
    if len(argv) < 2:
        return None
    exe_name = _resolved_name(argv[0])
    if _reparses_regardless_of_argv(exe_name):
        return None
    if _reparses_regardless_of_argv(_resolved_name(argv[1])):
        return None
    option = str(argv[1]).lower()
    if (exe_name, option) in SHELL_REPARSING:
        return None
    script = str(argv[1])
    if script.startswith("-"):
        return None
    try:
        if not (layout.root / script).is_file():
            return None
    except OSError:
        return None
    return PARAM_MIN_INDEX


RESULT_CONTRACT = (
    "## Result contract\nEnd your answer with exactly one fenced JSON block:\n"
    '```json\n{"kind":"diagnosis","schema_version":1,"action":"requeue|defer|architect|owner","summary":"one paragraph: '
    'what actually went wrong","guidance":"(requeue) concrete instructions for the next attempt","job_fix":"(requeue, '
    'optional, process jobs only) {\\"params\\": {\\"<name>\\": \\"<value>\\"}} — a correction for a parameter the '
    'job itself already declares (its own fix_params), e.g. a stale --sha; never a new command. Omit unless the job '
    '(not the agent) is wrong. A name the job did not declare, or a value that fails its declared check, is dropped, '
    'not applied. Every other field of the job (its adapter, timeout, argv positions it did not declare) is kept '
    'exactly as it was, whatever this names","blocked_on":"(defer) what must happen first","question":"(owner) the '
    'one decision or action only the owner can take"}\n```\n'
    "`requeue` means you are confident a fresh attempt with your guidance will pass the SAME verification and reviews; "
    "do not requeue to try the same thing again. `architect` is for a design question, not an execution problem. "
    "`owner` is for product choices, spending, external commitments, or physical actions on the workstation.\n")


def settings(layout: Layout) -> dict:
    cfg = layout.config / "routing.yaml"
    out = {"enabled": True, "roles": dict(DEFAULT_ROLES)}
    if cfg.exists():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
        esc = data.get("escalation") or {}
        if "diagnosis" in esc:
            out["enabled"] = bool(esc["diagnosis"])
        for level in LEVELS:
            if esc.get(f"{level}_role"):
                out["roles"][level] = str(esc[f"{level}_role"])
    return out


def _records(layout: Layout, task_id: str) -> list[dict]:
    return [r for r in jobs_mod.for_task(layout, task_id) if (r.get("spec") or {}).get("kind") == KIND]


def already(layout: Layout, task_id: str, signature: str, level: str, grant: int = 0) -> dict | None:
    """`grant` (`escalation.current_grant`) excludes a diagnosis dispatched under an earlier grant generation (R4):
    a repeat failure after a fresh grant must reach its OWN diagnosis, not the one the spent grant already answered."""
    for r in _records(layout, task_id):
        if (r.get("diag_signature") == signature and r.get("diag_level") == level and r.get("phase") != "FAILED"
                and int(r.get("diag_grant") or 0) == grant):
            return r
    return None


def count_requeues(layout: Layout, task_id: str) -> int:
    return sum(1 for r in _records(layout, task_id) if r.get("diag_action") == "requeue")


def brief(layout: Layout, task: Task, role: str, level: str, source: str, detail: str, issue_id: str | None) -> str:
    from . import packets
    card = packets.role_card(layout, role)
    attempts = "\n".join(f"- {a.id} ({a.resource}): {a.note}" for a in task.attempts[-6:]) or "- none recorded"
    notes = "\n".join(f"- {n.get('by')}: {n.get('note')}" for n in ((task.model_extra or {}).get("notes") or [])[-6:]) or "- none"
    levels = ("requeue, defer, architect or owner" if level == "manager" else "requeue, defer or owner")
    text = ((card + "\n\n") if card else "") + (
        f"# Diagnosis for {task.id}: {task.title}\n\nRole: {role} ({level} level). Repository: {layout.root}\n\n"
        f"Automatic retries for this task have stopped. Decide what happens next — {levels}. Read the repository and "
        f"the run directories under runs/{task.id}/; do not modify anything.\n\n"
        + packets.contract_block(task) +
        "\n## Expected artifacts\n" + "\n".join(f"- {a}" for a in task.artifacts) +
        f"\n\n## Why retries stopped ({source})\n```\n{detail[:4000]}\n```\n\n## Attempts\n{attempts}\n\n## Notes on the task\n{notes}\n"
        + (f"\n## Issue\n{issue_id}\n" if issue_id else "") + "\n" + RESULT_CONTRACT)
    from . import policy
    return policy.assert_packet_clean(text, layout, f"diagnosis packet {task.id}")


def _task_view(layout: Layout, task: Task, role: str, adapter_name: str, prompt: str, overrides: dict) -> Task:
    from . import review
    doc = task.model_dump(mode="json")
    doc["owner_role"] = role
    doc["job"] = {"adapter": adapter_name, "role": role, "prompt": prompt, "result_kind": KIND,
                  **review.review_lane(layout, role), **overrides}
    return Task.model_validate(doc)


def dispatch(supervisor, task: Task, source: str, detail: str, signature: str, level: str = "manager",
             issue_id: str | None = None, grant: int = 0) -> dict | None:
    """The diagnosis job for (task, signature, level) under this grant generation (`grant`, R4): the EXISTING
    record when one already matches (a replayed hand-off reuses a transition that is already durable — round-3
    review F7 — never a fresh dispatch, so a second architect for the same signature and generation is still
    never created), a freshly dispatched one otherwise, or None when the route is disabled, the role's lane is
    unavailable, or its capacity is out (the escalation then stands as an issue + BLOCKED task, as before). None
    therefore only ever means "no diagnosis is running for this" — a caller that reads it as "unavailable" is
    never looking at one that is in fact already under way."""
    from . import review
    layout = supervisor.layout
    cfg = settings(layout)
    if not cfg["enabled"] or level not in LEVELS:
        return None
    existing = already(layout, task.id, signature, level, grant=grant)
    if existing:
        return existing
    role = cfg["roles"][level]
    try:
        adapter_name, overrides, same_vendor = review.lane_for(layout, role)
    except CapacityDeferred:
        tasks_mod.log(layout, "diagnosis_deferred", "escalation", task=task.id, role=role, reason="capacity")
        return None
    if adapter_name not in REGISTRY:
        return None
    view = _task_view(layout, task, role, adapter_name, brief(layout, task, role, level, source, detail, issue_id), overrides)
    rec = supervisor.dispatch(view, adapter_name, note=f"diagnosis:{role}", kind=KIND,
                              extra={"diag_signature": signature, "diag_level": level, "diag_issue": issue_id,
                                     "diag_source": source, "diag_detail": detail[:4000], "diag_role": role,
                                     "diag_grant": grant})
    tasks_mod.log(layout, "diagnosis_dispatched", "escalation", task=task.id, role=role, level=level, issue=issue_id,
                  attempt=(rec or {}).get("attempt_id"))
    return rec


def rebuild(layout: Layout, rec: dict) -> Task | None:
    """Task view for a capacity continuation of a diagnosis job."""
    task = tasks_mod.load(layout, rec["task_id"])
    role, level = rec.get("diag_role"), rec.get("diag_level")
    if not role or task.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED):
        return None
    adapter_name = (rec.get("spec") or {}).get("adapter", "claude_cli")
    return _task_view(layout, task, role, adapter_name,
                      brief(layout, task, role, level, rec.get("diag_source", ""), rec.get("diag_detail", ""), rec.get("diag_issue")), {})


def _annotate_issue(layout: Layout, issue_id: str | None, **fields) -> None:
    if not issue_id:
        return
    p = layout.issues / f"{issue_id}.yaml"
    if not p.exists():
        return
    def fn(doc):
        doc.setdefault("diagnosis", []).append({"at": store.now_iso(), **fields})
        return doc
    try:
        store.update(p, fn, lock_path=layout.state / ".issues.lock")
    except (OSError, ValueError):
        pass


def _param_ok(layout: Layout, spec: dict, value) -> bool:
    """Whether `value` satisfies the job's own declared check for one `fix_params` entry. Never trusts the
    diagnosis for anything but the value itself: the slot (`index`), the check (`pattern`/`kind`) and (for a
    path) the root it must resolve inside all come from the job, which only task.set_job ever writes."""
    if not isinstance(value, str) or not value:
        return False
    index = spec.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or index < PARAM_MIN_INDEX:
        return False
    if spec.get("kind") == "path":
        try:
            return contained(layout.root / value, [layout.root], root=layout.root)
        except (OSError, ValueError):
            return False
    pattern = spec.get("pattern")
    return isinstance(pattern, str) and re.fullmatch(pattern, value) is not None


def propose_job_fix(layout: Layout, task: Task, role: str, proposed) -> dict | None:
    """Apply a diagnosis-proposed job correction through the declared interface only: `proposed` may name nothing
    but `params` (JOB_FIX_FIELDS[adapter]), and every entry in `params` must name a parameter the job itself
    already declared in its own `fix_params` (never written by a diagnosis) and supply a value that parameter's
    declared check accepts. None when there is nothing to apply (an agent job, an unlisted adapter, or no usable
    fields) — the ordinary `requeue` guidance still runs in that case. A name or value that fails is dropped, never
    applied; only the job's `argv`, and only at the declared, validated slots, is ever rewritten — `new_job` starts
    from the CURRENT job with a copy of its `argv`, so the adapter, every limit, owned_paths and every other field
    (including `fix_params` itself) keep their current value whatever `proposed` names."""
    if not isinstance(proposed, dict) or not proposed:
        return None
    from . import commands as commands_mod
    current = scheduler.job_spec(task)
    allowed = JOB_FIX_FIELDS.get(current.get("adapter"))
    if not allowed:
        return None
    dropped = sorted(set(proposed) - set(allowed))
    params_proposed = proposed.get("params") if "params" in allowed else None
    schema = current.get("fix_params") or {}
    argv = list(current.get("argv") or [])
    # Nothing is a mutable parameter unless this argv's own layout can be read (_command_prefix) AND the declared
    # index lands at or after the trusted prefix that layout establishes — never the executable, never an
    # interpreter option, never the script, whichever slot it actually occupies for this command (the pinning gap
    # round-2 review F1 closes: a layout this cannot read, a shell wrapper that re-parses its own command line,
    # or an index inside its trusted prefix, has no eligible slot at all, so a diagnosis can never widen what
    # "the script" means by declaring one).
    prefix_len = _command_prefix(layout, argv)
    applied_params: dict = {}
    if isinstance(params_proposed, dict):
        for name, value in params_proposed.items():
            spec = schema.get(name)
            if (prefix_len is not None and isinstance(spec, dict) and isinstance(spec.get("index"), int)
                    and not isinstance(spec.get("index"), bool) and spec["index"] >= prefix_len
                    and _param_ok(layout, spec, value) and spec["index"] < len(argv)):
                argv[spec["index"]] = value
                applied_params[name] = value
            else:
                dropped.append(f"params.{name}")
    elif "params" in proposed:
        dropped.append("params")
    dropped = sorted(dropped)
    if not applied_params:
        if dropped:
            tasks_mod.log(layout, "diagnosis_job_fixed", "escalation", task=task.id, role=role, applied={}, dropped=dropped)
            return {"applied": {}, "dropped": dropped}
        return None
    commands_mod.apply_job(layout, task.id, {**current, "argv": argv}, actor=f"diagnosis:{role}")
    applied = {"params": applied_params}
    tasks_mod.log(layout, "diagnosis_job_fixed", "escalation", task=task.id, role=role, applied=applied, dropped=dropped)
    return {"applied": applied, "dropped": dropped}


def route_to_owner(layout: Layout, task: Task, rec: dict, summary: str, question: str, why: str) -> dict:
    """Hand a stopped task to the owner as a `decision` item (inbox.apply_decision acts on the answer). The question
    and the options lead the text: the inbox card shows the first MAX_SUMMARY_CHARS and keeps the full diagnosis
    behind "Show full message" (before 1.4.0 the diagnosis came first and a long one cut the question off)."""
    role = rec.get("diag_role", "diagnosis")
    head = (f"{question.strip() if question and question.strip() else 'How do you want to proceed?'} "
            f"REQUEUE = re-run the task (text you add is passed on as guidance); CANCEL = cancel it; "
            f"DEFER = leave it stopped. ")
    text, detail = inbox_mod.fit(
        head + f"Why: {task.id} stopped after repeated failures and the {role} could not resolve it ({why}). {summary}")
    try:
        item = inbox_mod.create(layout, for_role="owner", subject=f"Decision needed: {task.id} — {task.title}", summary=text,
                                detail=detail, kind=inbox_mod.DECISION_KIND,
                                allowed_responses=list(inbox_mod.DECISION_RESPONSES), free_text=True,
                                from_="escalation", task_id=task.id, extra={"resume": "task"})
        return {"action": "owner", "inbox": item["id"]}
    except inbox_mod.InboxFull:
        tasks_mod.add_note(layout, task.id, f"owner decision needed ({why}); inbox full — see issue", actor="escalation")
        return {"action": "owner", "inbox": None}


def ingest(supervisor, rec: dict, result) -> dict:
    """Apply a diagnosis result. Writes the decision on the job record (`diag_action`) first, so a crash between the
    record and the task store replays the same branch and never grants a second requeue."""
    from . import escalation
    layout = supervisor.layout
    task = tasks_mod.load(layout, rec["task_id"])
    obj = (result.raw or {}) if isinstance(result.raw, dict) else {}
    action = str(obj.get("action") or "").lower()
    summary = str(obj.get("summary") or "")[:4000]     # the owner route keeps all of it (inbox `detail`)
    level, role, issue_id = rec.get("diag_level", "manager"), rec.get("diag_role", "?"), rec.get("diag_issue")
    if result.status == "FAILED" or action not in ACTIONS or (level == "architect" and action == "architect"):
        why = result.error or (f"action {action!r} not allowed at {level} level" if action else "no structured diagnosis")
        tasks_mod.log(layout, "diagnosis_failed", "loop", task=task.id, role=role, level=level, error=str(why)[:300])
        action, summary = "owner", summary or f"The {role} diagnosis did not produce a usable decision ({why})."
    if task.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED):
        tasks_mod.log(layout, "diagnosis_ignored", "loop", task=task.id, reason=f"task is {task.status.value}")
        return {"action": "ignored"}
    jobs_mod.set_phase(layout, rec["attempt_id"], rec["phase"], diag_action=action)
    _annotate_issue(layout, issue_id, role=role, level=level, action=action, summary=summary)
    tasks_mod.log(layout, "diagnosis", "loop", task=task.id, role=role, level=level, action=action, summary=summary[:300])

    if action == "requeue":
        if count_requeues(layout, task.id) > DIAGNOSIS_BUDGET:
            return route_to_owner(layout, task, rec, summary, str(obj.get("question") or ""),
                                   f"diagnosis budget of {DIAGNOSIS_BUDGET} requeues spent")
        if task.held:
            # The owner's one explicit stop control, durable on the task itself (round-4 F9): a requeue this
            # diagnosis proposes is declined, not granted and left to be undone later, so nothing here resets
            # the retry signatures or grants a fresh continuation budget - the task stays held, with a note,
            # until the owner's own action (tasks.set_status with an owner actor) releases it.
            held_note = f" ({task.held['note']})" if task.held.get("note") else ""
            tasks_mod.add_note(layout, task.id,
                               f"{role} diagnosis: requeue declined — task is held by the owner{held_note}; "
                               "release the Hold to resume", actor="escalation")
            return {"action": "held", "held_by": task.held.get("by")}
        guidance = str(obj.get("guidance") or summary)[:4000]

        def fn(doc):
            doc.setdefault("guidance", []).append({"at": store.now_iso(), "by": role, "text": guidance})
        tasks_mod._update(layout, task.id, fn)
        fix = propose_job_fix(layout, task, role, obj.get("job_fix"))
        # Explicit, replay-safe grant (R4): keyed by this diagnosis job's own attempt id, so a crash between this
        # write and the record's own DONE write (ingest() is re-run on the same rec/result) spends the GRANT only
        # once. grant_continuations() itself is called every time regardless (F2): it is generation-scoped, not
        # gated on `granted`, so a crash between the reset and the retirement, or partway through the retirement
        # loop, is finished on the next replay instead of leaving old markers counted forever against the new grant.
        escalation.reset_retries(layout, task.id, reason=f"requeued by {role} diagnosis", key=rec["attempt_id"])
        escalation.grant_continuations(layout, task.id, reason=f"requeued by {role} diagnosis")
        if task.status in (TaskStatus.BLOCKED, TaskStatus.FAILED):
            tasks_mod.set_status(layout, task.id, TaskStatus.READY, actor="escalation",
                                 note=f"{role} diagnosis: requeue — {summary[:200]}")
        else:
            tasks_mod.add_note(layout, task.id, f"{role} diagnosis: requeue — {summary[:200]}", actor="escalation")
        out = {"action": "requeue", "guidance": guidance}
        if fix:
            out["job_fix"] = fix
        return out
    if action == "defer":
        blocked_on = str(obj.get("blocked_on") or summary)[:1000]
        tasks_mod.add_note(layout, task.id, f"{role} diagnosis: deferred — blocked on: {blocked_on[:300]}", actor="escalation")
        if task.status == TaskStatus.IN_PROGRESS:
            tasks_mod.set_status(layout, task.id, TaskStatus.BLOCKED, actor="escalation", note=f"deferred: {blocked_on[:200]}")
        return {"action": "defer", "blocked_on": blocked_on}
    if action == "architect":
        tasks_mod.add_note(layout, task.id, f"{role} diagnosis: design question → architect. {summary[:200]}", actor="escalation")
        # Carry this diagnosis's OWN grant generation (F4): `already()`/`dispatch` dedupe an architect request by
        # (signature, level, grant), and omitting it here defaulted to 0, which let a generation-1 manager's
        # architect request match the spent generation-0 architect record and wrongly report the lane unavailable.
        nxt = dispatch(supervisor, task, rec.get("diag_source", "diagnosis"), rec.get("diag_detail", "") or summary,
                       rec.get("diag_signature", ""), level="architect", issue_id=issue_id, grant=rec.get("diag_grant") or 0)
        if nxt is None:                                        # no architect lane right now: the owner hears about it
            return route_to_owner(layout, task, rec, summary, str(obj.get("question") or ""), "architect lane unavailable")
        return {"action": "architect", "attempt": nxt.get("attempt_id")}
    tasks_mod.add_note(layout, task.id, f"{role} diagnosis: owner decision — {summary[:200]}", actor="escalation")
    return route_to_owner(layout, task, rec, summary, str(obj.get("question") or ""), "needs the owner")
