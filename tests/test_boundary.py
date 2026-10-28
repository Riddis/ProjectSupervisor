"""Gate G1 (supervisor.boundary): every rule with prohibited and legitimate examples, the explicit exception lists,
and the framework itself passing. Each case builds a throwaway framework root (a supervisor/ folder + boundary.toml)
so the rules are tested apart from the repository's own baseline."""
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from supervisor import boundary

REPO = Path(__file__).resolve().parents[1]

BASE_CONFIG = """
[imports]
allowed = ["yaml", "pydantic"]
dynamic_loaders = ["supervisor/extensions.py"]
[literals]
allowed_url_hosts = ["github.com"]
user_names = ["jdoe"]
[vendor]
terms = ["claude", "codex", "gpt", "openai"]
files = ["supervisor/adapters/claude_cli.py"]
"""


def framework(tmp_path, files: dict[str, str], config: str = BASE_CONFIG) -> Path:
    root = tmp_path / "fw"
    (root / "supervisor").mkdir(parents=True)
    (root / "boundary.toml").write_text(config, encoding="utf-8")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def rules(root, **kw):
    report = boundary.check(root, user_names=kw.pop("user_names", None) or ["jdoe"])
    return [(f.rule, f.path, f.text) for f in report.violations], report


# ---------------------------------------------------------------- G1a

@pytest.mark.parametrize("code,module", [
    ("import extensions.render\n", "extensions.render"),                  # a project's extension
    ("from engine.qa import score\n", "engine.qa"),                       # a project's own package
    ("import supervisor_ext_textgen\n", "supervisor_ext_textgen"),        # an extension package
    ("import importlib\nimportlib.import_module('extensions.bench')\n", "extensions.bench"),
])
def test_g1a_refuses_imports_of_project_or_extension_code(tmp_path, code, module):
    found, _ = rules(framework(tmp_path, {"supervisor/mod.py": code}))
    assert ("G1a", "supervisor/mod.py", module) in found


@pytest.mark.parametrize("code", [
    "from importlib import import_module as load\nload('extensions.bench')\n",
    "from importlib import import_module as load\nname = 'x'\nload(name)\n",
    "import importlib\nload = importlib.import_module\nagain = load\nagain('extensions.bench')\n",
    "import importlib\ngetattr(importlib, 'import_module')('extensions.bench')\n",
    "import functools, importlib\nf = functools.partial(importlib.import_module, 'extensions.bench')\n",
    "import importlib\nmods = list(map(importlib.import_module, ['extensions.bench']))\n",
    "from importlib.util import spec_from_file_location as locate\nlocate('m', 'p.py')\n",
    "import runpy\nrunpy.run_path('plugin.py')\n",
    "from builtins import __import__ as imp\nimp('extensions.bench')\n",
])
def test_g1a_sees_dynamic_imports_however_the_loader_is_named(tmp_path, code):
    """Review of 1.9.0 (P2): an alias of import_module (`as load`, an assignment, getattr, partial) escaped G1a."""
    found, _ = rules(framework(tmp_path, {"supervisor/mod.py": code}))
    assert any(r == "G1a" and p == "supervisor/mod.py" for r, p, _ in found), found


@pytest.mark.timeout(20)
@pytest.mark.parametrize("code,expected", [
    # rebinding to two different loaders made the old fixpoint flip between them forever
    ("import importlib, runpy\nload = importlib.import_module\nload = runpy.run_path\nload('plugin.py')\n",
     "dynamic import_module/run_path() outside the listed loaders"),
    ("import importlib, runpy\nload = runpy.run_path\nload = importlib.import_module\nload('extensions.bench')\n",
     "dynamic import_module/run_path() outside the listed loaders"),
    ("import importlib\na = importlib.import_module\nb = a\na = b\nb('extensions.bench')\n", "dynamic import"),
])
def test_g1a_alias_analysis_ends_on_rebinding_and_cycles(tmp_path, code, expected):
    """Review of 1.9.0, second round (P2): a name rebound to different loaders never let the alias fixpoint settle.
    A name that may be any of several loaders is judged as all of them: a path loader among them means a literal
    argument proves nothing, so the call is allowed only in a listed loader file."""
    report = boundary.check(framework(tmp_path, {"supervisor/mod.py": code}), user_names=[])
    details = [f.detail for f in report.violations if f.rule == "G1a"]
    assert any(expected in d for d in details), details


