"""store.rewrite_text replaces a file atomically (review 1.10.1 F2) and keeps what the old file had: its owner, group
and permissions (POSIX mode bits; the Windows DACL, its protection included), and its links (review 1.10.1 F3). When
that cannot be kept, the rewrite is refused and the old file stays as it was."""
import os
import stat
import subprocess
import sys

import pytest
import yaml

from supervisor import migrations, store, template
from supervisor._vendor import contract
from supervisor.paths import Layout

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")


def _sddl(path) -> str:
    """The file's owner, group and DACL as SDDL, read natively (GetFileSecurityW + ConvertSecurityDescriptorTo-
    StringSecurityDescriptorW), not through the code under test and not through a PowerShell child, whose module path
    depends on the caller's environment (review 1.10.1 F7: Get-Acl could not load in Windows CI)."""
    import ctypes
    from ctypes import wintypes
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi32.GetFileSecurityW
    get_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD)]
    get_security.restype = wintypes.BOOL
    to_string = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    to_string.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR),
                          ctypes.POINTER(wintypes.ULONG)]
    to_string.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    info = 0x1 | 0x2 | 0x4                                  # owner, group, DACL
    needed = wintypes.DWORD(0)
    get_security(str(path), info, None, 0, ctypes.byref(needed))
    assert needed.value, ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(needed.value)
    assert get_security(str(path), info, buffer, needed, ctypes.byref(needed)), ctypes.WinError(ctypes.get_last_error())
    text = wintypes.LPWSTR()
    assert to_string(buffer, 1, info, ctypes.byref(text), None), ctypes.WinError(ctypes.get_last_error())
    try:
        return text.value
    finally:
        kernel32.LocalFree(text)


def _leftovers(directory, name):
    return [p.name for p in directory.iterdir() if p.name.startswith(f".{name}.")]


@posix_only
@pytest.mark.parametrize("mode", [0o640, 0o664, 0o600, 0o644])
def test_a_rewrite_keeps_the_mode_owner_and_group(tmp_path, mode):
    p = tmp_path / "doc.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    os.chmod(p, mode)
    before = os.stat(p)
    assert store.rewrite_text(p, "a: 2\n")
    after = os.stat(p)
    assert p.read_text(encoding="utf-8") == "a: 2\n"
    assert (stat.S_IMODE(after.st_mode), after.st_uid, after.st_gid) == (mode, before.st_uid, before.st_gid)


@posix_only
def test_a_rewrite_keeps_a_supplementary_group(tmp_path):
    """A file shared through a group the writer belongs to, but not its primary group, keeps that group."""
    extra = [g for g in os.getgroups() if g != os.getgid()]
    if not extra:
        pytest.skip("the test account has no supplementary group")
    p = tmp_path / "shared.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    os.chown(p, -1, extra[0])
    os.chmod(p, 0o660)
    assert store.rewrite_text(p, "a: 2\n")
    assert (os.stat(p).st_gid, stat.S_IMODE(os.stat(p).st_mode)) == (extra[0], 0o660)


@posix_only
def test_migrate_keeps_the_modes_of_the_files_it_rewrites(tmp_path):
    """The reviewer's probe: project.yaml 0640 (version update) and config/routing.yaml 0664 (max_gpu_jobs rename)."""
    template.stamp(tmp_path / "p", name="Probe")
    layout = Layout(tmp_path / "p")
    raw = yaml.safe_load(layout.project_file.read_text(encoding="utf-8"))
    raw["framework_version"] = "1.9.0"
    layout.project_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    routing = layout.config / "routing.yaml"
    routing.write_text(routing.read_text(encoding="utf-8") + "\nlimits:\n  max_gpu_jobs: 1\n", encoding="utf-8")
    os.chmod(layout.project_file, 0o640)
    os.chmod(routing, 0o664)
    notes = migrations.run(layout)
    assert any("max_resource_jobs" in n for n in notes) and any("framework_version" in n for n in notes)
    assert stat.S_IMODE(os.stat(layout.project_file).st_mode) == 0o640
    assert stat.S_IMODE(os.stat(routing).st_mode) == 0o664


@windows_only
@pytest.mark.parametrize("protected", [True, False])
def test_a_rewrite_keeps_the_windows_acl(tmp_path, protected):
    """Protected: a DACL granting only the current user, which the directory's inherited ACEs must not replace.
    Unprotected: the inherited DACL a file normally has."""
    p = tmp_path / "doc.yaml"
    p.write_bytes(b"a: 1\r\n")
    if protected:
        user = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
        subprocess.run(["icacls", str(p), "/inheritance:r", "/grant:r", f"{user}:F"], capture_output=True, check=True)
    before = _sddl(p)
    assert ("D:P" in before) == protected
    assert store.rewrite_text(p, "a: 2\n")
    assert p.read_bytes() == b"a: 2\r\n"
    assert _sddl(p) == before


def test_a_rewrite_that_cannot_keep_the_access_is_refused(tmp_path, monkeypatch):
    p = tmp_path / "doc.yaml"
    p.write_bytes(b"a: 1\n")

    def cannot(source, target):
        raise PermissionError("not permitted")

    monkeypatch.setattr(contract, "_keep_access", cannot)
    with pytest.raises(contract.CannotKeep, match="doc.yaml"):
        store.rewrite_text(p, "a: 2\n")
    assert p.read_bytes() == b"a: 1\n"
    assert _leftovers(tmp_path, "doc.yaml") == []


def test_migrate_reports_a_refused_rewrite_as_a_migration_error(tmp_path, monkeypatch):
    template.stamp(tmp_path / "p", name="Probe")
    layout = Layout(tmp_path / "p")
    item = layout.inbox / "H-01.yaml"
    item.write_text(yaml.safe_dump({"id": "H-01", "allowed_responses": ["MORE_LIKE"]}), encoding="utf-8")
    old = item.read_bytes()

    def cannot(source, target):
        raise PermissionError("not permitted")

    monkeypatch.setattr(contract, "_keep_access", cannot)
    with pytest.raises(migrations.MigrationError, match="H-01.yaml"):
        migrations.run(layout)
    assert item.read_bytes() == old


def test_a_rewrite_through_a_symlink_replaces_the_target_and_keeps_the_link(tmp_path):
    target = tmp_path / "real.yaml"
    target.write_bytes(b"a: 1\n")
    link = tmp_path / "link.yaml"
    try:
        link.symlink_to(target)
    except OSError as exc:                                 # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")
    assert store.rewrite_text(link, "a: 2\n")
    assert link.is_symlink() and link.resolve() == target.resolve()
    assert target.read_bytes() == b"a: 2\n"


def test_a_rewrite_of_a_hard_linked_file_is_refused(tmp_path):
    p = tmp_path / "doc.yaml"
    p.write_bytes(b"a: 1\n")
    other = tmp_path / "other.yaml"
    os.link(p, other)
    with pytest.raises(contract.CannotKeep, match="link"):
        store.rewrite_text(p, "a: 2\n")
    assert p.read_bytes() == other.read_bytes() == b"a: 1\n"
    assert os.stat(p).st_nlink == 2
    assert _leftovers(tmp_path, "doc.yaml") == []
