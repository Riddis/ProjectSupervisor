"""Core logic: per-image verdicts and label sessions layered over a `round`/`labelling` inbox item.

Ported from a project-local extension of the same shape. Not ported: everything that exercised a project's own
identity-folder layout (a label destination tied to it, its traversal/symlink refusal tests) - that stays with a
project's own plugin (boundary D6, D7). This package's own label destination is `labels_path_template`
(`plugins.review_rounds`), proved generically below with an invented field name instead."""
from __future__ import annotations

import random

import pytest
import yaml
from PIL import Image

import supervisor_plugin_review_rounds as rr
from supervisor import plugin, testing


def make_round(layout, kind="round", n=3, reason_tags=None, references=False, retest_pairs=()):
    rows = []
    for i in range(n):
        p = layout.root / f"img{i}.png"
        Image.new("RGB", (32, 32), (10 * i % 256, 20, 30)).save(p)
        rows.append({"id": f"im{i}", "path": str(p.relative_to(layout.root)),
                    "identity_mean": round(0.5 + i * 0.05, 2), "decision": "KEEP"})
    for retest_id, source_id in retest_pairs:
        rows.append({"id": retest_id, "path": rows[0]["path"], "retest_of": source_id})
    fields = {"items": rows}
    if reason_tags:
        fields["reason_tags"] = reason_tags
    if references:
        fields["references"] = [rows[0]["path"]]
    return testing.make_inbox_item(layout, kind=kind, subject="round 1", summary="review these",
                                   allowed_responses=["SUBMIT"], **fields)


# ------------------------------------------------------------------- set_verdict


def test_set_verdict_persists_across_a_fresh_load(layout):
    item = make_round(layout, n=3)
    rr.set_verdict(layout, item["id"], "im0", "accept", note="looks good", by="owner")

    reloaded = testing.inbox_item(layout, item["id"])
    row = next(r for r in reloaded["items"] if r["id"] == "im0")
    assert row["verdict"] == "ACCEPT"            # normalised upper-case regardless of input case
    assert row["note"] == "looks good"
    assert row["verdict_by"] == "owner"
    assert row["verdict_at"]
    assert rr.progress(reloaded) == (1, 3)


def test_set_verdict_rejects_unknown_verdict(layout):
    item = make_round(layout, n=1)
    with pytest.raises(rr.RoundError):
        rr.set_verdict(layout, item["id"], "im0", "MAYBE")


def test_set_verdict_rejects_unknown_image_id(layout):
    item = make_round(layout, n=1)
    with pytest.raises(rr.RoundError):
        rr.set_verdict(layout, item["id"], "no-such-image", "ACCEPT")


def test_set_verdict_rejects_a_plain_question_item(layout):
    item = testing.make_inbox_item(layout, kind="question", subject="s", summary="body")
    with pytest.raises(rr.RoundError):
        rr.set_verdict(layout, item["id"], "im0", "ACCEPT")


def test_inbox_answer_refuses_a_round_with_unanswered_images(layout):
    """The generic inbox path must not be able to close a round out from under the review grid: calling the
    owner's generic answer directly on a round/labelling item is refused regardless of command source, and the
    item stays open."""
    item = make_round(layout, n=2)
    rr.set_verdict(layout, item["id"], "im0", "ACCEPT")
    with pytest.raises(plugin.InboxError):
        testing.answer(layout, item["id"], "SUBMIT")
    assert testing.inbox_item(layout, item["id"])["status"] == "open"


def test_commands_inbox_answer_handler_refuses_a_round(layout):
    item = make_round(layout, n=2)
    rr.set_verdict(layout, item["id"], "im0", "ACCEPT")
    outcome = plugin.submit_command(layout, "inbox.answer", {"item_id": item["id"], "response": "SUBMIT"},
                                    actor="owner-ui")
    assert outcome.get("ok") is False
    assert testing.inbox_item(layout, item["id"])["status"] == "open"


def test_set_verdict_rejects_once_the_item_is_answered(layout):
    item = make_round(layout, n=1)
    rr.set_verdict(layout, item["id"], "im0", "ACCEPT")
    rr.submit(layout, item["id"])
    with pytest.raises(rr.RoundError):
        rr.set_verdict(layout, item["id"], "im0", "REJECT")


# ------------------------------------------------------------------- submit


def test_submit_refuses_an_incomplete_round_without_partial(layout):
    item = make_round(layout, n=3)
    rr.set_verdict(layout, item["id"], "im0", "ACCEPT")
    rr.set_verdict(layout, item["id"], "im1", "REJECT")
    with pytest.raises(rr.RoundError):
        rr.submit(layout, item["id"])
    assert testing.inbox_item(layout, item["id"])["status"] == "open"


def test_submit_partial_accepts_an_incomplete_round(layout):
    item = make_round(layout, n=3)
    rr.set_verdict(layout, item["id"], "im0", "ACCEPT")
    result = rr.submit(layout, item["id"], partial=True)
    assert result["answered"] == 1 and result["total"] == 3
    assert testing.inbox_item(layout, item["id"])["status"] == "answered"