def test_g1a_an_aliased_loader_with_a_framework_or_stdlib_name_passes(tmp_path):
    code = ("from importlib import import_module as load\nload('json')\nload('supervisor.adapters')\n"
            "socket = __import__('socket')\n")
    found, _ = rules(framework(tmp_path, {"supervisor/mod.py": code}))
    assert found == []


def test_g1a_refuses_a_computed_dynamic_import_outside_the_loader(tmp_path):
    code = "import importlib\nname = 'x'\nimportlib.import_module(name)\n"
    found, _ = rules(framework(tmp_path, {"supervisor/mod.py": code, "supervisor/extensions.py": code}))
    assert [f for f in found if f[0] == "G1a"] == [("G1a", "supervisor/mod.py", "")]   # the loader may


def test_g1a_allows_stdlib_the_framework_and_declared_dependencies(tmp_path):
    code = ("from __future__ import annotations\nimport os, json\nimport yaml\nfrom pydantic import BaseModel\n"
            "from supervisor import tasks\nfrom .store import now_iso\nimport importlib\n"
            "importlib.import_module('supervisor.adapters')\n")
    tmpl = "from extensions import mine\n"                                # the template is project code
    found, _ = rules(framework(tmp_path, {"supervisor/mod.py": code, "supervisor/template/extensions/x.py": tmpl}))
    assert found == []


def test_g1a_allowed_list_covers_every_declared_dependency():
    """boundary.toml's import list is the import-name form of pyproject's dependencies and ui extra, no more."""
    py = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    import re
    dists = {re.split(r"[<>=!~\[ ]", d)[0].lower() for d in py["dependencies"] + py["optional-dependencies"]["ui"]}
    import_name = {"pydantic": "pydantic", "typer": "typer", "ruamel.yaml": "ruamel", "pyyaml": "yaml",
                   "psutil": "psutil", "portalocker": "portalocker", "python-ulid": "ulid", "pillow": "PIL",
                   "requests": "requests", "jsonschema": "jsonschema", "packaging": "packaging",
                   "fastapi": "fastapi", "uvicorn": "uvicorn",
                   "jinja2": "jinja2", "python-multipart": "multipart", "sse-starlette": "sse_starlette"}
    assert dists == set(import_name), "a dependency changed: update this map and boundary.toml [imports] allowed"
    allowed = set(boundary.load_config(REPO)["imports"]["allowed"])
    assert set(import_name.values()) <= allowed and allowed - set(import_name.values()) == {"starlette"}  # fastapi's


# ---------------------------------------------------------------- G1b

@pytest.mark.parametrize("line,text", [
    ('ROOT = "D:\\\\Repos\\\\Project"\n', "D:\\\\Repos\\\\Project"),
    ("path = 'C:/work/thing'\n", "C:/work/thing"),
    ("# see /home/alice/projects\n", "/home/alice"),
    ("cache = r'C:\\Users\\bob\\AppData'\n", "C:\\Users\\bob\\AppData"),
    ('URL = "https://render.mycompany.io/api"\n', "render.mycompany.io"),
    ('RENDER = "http://render-box.local:8188/"\n', "render-box.local"),        # mDNS: a real machine (review P2)
    ('HOST = "10.0.4.17"\n', "10.0.4.17"),
    ("# ask jane.doe@acme-corp.com\n", "jane.doe@acme-corp.com"),
    ("# written by jdoe\n", "jdoe"),
])
def test_g1b_refuses_project_specific_literals(tmp_path, line, text):
    found, _ = rules(framework(tmp_path, {"supervisor/mod.py": line}))
    assert any(r == "G1b" and t == text for r, _, t in found), found


