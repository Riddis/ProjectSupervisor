"""R-28 (last bullet): the framework repository cited another project's own records as evidence inside comments
and docstrings -- a decision id (`D-0NN`), an inbox or issue id (`H-0NN`/`OT-0NN`/`I-0NN`), a task or attempt id
(`T<phase>-<nn>`, phase 1 and up), or a plan or review document's bare filename (`RECOVERY_PLAN.md`,
`docs/plan/LONG_WORK.md`, `ATTEMPT_MODEL.md`, `CONTRIBUTOR_RULING.md`, `RELEASE_REVIEW_*.md`,
`REVIEW_PROMPT_*.md`, `FRAMEWORK_REVIEW_*.md`, `RELEASE_RECORD*.md`, `docs/plan/PHASE<n>.md`, and similar).
None of those records live in this repository (D-005): the framework repository never holds another project's
files or information, including a pointer into them.

The fifth shape the finding names is the project itself: its name, and the nicknames it gives the people and
agents who work in it. A literal name cannot be written down here -- the word list belongs in a local,
out-of-repository file (`SUPERVISOR_PROJECT_TERMS`, gate G1d), never in this repository, on the same rule this
test enforces -- but the *position* a name is used in can be. So the fifth shape is scanned structurally: a
named identity credited with a record or an act of judgement (`<Name>'s probe`, `<Name> reviewed ...`). In this
framework an agent is named by its role (`code_review`, "the reviewer", "the owner"); a nickname in that
position is one project's own vocabulary, and the note attributed to it is a pointer into that project's review
records. G1d catches a name only when the local word list happens to carry it, which is the owner's choice and
not this repository's to rely on; this test holds the structure instead.

The first four shapes are the ones a word list cannot hold at all: a decision id, an inbox/issue id, a task id
and a plan/review filename are generic patterns every project using this framework's planning convention
produces, not one project's distinctive vocabulary, so G1d catches them only for the few ids a particular local
list happens to name.

Scope: every tracked file in the repository, excluding `supervisor/template/agents/schemas/` -- the placeholder
and sample data a *new* project fills in with records of its own (a `task_ref` pattern example, sample ids in
`samples/*.json`), never a citation of this repository's own development history -- and excluding this test's own
file, whose docstring and allow-lists necessarily name the shapes and examples they are about. The rest of
`supervisor/template/` *is* scanned: starter content is copied into every new project, so a citation left there
propagates out of this repository instead of staying in it.

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
  `H-001`, `H-002`, `I-001`, `I-002`;
- for the fifth shape, the words this repository legitimately uses where a name would otherwise stand: the role
  and generic nouns that own a record (`the reviewer's probe`, `the owner's note`), the capitalised nouns it
  names things by (a screen, a language, a library, a tool, a role, a concept), and the function words that
  precede an attribution verb (`already reviewed`, `is approved`). Each list is below. Adding an ordinary noun
  to one of them is a one-line edit; adding a name is not, and is the thing this test refuses.
A real historical citation is never allow-listed; it is fixed by restating the rule or behaviour inline.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import supervisor

REPO_ROOT = Path(supervisor.__file__).resolve().parent.parent
TEMPLATE_SAMPLES = REPO_ROOT / "supervisor" / "template" / "agents" / "schemas"
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

# The fifth shape: a named identity credited with a record or an act of judgement. Three positions a name is
# used in, each read as "whoever owns this must be a role or a thing this repository names, never a nickname".
# The attribution verbs are the review and approval ones only: "raised", "probed" and "flagged" are this
# codebase's ordinary words for an exception or a diagnosis, and say nothing about who judged anything.
RECORD_OWNER = re.compile(r"\b([\w-]+)'s\s+(?:probe|probes|review|reviews|round|rounds|finding|findings|"
                          r"question|questions|note|notes|ruling|rulings|report|reports)\b")
PROPER_POSSESSIVE = re.compile(r"\b([A-Z][a-z]{2,})'s\b")
ATTRIBUTION = re.compile(r"\b([\w-]+)\s+(?:reviewed|reviewing|approved|approves)\b")

# The only ids this test treats as the framework's own neutral examples, never another project's history.
ALLOWED_TASK_IDS = {"T9-90", "T9-99", "T1-01", "T1-02", "T1-03", "T2-02"}
ALLOWED_INBOX_ISSUE_IDS = {"H-001", "H-002", "I-001", "I-002"}
# D-001 is the documented generic naming convention for a *new* project's own first decision record
# (`D-001-project-setup.md`, named that way in the setup skill and the interview doc for any project adopting
# the framework) -- never a citation of this repository's own D-001.
ALLOWED_DECISION_IDS = {"D-001"}

# Who may own a record here: a role, or the generic noun of the thing that produced it. A review finding this
# repository's own history recorded is written as a bare number (`F5`, `G7`, `R-16`), an ordinal (`1`, `4`) or a
# one-letter placeholder for "some reviewer" (`A`, `B`) -- this repository's own CHANGELOG holds those rounds,
# and a single letter names nobody, so neither is a pointer out of this repository.
ALLOWED_RECORD_OWNERS = {"check", "host", "one", "owner", "pass", "plan", "project", "producer", "release",
                         "review", "reviewer", "role", "round", "rule", "ruamel", "task", "worker"}
FINDING_NUMBER = re.compile(r"^(?:[A-Z]|[A-Z]-?\d+|\d+)$")
# Capitalised nouns this repository names things by: a screen, a language, a library, a vendor or model, a role,
# a concept. Never a person and never an agent's nickname.
ALLOWED_PROPER_POSSESSIVES = {"Architect", "Cancellation", "Center", "Claude", "Code", "Codex", "Console",
                             "Discovery", "Fable", "Hold", "Object", "Ollama", "Overview", "Owner", "Parity",
                             "Python", "Roadmap", "Starlette", "Supervisor", "Today"}
# What may stand in front of an attribution verb: a function word, or a role or generic noun (compared lowercase).
ALLOWED_ATTRIBUTION_SUBJECTS = {"a", "actually", "already", "an", "and", "are", "as", "be", "been", "def",
                                "digest", "each", "ever", "exact", "file", "from", "had", "has", "have",
                                "independently", "is", "it", "item", "maintainers", "never", "no", "once", "or",
                                "owner", "reviewer", "role", "skill", "that", "the", "this", "was", "were",
                                "who", "you"}


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
        if TEMPLATE_SAMPLES in p.parents:
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
            for m in RECORD_OWNER.finditer(line):
                owner = m.group(1)
                if owner.lower() not in ALLOWED_RECORD_OWNERS and not FINDING_NUMBER.match(owner):
                    hits.append(f"{rel}:{no}: a record owned by {owner!r}: {line.strip()!r}")
            for m in PROPER_POSSESSIVE.finditer(line):
                if m.group(1) not in ALLOWED_PROPER_POSSESSIVES:
                    hits.append(f"{rel}:{no}: a name in the possessive, {m.group(1)!r}: {line.strip()!r}")
            for m in ATTRIBUTION.finditer(line):
                if m.group(1).lower() not in ALLOWED_ATTRIBUTION_SUBJECTS:
                    hits.append(f"{rel}:{no}: {m.group(1)!r} credited with a judgement: {line.strip()!r}")
    return hits


def test_the_framework_repository_cites_no_other_project_history():
    """D-005: the framework repository never holds another project's files or information. A task id, a
    decision id, an inbox or issue id, a plan/review document's filename, or a named identity credited with a
    record, found anywhere in this repository (excluding the sample data under
    `supervisor/template/agents/schemas/` and this test's own file), is a pointer into a record that lives only
    in that other project's own state -- never something this repository may cite as its own evidence. Every one
    of these was once a citation like "(RECOVERY_PLAN.md section 3.3, D-027 decision 9)" or "<Name>'s probe"
    beside a plain-English statement of the same rule; the fix removes the citation and keeps the rule. A name
    this test cannot name is still held here by the position it is used in: gate G1d's word list is the owner's
    local file, and catches a name only if that list happens to carry it (see the module docstring)."""
    hits = _scan()
    assert not hits, "project-history references found in the repository:\n" + "\n".join(hits)
