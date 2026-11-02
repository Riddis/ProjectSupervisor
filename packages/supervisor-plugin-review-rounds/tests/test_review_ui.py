"""Control Center review grid: the FastAPI routes, against the test client `supervisor.testing.ui_client` gives
every plugin's own tests - never `create_app`/`TestClient` directly, so a plugin's tests never import a
framework internal.

Not ported: the external-edit navigation and upload-form tests, and everything that exercised a project's own
identity-folder label destination - that stays with a project's own plugin (boundary D6, D7). The grid page
itself carries no upload form and no project-specific identity-folder path, asserted directly below."""
from __future__ import annotations

import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import supervisor_plugin_review_rounds as rr
from supervisor import testing

from test_rounds import make_round

CSRF = testing.CSRF


@pytest.fixture
def client(layout):
    return testing.ui_client(layout)


def post(client, url, **data):
    return client.post(url, data={"csrf_token": CSRF, **data}, follow_redirects=False)


def scored_layout(tmp_path):
    return testing.project(tmp_path / "scored", plugins=["review_rounds"], routing={"plugins": {"review_rounds": {
        "tile_score_fields": ["identity_mean", "decision"],
        "filters": [{"field": "decision", "label": "Decision", "kind": "select", "options": ["GOOD", "MAYBE", "BAD"]},
                    {"field": "identity_mean", "label": "Min identity", "kind": "number_min"}],
    }}})


# ------------------------------------------------------------------- grid page carries no upload form, no project-specific path


def test_review_page_has_no_upload_form(layout, client):
    item = make_round(layout, kind="round", n=1)
    page = client.get(f"/review/{item['id']}")
    assert page.status_code == 200
    assert "multipart/form-data" not in page.text
    assert 'type="file"' not in page.text
    assert "/upload" not in page.text
    assert f"<title>Review {item['id']} — Control Center</title>" in page.text   # the title names no project


def test_review_index_page_title_names_no_project(layout, client):
    assert "<title>Review — Control Center</title>" in client.get("/review").text


# ------------------------------------------------------------------- grid page


def test_review_page_renders_tiles_and_configured_scores(tmp_path):
    layout = scored_layout(tmp_path)
    client = testing.ui_client(layout)
    item = make_round(layout, kind="round", n=3)
    r = client.get(f"/review/{item['id']}")
    assert r.status_code == 200
    assert 'data-id="im0"' in r.text
    assert 'data-id="im2"' in r.text
    assert "identity_mean: 0.5" in r.text        # score visible: not a labelling (blind) session, field configured


def test_review_page_shows_no_scores_when_none_are_configured(layout, client):
    item = make_round(layout, kind="round", n=1)
    r = client.get(f"/review/{item['id']}")
    assert "identity_mean" not in r.text          # the default config names no tile_score_fields


def test_review_page_404s_for_a_missing_item(layout, client):
    r = client.get("/review/H-999")
    assert r.status_code == 404


def test_review_page_400s_for_a_non_round_item(layout, client):
    item = testing.make_inbox_item(layout, kind="question", subject="s", summary="body")
    r = client.get(f"/review/{item['id']}")
    assert r.status_code == 400


# ------------------------------------------------------------------- verdicts persist and survive reload


def test_verdict_persists_and_survives_a_reload(layout, client):
    item = make_round(layout, n=2)
    r = post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="ACCEPT")
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "image_id": "im0", "verdict": "ACCEPT", "answered": 1, "total": 2}

    reloaded = client.get(f"/review/{item['id']}")
    tile = re.search(r'data-id="im0"[^>]*', reloaded.text).group(0)
    assert 'data-verdict="ACCEPT"' in tile
    assert "1 of 2" in reloaded.text

    assert testing.inbox_item(layout, item["id"])["items"][0]["verdict"] == "ACCEPT"   # on disk, not just the page


def test_verdict_rejects_an_unknown_verdict_token(layout, client):
    item = make_round(layout, n=1)
    r = post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="BOGUS")
    assert r.status_code == 400
    assert testing.inbox_item(layout, item["id"])["items"][0].get("verdict") is None


def test_verdict_rejects_an_unknown_image_id(layout, client):
    item = make_round(layout, n=1)
    r = post(client, f"/review/{item['id']}/verdict", image_id="no-such-image", verdict="ACCEPT")
    assert r.status_code == 400


# ------------------------------------------------------------------- keyboard-shortcut mapping


