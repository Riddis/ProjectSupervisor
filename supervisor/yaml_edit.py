"""Edits to hand-authored YAML (config/routing.yaml, project.yaml) that change only what they edit.

These files carry owner-decision comments, long lines and the owner's own layout. When only existing scalar values
change, `edit()` replaces exactly those values' tokens, located through the YAML parser (never by searching text);
every other byte stays. Anything else (a key added or removed, a list changed) goes through ruamel's round trip, set
to keep long lines whole, write `null` as `null` and indent lists the way the file already does. Every result is read
back and compared with the requested document, types included. One that differs is not written: an anchor shared by
two keys and edited on one of them changes both (review 1.11.0 F1). The requested document is then written whole,
without the file's comments, and checked the same way; when even that does not read back, the edit is refused
(EditRefused) and nothing is written. ruamel writes YAML 1.2 and the framework reads YAML 1.1 (PyYAML), so a string
that 1.1 would read as another type (`on`, `no`, `~`, `1_000`) is always written quoted (review 1.11.0 R2-1). Writing
the result is the caller's job (store.rewrite_text: atomic, the file's own line endings)."""
from __future__ import annotations

import copy
import io

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.representer import RoundTripRepresenter


class EditRefused(ValueError):
    """No way of writing the requested document reads back as it; the file is left as it was."""


