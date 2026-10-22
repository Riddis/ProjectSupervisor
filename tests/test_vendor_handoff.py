import hashlib, json
import pytest
from supervisor._vendor import handoff as h


def test_instructions_mention_files(tmp_path):
    text = h.instructions(tmp_path, prior="X/HANDOFF.md")
    assert "HANDOFF_REQUEST.json" in text and "X/HANDOFF.md" in text and "CAPACITY HANDOFF: INCOMPLETE" in text


def test_request_and_save_fallback(tmp_path):
    h.request(tmp_path, reset_epoch=123)
    assert json.loads((tmp_path / "HANDOFF_REQUEST.json").read_text())["requested"] is True
    (tmp_path / "result.txt").write_text("line1\nline2\n")
    p = h.save(tmp_path, {"session_id": "s", "status": "FAILED"}, purpose="demo")
    assert "UNVERIFIED" in p.read_text() and "line2" in p.read_text()
    assert not h.incomplete(tmp_path)
    p.write_text("CAPACITY HANDOFF: INCOMPLETE\n...")
    assert h.incomplete(tmp_path)


def test_continuation_fields_and_baseline(tmp_path):
    assert h.continuation_fields(tmp_path, writer=True) == {}
    (tmp_path / "HANDOFF.md").write_text("x")
    (tmp_path / "scope-before.json").write_text(json.dumps({"a": 1}))
    f = h.continuation_fields(tmp_path, writer=True)
    assert f["continuation_scope_sha256"] == hashlib.sha256(b'{"a": 1}').hexdigest()
    assert h.scope_baseline(f, {"current": True}) == {"a": 1}
    (tmp_path / "scope-before.json").write_text(json.dumps({"a": 2}))
    with pytest.raises(ValueError):
        h.scope_baseline(f, {})
    assert h.scope_baseline({}, {"current": True}) == {"current": True}
