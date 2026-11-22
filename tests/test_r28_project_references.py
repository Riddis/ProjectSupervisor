"""R-28 (last bullet): the framework repository cited another project's own records as evidence inside comments
and docstrings -- a decision id (`D-0NN`), an inbox or issue id (`H-0NN`/`OT-0NN`/`I-0NN`), a task or attempt id
(`T<phase>-<nn>`, phase 1 and up), or a plan or review document's bare filename (`RECOVERY_PLAN.md`,
`docs/plan/LONG_WORK.md`, `ATTEMPT_MODEL.md`, `CONTRIBUTOR_RULING.md`, `RELEASE_REVIEW_*.md`,
`REVIEW_PROMPT_*.md`, `FRAMEWORK_REVIEW_*.md`, `RELEASE_RECORD*.md`, `docs/plan/PHASE<n>.md`, and similar).
None of those records live in this repository (D-005): the framework repository never holds another project's
files or information, including a pointer into them.

The fifth shape the finding names, the deployed project's own name, is deliberately not scanned here: it is a
literal word, not a structural shape, and gate G1d already scans the whole repository for exactly that against
a local, out-of-repository word list (`SUPERVISOR_PROJECT_TERMS`) -- a list this test must never hold, on the
same rule it enforces. This test covers the four shapes G1d's word list cannot: a decision id, an inbox/issue
id, a task id and a plan/review filename are generic patterns, not one project's configured vocabulary, so no
word list would ever catch them.

Scope: every tracked file in the repository, excluding `supervisor/template/` and its own schema samples --
starter content copied into a *new* project (its own placeholders, a generic path convention like
`docs/plan/PROJECT_PLAN.md` or `D-001-project-setup.md` that any future project creates for itself, never a
citation of this repository's own development history) -- and excluding this test's own file, whose docstring
and allow-list necessarily name the shapes and examples it is about.

Allow-list, stated here and nowhere else:
- the documented neutral task-id convention `T0-<nn>` (docs/TASK_CONTRACT.md, "T0-01 is the convention"; also
  used as sample/fixture data throughout this suite);
- a handful of task ids this framework's own tests construct and consume entirely within themselves, never
  citing a record outside the repository: `T9-90`, `T9-99` (large round numbers used only as "a big id"
  placeholders), `T1-01`, `T1-02`, `T1-03` (arbitrary ids a test makes up for "some task" in its own fixture
  data), `T2-02` (an arbitrary `resolving_task` value one test passes to its own fixture and reads straight
  back);
- a handful of inbox/issue ids the framework's own tests generate and consume entirely within themselves --
  the first and second id a fresh test layout's own code assigns, never a citation of an external record:
  `H-001`, `H-002`, `I-001`, `I-002`.
A real historical citation is never allow-listed; it is fixed by restating the rule or behaviour inline.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import supervisor

REPO_ROOT = Path(supervisor.__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "supervisor" / "template"
THIS_FILE = Path(__file__).resolve()

TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".html", ".js", ".css", ".txt", ".json"}

TASK_ID = re.compile(r"\bT[1-9]-\d\d(\.[a-z]\d+)*\b")
DECISION_ID = re.compile(r"\bD-0\d\d\b")
INBOX_ISSUE_ID = re.compile(r"\bH-0\d\d\b|\bOT-0\d\d\b|\bI-0\d\d\b")
DOC_NAME = re.compile(
    r"\bRECOVERY_PLAN\.md\b|docs/plan/LONG_WORK\.md|\bATTEMPT_MODEL\.md\b|\bCONTRIBUTOR_RULING\.md\b|"
    r"\bRELEASE_REVIEW_[\w.\-]*\.md\b|\bREVIEW_PROMPT_[\w.\-]*\.md\b|\bFRAMEWORK_REVIEW_[\w.\-]*\.md\b|"
    r"\bRELEASE_RECORD[\w.\-]*\.md\b|docs/plan/PHASE\d+\.md|\bCONTRIBUTION_DRYRUN\.md\b|\bTESTING_GAPS\.md\b|"
    r"\bFRAMEWORK_SURFACE\.md\b|\bBOUNDARY_RULING\.md\b|\bVERIFY_RULING\.md\b|\bCANDIDATE_HEAD\.md\b"
)

# The only ids this test treats as the framework's own neutral examples, never another project's history.
ALLOWED_TASK_IDS = {"T9-90", "T9-99", "T1-01", "T1-02", "T1-03", "T2-02"}
ALLOWED_INBOX_ISSUE_IDS = {"H-001", "H-002", "I-001", "I-002"}
# D-001 is the documented generic naming convention for a *new* project's own first decision record
# (`D-001-project-setup.md`, named that way in the setup skill and the interview doc for any project adopting
# the framework) -- never a citation of this repository's own D-001.
ALLOWED_DECISION_IDS = {"D-001"}


def _is_allowed_task_id(match_text: str) -> bool:
    base = match_text.split(".")[0]
    return base.startswith("T0-") or match_text in ALLOWED_TASK_IDS or base in ALLOWED_TASK_IDS


def _repo_files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files"], capture_output=True, text=True, check=True)
    files = []
    for rel in out.stdout.splitlines():
        if not rel:
            continue
        p = REPO_ROOT / rel
        if TEMPLATE_DIR in p.parents:
            continue
        if p.resolve() == THIS_FILE:
            continue
        if "__pycache__" in p.parts:
            continue
        if p.suffix.lower() in TEXT_SUFFIXES and p.is_file():
            files.append(p)
    return files


def _scan() -> list[str]:
    hits = []
    for p in _repo_files():
        rel = p.relative_to(REPO_ROOT).as_posix()
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            for m in TASK_ID.finditer(line):
                if not _is_allowed_task_id(m.group(0)):
                    hits.append(f"{rel}:{no}: task id {m.group(0)!r}: {line.strip()!r}")
            for m in DECISION_ID.finditer(line):
                if m.group(0) not in ALLOWED_DECISION_IDS:
                    hits.append(f"{rel}:{no}: decision id {m.group(0)!r}: {line.strip()!r}")
            for m in INBOX_ISSUE_ID.finditer(line):
                if m.group(0) not in ALLOWED_INBOX_ISSUE_IDS:
                    hits.append(f"{rel}:{no}: inbox/issue id {m.group(0)!r}: {line.strip()!r}")
            for m in DOC_NAME.finditer(line):
                hits.append(f"{rel}:{no}: review/plan filename {m.group(0)!r}: {line.strip()!r}")
    return hits


def test_the_framework_repository_cites_no_other_project_history():
    """D-005: the framework repository never holds another project's files or information. A task id, a
    decision id, an inbox or issue id, or a plan/review document's filename, found anywhere in this repository
    (excluding the starter `supervisor/template/` and this test's own file), is a pointer into a record that
    lives only in that other project's own state -- never something this repository may cite as its own
    evidence. Every one of these was once a citation like "(RECOVERY_PLAN.md section 3.3, D-027 decision 9)"
    beside a plain-English statement of the same rule; the fix removes the citation and keeps the rule. The
    project's own name is gate G1d's job, not this test's (see the module docstring)."""
    hits = _scan()
    assert not hits, "project-history references found in the repository:\n" + "\n".join(hits)