KEYBOARD_VERDICTS = {"a": "ACCEPT", "r": "REJECT", "x": "MODIFY", "g": "REDO"}


def test_every_keyboard_shortcut_verdict_is_accepted_by_the_backend(layout, client):
    item = make_round(layout, n=len(KEYBOARD_VERDICTS))
    ids = rr.image_ids(testing.inbox_item(layout, item["id"]))
    for image_id, (_, verdict) in zip(ids, KEYBOARD_VERDICTS.items()):
        r = post(client, f"/review/{item['id']}/verdict", image_id=image_id, verdict=verdict)
        assert r.status_code == 200 and r.json()["verdict"] == verdict


def test_review_js_keyboard_map_matches_the_documented_verdicts():
    """The one place the key -> verdict mapping is defined for the owner (review.js) must name exactly the
    verdicts the backend accepts (rr.VERDICTS) - a typo here would silently strand a keyboard shortcut."""
    static_dir = Path(rr.__file__).resolve().parent / "static"
    text = (static_dir / "review.js").read_text(encoding="utf-8")
    match = re.search(r"VERDICT_KEYS\s*=\s*\{([^}]*)\}", text)
    assert match, "VERDICT_KEYS object not found in review.js"
    pairs = dict(re.findall(r'(\w+)\s*:\s*"(\w+)"', match.group(1)))
    assert pairs == KEYBOARD_VERDICTS
    assert set(pairs.values()) == set(rr.VERDICTS)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH (no JS runtime to execute review.js)")
def test_review_js_keyboard_verdict_targets_the_clicked_compare_tile():
    """Runs the real review.js against a hand-written DOM/fetch stub (tests/js/dom_stub.js - no npm, no jsdom):
    click Compare on im1, press A, and the verdict POST must carry image_id=im1, not whichever tile loaded
    first. Also covers A with no prior click/focus at all (falls back to the first visible tile)."""
    script = Path(__file__).parent / "js" / "review_interaction_test.js"
    static_review_js = Path(rr.__file__).resolve().parent / "static" / "review.js"
    result = subprocess.run(["node", str(script), str(static_review_js)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


# ------------------------------------------------------------------- labelling mode: blind + randomised


def test_labelling_page_hides_scores_by_default(tmp_path):
    layout = scored_layout(tmp_path)
    client = testing.ui_client(layout)
    item = make_round(layout, kind="labelling", n=2)
    r = client.get(f"/review/{item['id']}")
    assert r.status_code == 200
    assert "identity_mean: 0.5" not in r.text          # the score value is blind; the filter control itself is not
    assert "reveal scores" in r.text


def test_labelling_reveal_query_param_shows_scores_and_is_logged(tmp_path):
    layout = scored_layout(tmp_path)
    client = testing.ui_client(layout)
    item = make_round(layout, kind="labelling", n=2)
    r = client.get(f"/review/{item['id']}?reveal=1")
    assert r.status_code == 200
    assert "identity_mean: 0.5" in r.text

    log_text = layout.log.read_text(encoding="utf-8")
    assert "labelling_reveal" in log_text


def test_labelling_order_is_randomised_and_stable_across_requests(layout, client):
    item = make_round(layout, kind="labelling", n=12)
    expected = rr.image_ids(testing.inbox_item(layout, item["id"]))
    random.Random(item["id"]).shuffle(expected)

    r1 = client.get(f"/review/{item['id']}")
    r2 = client.get(f"/review/{item['id']}")
    order1 = re.findall(r'data-id="(im\d+)"', r1.text)
    order2 = re.findall(r'data-id="(im\d+)"', r2.text)
    assert order1 == order2 == expected
    assert order1 != sorted(order1)                   # genuinely shuffled, not coincidentally sorted


def test_round_kind_is_not_randomised(layout, client):
    item = make_round(layout, kind="round", n=6)
    r = client.get(f"/review/{item['id']}")
    order = re.findall(r'data-id="(im\d+)"', r.text)
    assert order == [f"im{i}" for i in range(6)]


def test_review_page_get_never_writes_label_order_to_disk(layout, client):
    """The grid page is a GET - it must stay read-only, unlike a persisting `ensure_label_order()` call. Two
    reloads must still agree on the order (asserted elsewhere) without persisting it."""
    item = make_round(layout, kind="labelling", n=5)
    client.get(f"/review/{item['id']}")
    client.get(f"/review/{item['id']}")
    assert "label_order" not in testing.inbox_item(layout, item["id"])


# ------------------------------------------------------------------- reason tags (labelling)


def test_reason_tag_checkboxes_render_per_tile(layout, client):
    item = make_round(layout, kind="labelling", n=2, reason_tags=["blurry", "artifact"])
    r = client.get(f"/review/{item['id']}")
    assert r.text.count('class="tile-tag-input" value="blurry"') == 2     # one per tile
    assert r.text.count('class="tile-tag-input" value="artifact"') == 2


def test_a_tag_chosen_in_the_grid_appears_in_the_written_label_file(layout, client):
    item = make_round(layout, kind="labelling", n=2, reason_tags=["blurry", "artifact"])
    post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="REJECT",
        reason_tags="blurry", reason_tags_set="1")
    post(client, f"/review/{item['id']}/verdict", image_id="im1", verdict="ACCEPT",
        reason_tags="", reason_tags_set="1")
    post(client, f"/review/{item['id']}/submit")

    label_file = layout.state / "qa" / "labels" / f"{item['id']}.yaml"
    doc = yaml.safe_load(label_file.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in doc["items"]}
    assert by_id["im0"]["reason_tags"] == ["blurry"]
    assert by_id["im1"]["reason_tags"] == []


def test_reason_tags_can_be_cleared_once_set(layout, client):
    item = make_round(layout, n=1, reason_tags=["blurry"])
    post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="REJECT",
        reason_tags="blurry", reason_tags_set="1")
    assert testing.inbox_item(layout, item["id"])["items"][0]["reason_tags"] == ["blurry"]

    post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="REJECT",
        reason_tags="", reason_tags_set="1")
    assert testing.inbox_item(layout, item["id"])["items"][0]["reason_tags"] == []