@pytest.mark.parametrize("line", [
    'HOST = "127.0.0.1"\n', 'BIND = "0.0.0.0"\n', 'url = f"http://{host}:{port}/"\n', 'u = "http://localhost:8790/"\n',
    'u = "http://[::1]:80/"\n', 'u = "https://example.com/x"\n', 'u = "https://shop.example.test/a"\n',
    'u = "https://github.com/owner/repo"\n', "# remote: git@github.com:you/project.git\n",
    "# mail noreply@example.org\n", 'p = "state/tasks/T-01.yaml"\n', 'v = "1.8.4"\n',
    "drive = d[1] == ':'\n", 'ratio = "16:9"\n',
])
def test_g1b_allows_loopback_reserved_names_and_relative_paths(tmp_path, line):
    found, _ = rules(framework(tmp_path, {"supervisor/mod.py": line}))
    assert [f for f in found if f[0] == "G1b"] == []


def test_g1b_exceptions_are_explicit_per_file(tmp_path):
    cfg = BASE_CONFIG + '\n[[literals.allowed]]\nfile = "supervisor/a.py"\ntext = \'D:\\demo\'\n'
    root = framework(tmp_path, {"supervisor/a.py": "x = r'D:\\demo'\n", "supervisor/b.py": "x = r'D:\\demo'\n"},
                     config=cfg)
    found, _ = rules(root)
    assert [(r, p) for r, p, _ in found if r == "G1b"] == [("G1b", "supervisor/b.py")]


# ---------------------------------------------------------------- G1c

def test_g1c_refuses_vendor_names_outside_the_listed_files(tmp_path):
    root = framework(tmp_path, {"supervisor/adapters/claude_cli.py": "LANE = 'claude'\n",
                                "supervisor/gate.py": "if adapter == 'codex_cli':\n    pass\n",
                                "supervisor/ui/x.html": "<p>Powered by GPT-5</p>\n"})
    found, _ = rules(root)
    assert sorted((p, t) for r, p, t in found if r == "G1c") == [("supervisor/gate.py", "codex"),
                                                                ("supervisor/ui/x.html", "GPT")]


def test_g1c_does_not_match_inside_other_words_and_reports_stale_entries(tmp_path):
    root = framework(tmp_path, {"supervisor/mod.py": "precodexed = 'gptx'  # sclaudes\n",
                                "supervisor/adapters/claude_cli.py": "nothing here\n"})
    found, report = rules(root)
    assert [f for f in found if f[0] == "G1c"] == []
    assert any("supervisor/adapters/claude_cli.py" in n and "remove it" in n for n in report.notes)


# ---------------------------------------------------------------- evidence, CLI, the framework itself

def test_module_entry_point_exit_codes(tmp_path):
    clean = framework(tmp_path, {"supervisor/mod.py": "x = 1\n"})
    run = lambda root: subprocess.run([sys.executable, "-m", "supervisor.boundary", "--root", str(root), "--json"],
                                      cwd=REPO, capture_output=True, text=True, env={"CI": "1", "PATH": ""}, check=False)
    ok = run(clean)
    assert ok.returncode == 0 and json.loads(ok.stdout)["ok"] is True
    (clean / "supervisor" / "bad.py").write_text("import extensions\n", encoding="utf-8")
    bad = run(clean)
    assert bad.returncode == 1 and json.loads(bad.stdout)["violations"][0]["rule"] == "G1a"
    (clean / "boundary.toml").unlink()
    assert run(clean).returncode == 2


def test_a_root_without_the_package_cannot_pass(tmp_path):
    """Review of 1.9.0 (P2): `--root` at a folder with boundary.toml but no supervisor/ scanned nothing and said OK."""
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    (empty / "boundary.toml").write_text(BASE_CONFIG, encoding="utf-8")
    with pytest.raises(boundary.ConfigError, match="nothing to check"):
        boundary.check(empty, user_names=[])
    (empty / "supervisor").mkdir()
    (empty / "supervisor" / "README.md").write_text("no code here\n", encoding="utf-8")
    assert boundary.run(empty, echo=lambda *_: None) == 2


def test_the_framework_package_passes_its_own_boundary_check():
    report = boundary.check(REPO, user_names=[])
    assert report.ok, "\n".join(str(f) for f in report.violations)
    assert report.notes == [], report.notes