def test_submit_answers_the_inbox_item_with_the_per_image_verdicts_already_on_it(layout):
    item = make_round(layout, n=2)
    rr.set_verdict(layout, item["id"], "im0", "ACCEPT")
    rr.set_verdict(layout, item["id"], "im1", "REJECT")
    rr.submit(layout, item["id"])

    answered = testing.inbox_item(layout, item["id"])
    assert answered["status"] == "answered"
    verdicts = {r["id"]: r["verdict"] for r in answered["items"]}
    assert verdicts == {"im0": "ACCEPT", "im1": "REJECT"}


def test_submit_writes_labels_under_the_configured_template(layout):
    item = make_round(layout, kind="labelling", n=2)
    rr.set_verdict(layout, item["id"], "im0", "ACCEPT")
    rr.set_verdict(layout, item["id"], "im1", "REJECT")
    result = rr.submit(layout, item["id"])

    label_file = layout.state / "qa" / "labels" / f"{item['id']}.yaml"   # labels_path_template's neutral default
    assert label_file.exists()
    assert result["label_file"] == str(label_file.relative_to(layout.root))
    doc = yaml.safe_load(label_file.read_text(encoding="utf-8"))
    assert {i["id"]: i["verdict"] for i in doc["items"]} == {"im0": "ACCEPT", "im1": "REJECT"}


def test_submit_resubmitting_an_answered_round_does_not_rewrite_the_label(layout):
    item = make_round(layout, kind="labelling", n=1)
    rr.set_verdict(layout, item["id"], "im0", "ACCEPT")
    rr.submit(layout, item["id"], session_seconds=123)

    label_file = layout.state / "qa" / "labels" / f"{item['id']}.yaml"
    before = label_file.read_bytes()
    with pytest.raises(rr.RoundError):
        rr.submit(layout, item["id"], session_seconds=2)
    assert label_file.read_bytes() == before
    doc = yaml.safe_load(label_file.read_text(encoding="utf-8"))
    assert doc["session_seconds"] == 123


def test_submit_replay_with_the_same_command_id_is_a_noop(layout):
    item = make_round(layout, kind="labelling", n=1)
    rr.set_verdict(layout, item["id"], "im0", "ACCEPT")
    rr.submit(layout, item["id"], session_seconds=123, command_id="cmd-1")

    label_file = layout.state / "qa" / "labels" / f"{item['id']}.yaml"
    before = label_file.read_bytes()
    result = rr.submit(layout, item["id"], session_seconds=999, command_id="cmd-1")
    assert result["status"] == "answered"
    assert label_file.read_bytes() == before                  # a replayed command must not touch the file


def test_a_round_kind_item_writes_no_label_file(layout):
    item = make_round(layout, kind="round", n=1)
    rr.set_verdict(layout, item["id"], "im0", "ACCEPT")
    result = rr.submit(layout, item["id"])
    assert result["label_file"] is None
    assert not (layout.state / "qa").exists()


# ------------------------------------------------------------------- labels_path_template (config moved, not lost)


def test_labels_path_rejects_a_template_field_the_round_does_not_have(layout, tmp_path):
    layout2 = testing.project(tmp_path / "p2", plugins=["review_rounds"],
                              routing={"plugins": {"review_rounds": {"labels_path_template": "state/qa/{batch_id}/{item_id}.yaml"}}})
    item = make_round(layout2, kind="labelling", n=1)
    with pytest.raises(rr.RoundError, match="batch_id"):
        rr.labels_path(layout2, item, item["id"])


def test_labels_path_neutralises_a_traversal_field_and_stays_under_the_project_root(layout, tmp_path):
    layout2 = testing.project(tmp_path / "p2", plugins=["review_rounds"],
                              routing={"plugins": {"review_rounds": {"labels_path_template": "state/qa/{batch_id}/{item_id}.yaml"}}})
    item = make_round(layout2, kind="labelling", n=1)
    item = testing.inbox_item(layout2, item["id"])
    from supervisor import store
    from supervisor import inbox as inbox_mod
    store.update(inbox_mod.path_for(layout2, item["id"]), lambda doc: {**doc, "batch_id": "../../outside-review-root"},
                lock_path=layout2.state / ".inbox.lock")
    item = testing.inbox_item(layout2, item["id"])
    dest = rr.labels_path(layout2, item, item["id"])
    assert layout2.root in dest.parents
    assert (layout2.state / "qa") in dest.parents


def test_labels_path_rejects_a_field_that_sanitises_to_empty(layout, tmp_path):
    layout2 = testing.project(tmp_path / "p2", plugins=["review_rounds"],
                              routing={"plugins": {"review_rounds": {"labels_path_template": "state/qa/{batch_id}/{item_id}.yaml"}}})
    item = make_round(layout2, kind="labelling", n=1)
    from supervisor import store
    from supervisor import inbox as inbox_mod
    store.update(inbox_mod.path_for(layout2, item["id"]), lambda doc: {**doc, "batch_id": "../../"},
                lock_path=layout2.state / ".inbox.lock")
    item = testing.inbox_item(layout2, item["id"])
    with pytest.raises(rr.RoundError):
        rr.labels_path(layout2, item, item["id"])