def test_omitting_reason_tags_leaves_the_existing_tags_untouched(layout, client):
    item = make_round(layout, n=1, reason_tags=["blurry"])
    post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="REJECT",
        reason_tags="blurry", reason_tags_set="1")
    post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="MODIFY", note="try again")
    assert testing.inbox_item(layout, item["id"])["items"][0]["reason_tags"] == ["blurry"]


def test_review_js_posts_reason_tags_from_the_tile_checkboxes():
    static_dir = Path(rr.__file__).resolve().parent / "static"
    text = (static_dir / "review.js").read_text(encoding="utf-8")
    assert "tile-tag-input" in text
    assert "params.reason_tags = tileTags(tile)" in text
    assert 'params.reason_tags_set = "1"' in text


# ------------------------------------------------------------------- queued verdicts (supervisor holds the lock)


def test_a_queued_verdict_is_not_written_to_disk_until_applied(layout, client):
    item = make_round(layout, n=1)
    with testing.loop_lock(layout):
        r = post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="ACCEPT")
        assert r.status_code == 200
        assert r.json()["queued"] is True
    assert testing.inbox_item(layout, item["id"])["items"][0].get("verdict") is None


def test_review_js_treats_a_queued_verdict_as_pending_not_saved():
    """A `queued: true` response means the command is only sitting in state/commands/ - not yet on the item.
    The tile must not look answered until a reload shows it really is."""
    static_dir = Path(rr.__file__).resolve().parent / "static"
    text = (static_dir / "review.js").read_text(encoding="utf-8")
    assert "data.queued" in text
    match = re.search(r"function applyQueuedResult\([^)]*\)\s*\{([^}]*)\}", text)
    assert match, "applyQueuedResult() not found in review.js"
    assert "has-verdict" not in match.group(1)


# ------------------------------------------------------------------- submit


def test_submit_without_answering_everything_is_rejected(layout, client):
    item = make_round(layout, n=3)
    post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="ACCEPT")
    r = post(client, f"/review/{item['id']}/submit")
    assert r.status_code == 303
    assert "flash=error" in r.headers["location"]
    assert testing.inbox_item(layout, item["id"])["status"] == "open"


def test_submit_partial_is_accepted_explicitly(layout, client):
    item = make_round(layout, n=3)
    post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="ACCEPT")
    r = post(client, f"/review/{item['id']}/submit", partial="1")
    assert r.status_code == 303
    assert "flash=applied" in r.headers["location"]
    assert testing.inbox_item(layout, item["id"])["status"] == "answered"


