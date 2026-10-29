"""plugin.ui, the UI kit (phase 2 step 5): a plugin page renders inside the framework's own chrome, depends on the
framework's auth, checks the same CSRF token as a framework form and submits its action as an owner command, with
the same flash banner; thumbnails come from the framework's cache and never from outside the project. supervisor.plugin
itself imports without the UI's dependencies; plugin.ui loads them when used."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from supervisor import plugin, plugins, testing

REPO = Path(__file__).resolve().parents[1]
CSRF = testing.CSRF

PAGE = '''
from pathlib import Path
from fastapi import APIRouter, Depends, Form, Request
from supervisor.plugin import ui

router = APIRouter()
HERE = Path(__file__).resolve().parent

@router.get("/board")
def board(request: Request, layout=Depends(ui.require_auth)):
    return ui.render(request, "board.html", {"count": 2})

@router.post("/board/bump")
def bump(request: Request, csrf_token: str = Form(""), layout=Depends(ui.require_auth)):
    ui.require_csrf(request, csrf_token)
    return ui.act(request, layout, "board.bump", {"n": 1}, "/board")

def _bump(layout, payload, actor):
    (layout.root / "bumped.txt").write_text(actor, encoding="utf-8")
    return {"n": payload["n"]}

def setup(registry):
    registry.template_dir(HERE / "templates")
    registry.command("board.bump", _bump)
    registry.ui_router(router, "Board", "/board")
'''
BOARD_HTML = '''{% extends "base.html" %}
{% block title %}Board{% endblock %}
{% block content %}<section class="panel" id="board">{{ count }} cards</section>
<form method="post" action="/board/bump"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"></form>
{% endblock %}
'''


@pytest.fixture
def client(layout):
    testing.add_plugins(layout, {"board": PAGE}, files={"plugins/templates/board.html": BOARD_HTML})
    yield testing.ui_client(layout)
    plugins.reset()


def test_a_plugin_page_has_the_framework_chrome(client):
    html = client.get("/board").text
    assert "2 cards" in html and "<nav" in html and 'href="/board"' in html
    assert f'name="csrf_token" value="{CSRF}"' in html                  # the framework's CSRF token reaches the page


def test_a_plugin_action_needs_the_csrf_token_and_runs_as_an_owner_command(client, layout):
    assert client.post("/board/bump", data={"csrf_token": "forged"}, follow_redirects=False).status_code == 403
    assert not (layout.root / "bumped.txt").exists()
    r = client.post("/board/bump", data={"csrf_token": CSRF}, follow_redirects=False)
    assert r.status_code == 303 and "flash=" in r.headers["location"]
    assert (layout.root / "bumped.txt").read_text(encoding="utf-8") == "owner-ui"


def test_a_plugin_page_refuses_a_foreign_origin(client):
    r = client.post("/board/bump", data={"csrf_token": CSRF}, headers={"Origin": "http://evil.example"},
                    follow_redirects=False)
    assert r.status_code in (400, 403)


def test_flash_is_the_frameworks_banner_value():
    from supervisor.ui.deps import encode_flash
    assert plugin.ui.flash("applied", "uploaded a.png") == encode_flash("applied", "uploaded a.png")


def test_thumbnails_come_from_inside_the_project_only(layout, tmp_path):
    inside = layout.root / "pics" / "a.png"
    inside.parent.mkdir(parents=True)
    Image.new("RGB", (40, 30), "red").save(inside)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.png"
    Image.new("RGB", (40, 30), "blue").save(outside)
    try:
        assert plugin.ui.is_image(inside) and not plugin.ui.is_image(layout.root / "notes.txt")
        key = plugin.ui.thumbnail(layout, inside)
        assert key and key.endswith(".jpg")
        assert plugin.ui.thumbnail(layout, inside, (512, 512)) not in (None, key)
        assert plugin.ui.thumbnail(layout, outside) is None
    finally:
        outside.unlink(missing_ok=True)


def test_supervisor_plugin_imports_without_the_ui_and_ui_loads_on_use():
    code = ("import sys, supervisor.plugin as e; before = 'fastapi' in sys.modules; e.ui; "
            "print(before, 'fastapi' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), capture_output=True, text=True,
                         encoding="utf-8", check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["False", "True"]