def test_labels_path_refuses_a_symlinked_destination_directory(layout, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (layout.state / "qa").parent.mkdir(parents=True, exist_ok=True)
    try:
        (layout.state / "qa").symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink support")
    item = make_round(layout, kind="labelling", n=1)
    with pytest.raises(rr.RoundError):
        rr.labels_path(layout, item, item["id"])
    assert not (elsewhere / f"{item['id']}.yaml").exists()     # nothing was written through the link either


# ------------------------------------------------------------------- agreement stats


def test_agreement_stats_counts_matching_and_mismatched_retest_pairs(layout):
    item = make_round(layout, kind="labelling", n=2, retest_pairs=[("im0-retest", "im0")])
    rr.set_verdict(layout, item["id"], "im0", "ACCEPT")
    rr.set_verdict(layout, item["id"], "im0-retest", "ACCEPT")
    rr.set_verdict(layout, item["id"], "im1", "REJECT")

    stats = rr.agreement_stats(testing.inbox_item(layout, item["id"]))
    assert stats == {"pairs": 1, "scored": 1, "agreed": 1, "agreement_rate": 1.0}


def test_agreement_stats_is_empty_with_no_retest_pairs(layout):
    item = make_round(layout, n=2)
    stats = rr.agreement_stats(testing.inbox_item(layout, item["id"]))
    assert stats == {"pairs": 0, "scored": 0, "agreed": 0, "agreement_rate": None}


# ------------------------------------------------------------------- label order (randomised, persisted)


def test_ensure_label_order_is_a_permutation_persisted_across_calls(layout):
    item = make_round(layout, kind="labelling", n=12)
    first = rr.ensure_label_order(layout, item["id"])
    second = rr.ensure_label_order(layout, item["id"])
    assert sorted(first) == sorted(rr.image_ids(item))
    assert first == second                                    # persisted, not reshuffled on a second call
    assert testing.inbox_item(layout, item["id"])["label_order"] == first


def test_ensure_label_order_matches_the_documented_seeded_shuffle(layout):
    """Locks in the exact algorithm (`random.Random(item_id).shuffle`) so a refactor that changes it is caught -
    not just "it's some permutation"."""
    item = make_round(layout, kind="labelling", n=12)
    expected = rr.image_ids(item)
    random.Random(item["id"]).shuffle(expected)
    assert rr.ensure_label_order(layout, item["id"]) == expected


def test_a_plain_round_keeps_creation_order(layout):
    item = make_round(layout, kind="round", n=5)
    assert rr.ensure_label_order(layout, item["id"]) == rr.image_ids(item)
    assert "label_order" not in testing.inbox_item(layout, item["id"])


def test_label_order_is_read_only_and_matches_ensure_label_order(layout):
    """`label_order()` is the pure version the review page's GET uses (no mutation on a GET) - it must agree
    with the persisting `ensure_label_order()` without writing anything itself."""
    item = make_round(layout, kind="labelling", n=12)
    before = testing.inbox_item(layout, item["id"])
    computed = rr.label_order(item)
    after = testing.inbox_item(layout, item["id"])
    assert before == after                                     # untouched on disk
    assert computed == rr.ensure_label_order(layout, item["id"])


def test_submit_persists_the_label_order_the_owner_actually_saw(layout):
    item = make_round(layout, kind="labelling", n=5)
    expected = rr.label_order(item)
    for image_id in expected:
        rr.set_verdict(layout, item["id"], image_id, "ACCEPT")
    rr.submit(layout, item["id"])
    assert testing.inbox_item(layout, item["id"])["label_order"] == expected


# ------------------------------------------------------------------- reason tags


def test_set_verdict_with_reason_tags_persists_them(layout):
    item = make_round(layout, n=1, reason_tags=["blurry", "artifact"])
    rr.set_verdict(layout, item["id"], "im0", "REJECT", reason_tags=["blurry"])
    row = testing.inbox_item(layout, item["id"])["items"][0]
    assert row["reason_tags"] == ["blurry"]


def test_set_verdict_without_reason_tags_leaves_existing_tags_untouched(layout):
    item = make_round(layout, n=1, reason_tags=["blurry"])
    rr.set_verdict(layout, item["id"], "im0", "REJECT", reason_tags=["blurry"])
    rr.set_verdict(layout, item["id"], "im0", "MODIFY", note="again")      # reason_tags=None (default)
    row = testing.inbox_item(layout, item["id"])["items"][0]
    assert row["reason_tags"] == ["blurry"]


def test_set_verdict_with_an_empty_reason_tags_list_clears_it(layout):
    item = make_round(layout, n=1, reason_tags=["blurry"])
    rr.set_verdict(layout, item["id"], "im0", "REJECT", reason_tags=["blurry"])
    rr.set_verdict(layout, item["id"], "im0", "REJECT", reason_tags=[])
    row = testing.inbox_item(layout, item["id"])["items"][0]
    assert row["reason_tags"] == []
