"""The reference extension: every registry hook, used through `supervisor.ext` only (docs/EXTENSION_API.md).

It keeps "notes": inbox items of its own kind `reference_note`, which the owner annotates on its Control Center page
and closes there. Around that it registers one of everything: a subprocess lane, a python job, a doctor check, an
Overview panel, a packet screen, a projection, a CLI group, two owner commands, an inbox kind, a page with its
templates and static files, and its own config section. tests/test_hook_contracts.py loads it through the real
loader, as a project would, and checks each hook's contract.

It follows the extension standard (docs/EXTENSION_STANDARD.md): supervisor-extension.toml declares everything
setup() registers, and `supervisor extension check reference` (gate G5) proves it. Install it in a project either as
a package (`pip install` this folder's parent, examples/reference_extension/, which has a pyproject.toml), or by
copying this folder to `extensions/reference/`. Then list `reference` under `extensions:` in project.yaml. Its
settings live in config/routing.yaml, validated by config.Settings:

    extensions:
      reference:
        paused: false            # true: its lane defers every job (no attempt spent)
        forbidden_word: SECRET   # its packet screen refuses a brief containing this word
        agent_lane: null         # an agent lane whose executable the doctor check reports (nothing is launched)

Importing this package registers nothing: setup() does all of it."""
from __future__ import annotations

from pathlib import Path

from supervisor import ext

from . import lane, pages

KIND = "reference_note"
HERE = Path(__file__).resolve().parent


def settings(layout: ext.Layout) -> dict:
    return ext.config(layout, "reference")


def notes(layout: ext.Layout) -> list[dict]:
    return ext.owned_items(layout, KIND)


# ------------------------------------------------------------------ hooks

def count_job(root, run_dir, out: str = "reference_count.json", **params) -> dict:
    """python_job `reference_count`: write how many notes are open."""
    layout = ext.Layout(root)
    open_notes = [n for n in notes(layout) if n.get("status") == "open"]
    ext.atomic_json(layout.root / out, {"open": len(open_notes), "at": ext.now_iso()})
    return {"status": "COMPLETE", "artifacts": [out], "notes": f"{len(open_notes)} open notes"}


def check(layout: ext.Layout) -> ext.Check:
    """doctor_check: the policy config is in effect, and the configured agent lane resolves."""
    problems = ext.policy.config_problems(layout)
    if problems:
        return ext.Check("reference", False, "config/policy.yaml entries rejected: " + "; ".join(problems))
    detail = f"{len(notes(layout))} notes; GPU floor {ext.resources(layout)['gpu'].get('usable_vram_floor_mb', 'unset')}"
    lane_name = settings(layout).get("agent_lane")
    if lane_name:
        try:
            handle = ext.agent_lane(layout, lane_name)
        except ext.ExtensionError as exc:
            return ext.Check("reference", False, str(exc))
        detail += f"; agent lane {handle.name}: {Path(handle.executable).name}"
    return ext.Check("reference", True, detail)


def panel(layout: ext.Layout) -> dict:
    """overview_panel: open and answered notes."""
    items = notes(layout)
    open_count = sum(1 for n in items if n.get("status") == "open")
    return {"title": "Reference notes", "rows": [("open", str(open_count)), ("closed", str(len(items) - open_count))],
            "progress": (len(items) - open_count, max(len(items), 1))}


def screen(layout: ext.Layout, text: str, where: str, *, job: dict | None = None) -> None:
    """packet_screen: refuse a brief that carries the configured forbidden word."""
    word = settings(layout).get("forbidden_word")
    if word and word in text:
        raise ext.policy.PolicyViolation(f"{where} refused by the reference extension: it contains {word!r}")


def projection(layout: ext.Layout) -> Path:
    """projection: REFERENCE.md, one line per note."""
    lines = ["# Reference notes", ""] + [f"- {n['id']} ({n.get('status')}): {len(n.get('notes') or [])} annotations"
                                         for n in notes(layout)]
    path = layout.root / "REFERENCE.md"
    ext.atomic_write_text(path, "\n".join(lines) + "\n", newline="\n")
    return path


def annotate(layout: ext.Layout, payload: dict, actor: str) -> dict:
    """command `reference.note`: add an annotation to a note; idempotent by the command id."""
    cid = payload["_id"]

    def fn(doc: dict) -> dict:
        entries = doc.setdefault("notes", [])
        if not any(e.get("command_id") == cid for e in entries):
            entries.append({"text": str(payload["text"]), "by": actor, "at": ext.now_iso(), "command_id": cid})
        return doc

    doc = ext.update_owned_item(layout, payload["item_id"], fn)
    ext.log_event(layout, "reference_note", actor, item=payload["item_id"], command=cid)
    return {"annotations": len(doc.get("notes") or [])}


def close(layout: ext.Layout, payload: dict, actor: str) -> dict:
    """command `reference.close`: close a note as its owner; idempotent by the command id."""
    item = ext.close_owned_item(layout, payload["item_id"], "DONE", note=str(payload.get("note", "")), by=actor,
                                command_id=payload["_id"])
    return {"status": item.get("status")}


def make_cli():
    """cli `supervisor reference ...`."""
    import typer

    app = typer.Typer(help="the reference extension")

    @app.command("count")
    def count() -> None:
        """How many notes are open."""
        layout = ext.Layout()
        typer.echo(f"{sum(1 for n in notes(layout) if n.get('status') == 'open')} open notes")

    return app


def setup(registry: ext.Registry) -> None:
    registry.config_sections("reference")
    registry.adapter(lane.ADAPTER)
    registry.python_job("reference_count", count_job)
    registry.doctor_check(check)
    registry.overview_panel(panel)
    registry.packet_screen(screen)
    registry.projection(projection, path="REFERENCE.md", name="reference_notes")
    registry.cli(make_cli(), "reference")
    registry.command("reference.note", annotate)
    registry.command("reference.close", close)
    registry.inbox_kind(KIND, "Reference notes", page="/reference/{id}", answerable=False)
    registry.template_dir(HERE / "templates")
    registry.static_dir(HERE / "static", "reference")
    registry.ui_router(pages.router, "Reference", "/reference")
