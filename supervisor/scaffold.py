"""`supervisor extension new <name> --package`: the standard extension package (docs/EXTENSION_STANDARD.md), stamped
from `supervisor/extension_template/`.

In the template, `_package_` stands for the Python package's folder and `{{ key }}` for a value, in file names and
in text. Python files are stored as `*.py-tpl`: with placeholders in them they are not valid Python, and nothing in
the framework may try to import or lint them. The result passes `supervisor extension check` and its own tests as
generated."""
from __future__ import annotations

import re
from pathlib import Path

from . import __version__

TEMPLATE = Path(__file__).resolve().parent / "extension_template"
IDENTITY = re.compile(r"^[a-z][a-z0-9_]*$")


def framework_range(version: str = __version__) -> str:
    """The range a new extension declares: this minor release up to the next major (`>=1.12,<2`)."""
    major, minor = (int(x) for x in version.split(".")[:2])
    return f">={major}.{minor},<{major + 1}"


def package(directory: str | Path, identity: str) -> Path:
    """Create `<directory>/supervisor-ext-<identity>/`; returns it. ValueError for an identity the manifest would
    refuse, FileExistsError when the folder exists (nothing is overwritten)."""
    if not IDENTITY.match(identity):
        raise ValueError(f"invalid identity {identity!r}: lower case letters, digits and _, starting with a letter")
    values = {"identity": identity, "package": f"supervisor_ext_{identity}",
              "distribution": f"supervisor-ext-{identity.replace('_', '-')}",
              "framework_range": framework_range(), "framework_version": __version__}
    root = Path(directory) / values["distribution"]
    if root.exists():
        raise FileExistsError(f"{root} exists already; nothing was written")

    def fill(text: str) -> str:
        for key, value in values.items():
            text = text.replace("{{ " + key + " }}", value)
        return text

    for src in sorted(p for p in TEMPLATE.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        parts = [values["package"] if part == "_package_" else fill(part) for part in src.relative_to(TEMPLATE).parts]
        if parts[-1].endswith(".py-tpl"):
            parts[-1] = parts[-1][: -len("-tpl")]
        dest = root.joinpath(*parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(fill(src.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
    return root


def local_manifest(identity: str) -> str:
    """The manifest `supervisor extension new <name>` writes beside a new project-local module."""
    return (f"# The manifest of extensions/{identity}.py (docs/EXTENSION_STANDARD.md in the framework repo). List in\n"
            f"# [provides] what setup() registers; `supervisor extension check {identity}` proves the two agree.\n"
            f'identity = "{identity}"\nversion = "0.1.0"\nframework = "{framework_range()}"\n')