def _unshared(value):
    """A copy with no shared containers: a YAML alias loads as one object under two keys, and an expected document
    built on it would change both when one is assigned."""
    if isinstance(value, dict):
        return {k: _unshared(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unshared(v) for v in value]
    return value


def _kind(value) -> str:
    for kind, types in (("bool", bool), ("int", int), ("float", float), ("str", str), ("map", dict), ("seq", list)):
        if isinstance(value, types):
            return kind
    return "null" if value is None else type(value).__name__


def same(a, b) -> bool:
    """Whether two loaded documents are equal, kinds included: `True == 1` and `1 == 1.0` in Python, not in YAML."""
    if _kind(a) != _kind(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def _reads_as(text: str, doc) -> bool:
    try:
        return same(yaml.safe_load(text), doc)
    except yaml.YAMLError:
        return False


def scalar_changes(old, new, prefix: tuple = ()) -> dict | None:
    """{key path: new value} for every scalar that differs between the mappings `old` and `new`, or None when
    anything else differs: a key added or removed, a list changed, a value that is or becomes a mapping or list."""
    if not isinstance(old, dict) or not isinstance(new, dict) or set(old) != set(new):
        return None
    out: dict = {}
    for key in old:
        a, b = old[key], new[key]
        if isinstance(a, dict) and isinstance(b, dict):
            sub = scalar_changes(a, b, prefix + (key,))
            if sub is None:
                return None
            out.update(sub)
        elif isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
            if a != b:
                return None
        elif not same(a, b):
            out[prefix + (key,)] = b
    return out


def _render(value, style) -> str:
    """`value` as one YAML scalar token: in the quote style the old token had when the value is a string, else
    plain, quoted only where a plain scalar would read back differently ('1.10' stays a string)."""
    quoted = style if isinstance(value, str) and style in ("'", '"') else None
    text = yaml.safe_dump(value, default_style=quoted, allow_unicode=True, width=1 << 30).strip()
    return text[:-3].rstrip() if text.endswith("\n...") else text


def replace_scalars(text: str, changes: dict) -> str | None:
    """`text` with the scalar at each key path replaced by its new value, everything else as it was; None when a
    path does not lead to an existing plain or quoted scalar of a mapping, or the result would not parse to the
    document with those changes."""
    if not changes:
        return text
    try:
        root = yaml.compose(text)
        tokens = {t.end_mark.index: t for t in yaml.scan(text) if isinstance(t, yaml.ScalarToken)}
        expected = _unshared(yaml.safe_load(text))
    except yaml.YAMLError:
        return None
    spans = []
    for path, value in changes.items():
        node, target = root, expected
        for depth, key in enumerate(path):
            if not isinstance(node, yaml.MappingNode) or not isinstance(target, dict) or key not in target:
                return None
            found = [v for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == str(key)]
            if len(found) != 1:
                return None
            node = found[0]
            if depth < len(path) - 1:
                target = target[key]
        if not isinstance(node, yaml.ScalarNode):
            return None
        token = tokens.get(node.end_mark.index)            # a scalar node ends where its token ends
        if token is None or token.style in ("|", ">"):
            return None
        target[path[-1]] = value
        spans.append((token.start_mark.index, token.end_mark.index, _render(value, token.style)))
    spans.sort(reverse=True)
    for (start, end, _), (next_start, _e, _r) in zip(spans[1:], spans):
        if end > next_start:
            return None                                    # one token for two paths (an alias): not a local edit
    out = text
    for start, end, rendered in spans:
        out = out[:start] + rendered + out[end:]
    return out if _reads_as(out, expected) else None    # an aliased token changes the other path too: refused


def merge_into(existing, new: dict) -> None:
    """Update a ruamel round-trip mapping in place from a plain dict: changed values replace, nested mappings and
    lists are merged item by item (keeping their comments and flow style), keys absent from `new` are dropped, new
    keys are appended."""
    for k in [k for k in list(existing.keys()) if k not in new]:
        del existing[k]
    for k, v in new.items():
        cur = existing.get(k) if k in existing else None
        if isinstance(v, dict) and isinstance(cur, dict):
            merge_into(cur, v)
        elif isinstance(v, list) and isinstance(cur, list):
            _merge_list(cur, v)
        elif k not in existing or not same(cur, v):
            existing[k] = v


def _merge_list(existing, new: list) -> None:
    """A ruamel sequence updated item by item; appended items take over the comment (often a blank line) that
    followed the old last item, so the list still ends where it did."""
    old_len = len(existing)
    for i, v in enumerate(new[:old_len]):
        cur = existing[i]
        if isinstance(v, dict) and isinstance(cur, dict):
            merge_into(cur, v)
        elif isinstance(v, list) and isinstance(cur, list):
            _merge_list(cur, v)
        elif not same(cur, v):
            existing[i] = v
    del existing[len(new):]
    if len(new) > old_len:
        comments = getattr(getattr(existing, "ca", None), "items", None)
        tail = comments.pop(old_len - 1, None) if comments is not None and old_len else None
        existing.extend(new[old_len:])
        if tail is not None:
            comments[len(existing) - 1] = tail


class _Representer(RoundTripRepresenter):
    """The round-trip representer, writing None as `null` (ruamel writes an empty value) and quoting a string the
    framework's YAML 1.1 reader would read as another type."""


_READER = yaml.resolver.Resolver()


def _represent_str(representer, data: str):
    """A string, quoted when the framework's reader would read it plain as something else (YAML 1.1: `on` is true,
    `~` is null); ruamel resolves plain scalars by YAML 1.2 and would write those bare."""
    plain_is_str = _READER.resolve(yaml.ScalarNode, data, (True, False)) == "tag:yaml.org,2002:str"
    return representer.represent_scalar("tag:yaml.org,2002:str", data, style=None if plain_is_str else "'")


_Representer.add_representer(type(None), lambda r, _d: r.represent_scalar("tag:yaml.org,2002:null", "null"))
_Representer.add_representer(str, _represent_str)


def _most_common(samples: list, default):
    return max(set(samples), key=samples.count) if samples else default


def _indents(text: str) -> tuple[int, int, int]:
    """(mapping indent, sequence indent, dash offset) as the file writes its block collections, measured from the
    parser's positions: a nested key's column minus its parent key's, and for a list under a key the item's and the
    dash's columns minus the key's. ruamel's own guess assumes mappings and lists share one indent."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return 2, 2, 0
    mapping, sequence = [], []

    def walk(node, parent_column):
        if not isinstance(node, yaml.MappingNode) or node.flow_style:
            return
        for key, value in node.value:
            column = key.start_mark.column
            if parent_column is not None:
                mapping.append(column - parent_column)
            if isinstance(value, yaml.SequenceNode) and not value.flow_style and value.value:
                sequence.append((value.value[0].start_mark.column - column, value.start_mark.column - column))
            elif isinstance(value, yaml.MappingNode):
                walk(value, column)

    walk(root, None)
    mapping_indent = _most_common([m for m in mapping if m > 0], 2)
    seq, offset = _most_common(sequence, (2, 0))
    return mapping_indent, max(seq, offset + 2), max(offset, 0)


def _round_trip(text: str) -> YAML:
    rt = YAML()
    rt.Representer = _Representer
    rt.preserve_quotes = True
    rt.width = 4096                                         # never reflow an owner's long line
    mapping, sequence, offset = _indents(text)
    rt.indent(mapping=mapping, sequence=sequence, offset=offset)
    return rt


def dump_round_trip(text: str, doc: dict) -> str:
    """`doc` written over `text` through ruamel's round trip: comments and key order kept, long lines whole, `null`
    as written, list items indented as the file indents them."""
    rt = _round_trip(text)
    try:
        existing = rt.load(text) if text.strip() else None
    except Exception:                                       # noqa: BLE001 - malformed: replaced by the full document
        existing = None
    if isinstance(existing, dict):
        merge_into(existing, copy.deepcopy(doc))
        out = existing
    else:
        out = copy.deepcopy(doc)
    buf = io.StringIO()
    rt.dump(out, buf)
    return buf.getvalue()


def dump_whole(text: str, doc: dict) -> str:
    """`doc` written whole, with the file's indentation and long lines but none of its comments: the writer of last
    resort, when no edit of the file reads back as `doc`."""
    rt = _round_trip(text)
    buf = io.StringIO()
    rt.dump(_unshared(doc), buf)
    return buf.getvalue()


def edit(text: str, doc: dict) -> str:
    """`text` rewritten to say `doc`: only the changed values' tokens when only existing scalars changed, else the
    careful round trip (dump_round_trip), else `doc` whole (dump_whole). Each candidate is read back and kept only
    when it is `doc`, types included; EditRefused, and nothing to write, when none is. `text` unchanged when `doc`
    says what it already says."""
    try:
        old = yaml.safe_load(text) if text.strip() else None
    except yaml.YAMLError:
        old = None
    changes = scalar_changes(old, doc) if isinstance(old, dict) else None
    if changes is not None:
        out = replace_scalars(text, changes)
        if out is not None and _reads_as(out, doc):
            return out
    for write in (dump_round_trip, dump_whole):
        out = write(text, doc)
        if _reads_as(out, doc):
            return out
    raise EditRefused("no rewrite of the file reads back as the requested document; the file is unchanged")