def test_inbox_answer_endpoint_refuses_a_round_with_unanswered_images(layout, client):
    """The generic /inbox/{id}/answer route must not be able to close a round out from under the review grid -
    it 400s and points at /review/{id}, while /review/{id}/submit keeps working."""
    item = make_round(layout, n=2)
    post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="ACCEPT")
    r = post(client, f"/inbox/{item['id']}/answer", response="SUBMIT")
    assert r.status_code == 400
    assert f"/review/{item['id']}" in r.json()["detail"]
    assert testing.inbox_item(layout, item["id"])["status"] == "open"

    post(client, f"/review/{item['id']}/verdict", image_id="im1", verdict="REJECT")
    r2 = post(client, f"/review/{item['id']}/submit")
    assert r2.status_code == 303 and "flash=applied" in r2.headers["location"]


def test_submit_full_round_answers_item_and_writes_labels(layout, client):
    item = make_round(layout, kind="labelling", n=2)
    post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="ACCEPT")
    post(client, f"/review/{item['id']}/verdict", image_id="im1", verdict="REJECT")
    r = post(client, f"/review/{item['id']}/submit", session_seconds="42")
    assert r.status_code == 303
    assert "flash=applied" in r.headers["location"]

    label_file = layout.state / "qa" / "labels" / f"{item['id']}.yaml"
    assert label_file.exists()
    doc = yaml.safe_load(label_file.read_text(encoding="utf-8"))
    assert doc["session_seconds"] == 42.0


# ------------------------------------------------------------------- labelling blind leaks


def test_labelling_item_inbox_render_has_no_score_detail(tmp_path):
    layout = scored_layout(tmp_path)
    client = testing.ui_client(layout)
    item = make_round(layout, kind="labelling", n=2)
    r = client.get("/inbox")
    assert r.status_code == 200
    assert "identity_mean" not in r.text
    assert f"/review/{item['id']}" in r.text                  # the grid link survives


def test_open_labelling_session_grid_shows_no_agreement_text(layout, client):
    item = make_round(layout, kind="labelling", n=2, retest_pairs=[("im0-retest", "im0")])
    post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="ACCEPT")
    post(client, f"/review/{item['id']}/verdict", image_id="im0-retest", verdict="ACCEPT")
    r = client.get(f"/review/{item['id']}")
    assert "test–retest agreement" not in r.text


def test_agreement_text_appears_only_after_submit(layout, client):
    item = make_round(layout, kind="labelling", n=2, retest_pairs=[("im0-retest", "im0")])
    post(client, f"/review/{item['id']}/verdict", image_id="im0", verdict="ACCEPT")
    post(client, f"/review/{item['id']}/verdict", image_id="im0-retest", verdict="ACCEPT")
    post(client, f"/review/{item['id']}/verdict", image_id="im1", verdict="REJECT")
    post(client, f"/review/{item['id']}/submit")

    r = client.get(f"/review/{item['id']}")
    assert "test–retest agreement" in r.text


# ------------------------------------------------------------------- round-level references feed Compare


def test_round_level_references_give_every_tile_a_compare_button(layout, client):
    item = make_round(layout, kind="round", n=2, references=True)
    r = client.get(f"/review/{item['id']}")
    assert r.text.count('class="tile-compare"') == 2


def test_review_grid_opts_out_of_live_updates():
    """The image review grid keeps its own client state (filters, selection, timer); a live morph would reset it."""
    template = Path(rr.__file__).resolve().parent / "templates" / "review.html"
    assert 'id="review-grid" data-live-off' in template.read_text(encoding="utf-8")


# ------------------------------------------------------------------- configured filters (config moved, not lost)


def test_configured_filters_render_as_generic_controls(tmp_path):
    layout = scored_layout(tmp_path)
    client = testing.ui_client(layout)
    item = make_round(layout, kind="round", n=1)
    r = client.get(f"/review/{item['id']}")
    assert 'class="review-filter" data-field="decision" data-kind="select"' in r.text
    assert 'class="review-filter" data-field="identity_mean" data-kind="number_min"' in r.text
    assert 'data-filter-decision="KEEP"' in r.text


def test_a_project_with_no_filters_configured_still_gets_the_verdict_filter(layout, client):
    item = make_round(layout, kind="round", n=1)
    r = client.get(f"/review/{item['id']}")
    assert 'class="review-filter" data-field="verdict" data-kind="verdict"' in r.text
