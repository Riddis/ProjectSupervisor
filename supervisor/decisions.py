"""Decision records (state/decisions/*.md): short-form `D-0NN-<slug>.md` files with a YAML front-matter header,
rendered to DECISIONS.md by `render.render_decisions` (never parsed back there). The only writer added here is the
Control Center's "convert a chat reply into a decision" action; every decision written by an
agent is still a plain file edit under its own task's `owned_paths`, as before."""
from __future__ import annotations

import re
from pathlib import Path

from . import store
from .paths import Layout
from .store import now_iso


def _next_id(layout: Layout) -> str:
    n = 0
    for p in layout.decisions.glob("D-*.md"):
        m = re.match(r"D-(\d+)", p.stem)
        if m:
            n = max(n, int(m.group(1)))
    return f"D-{n + 1:03d}"


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60] or "decision"


def path_for(layout: Layout, decision_id: str, slug: str) -> Path:
    return layout.decisions / f"{decision_id}-{slug}.md"


def create(layout: Layout, title: str, body: str, status: str = "proposed", decided_by: str = "owner") -> dict:
    layout.decisions.mkdir(parents=True, exist_ok=True)
    decision_id = _next_id(layout)
    slug = _slug(title)
    date = now_iso()[:10]
    front_matter = (f"---\nid: {decision_id}\ndate: {date}\ntitle: {title}\nstatus: {status}\n"
                    f"decided_by: {decided_by}\n---\n")
    content = f"{front_matter}\n## Decision\n\n{body.strip()}\n"
    store.write_text(path_for(layout, decision_id, slug), content)
    from . import tasks as tasks_mod
    tasks_mod.log(layout, "decision_created", decided_by, id=decision_id, title=title)
    return {"id": decision_id, "title": title, "status": status}
