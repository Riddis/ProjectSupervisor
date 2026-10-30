"""Write supervisor/skills_history.json: the hash of every version of every template skill file the framework's
release tags shipped, so `supervisor skills status` knows an older shipped copy nobody edited from a copy a project
changed (supervisor/skills.py).

    python tools/skills_history.py            # from the framework root; rewrites the file from every vX.Y.Z tag

Run it at each release, after tagging the previous one, and commit the result with the release."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "supervisor/template/_dot_claude/skills/"
OUT = ROOT / "supervisor" / "skills_history.json"
RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")    # framework releases only, never a package's <identity>-vX.Y.Z


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout


def main() -> int:
    history: dict[str, set[str]] = {}
    for tag in (t for t in git("tag", "-l", "v*", "--sort=v:refname").split() if RELEASE_TAG.match(t)):
        for path in git("ls-tree", "-r", "--name-only", tag, "--", PREFIX).split("\n"):
            if not path.startswith(PREFIX):
                continue
            text = git("show", f"{tag}:{path}")
            digest = hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()
            history.setdefault(path[len(PREFIX):], set()).add(digest)
    doc = {"about": "sha256 of each template skill file as release tags shipped it (line endings ignored); "
                    "written by tools/skills_history.py",
           "files": {rel: sorted(h) for rel, h in sorted(history.items())}}
    OUT.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"{OUT.relative_to(ROOT)}: {sum(len(h) for h in history.values())} version(s) of {len(history)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
