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


def _user_sid() -> str:
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, encoding="utf-8",
                         errors="replace", check=True).stdout
    return out.strip().split(",")[-1].strip('"')


def _set_dacl(path, sddl):
    """Replace a file's or folder's whole DACL (protected: nothing inherited, nothing else kept) with `sddl`."""
    import ctypes
    from ctypes import wintypes
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    to_descriptor = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    to_descriptor.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                              ctypes.POINTER(wintypes.ULONG)]
    to_descriptor.restype = wintypes.BOOL
    set_security = advapi32.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    assert to_descriptor(sddl, 1, ctypes.byref(descriptor), None), ctypes.WinError(ctypes.get_last_error())
    try:
        assert set_security(str(path), 0x4 | 0x80000000, descriptor), ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.LocalFree(descriptor)


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


ACL_ACCESS, ACL_DEFAULT = "system.posix_acl_access", "system.posix_acl_default"
USER_OBJ, NAMED_USER, GROUP_OBJ, MASK, OTHER, NO_ID = 0x01, 0x02, 0x04, 0x10, 0x20, 0xFFFFFFFF


def _posix_acl(named_user_perm, other_perm=0):
    """A Linux POSIX ACL xattr value (version 2, then tag/perm/id entries in order) with one named user, uid 65534."""
    import struct
    entries = [(USER_OBJ, 6, NO_ID), (NAMED_USER, named_user_perm, 65534), (GROUP_OBJ, 4, NO_ID),
               (MASK, 4, NO_ID), (OTHER, other_perm, NO_ID)]
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)


def _access(path):
    """Everything that decides who can open the file on Linux: mode bits, owner, group and every extended attribute
    (POSIX ACLs live in system.posix_acl_access)."""
    st = os.stat(path)
    return (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid,
            {name: os.getxattr(path, name) for name in sorted(os.listxattr(path))})


@pytest.fixture
def linux_acls(tmp_path):
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux POSIX ACLs")
    probe = tmp_path / "acl-probe"
    probe.write_text("x", encoding="utf-8")
    try:
        os.setxattr(probe, ACL_ACCESS, _posix_acl(4))
    except OSError as exc:
        pytest.skip(f"this filesystem has no POSIX ACLs: {exc}")
    probe.unlink()


def test_a_directory_default_acl_does_not_reach_a_rewritten_file(tmp_path, linux_acls):
    """Review 1.10.1 F6, broadening: the directory grants uid 65534 read by default, the file itself does not (its
    access ACL removed, mode 0640). The temporary file inherited the default entry and chmod's mask made it effective."""
    shared = tmp_path / "shared"
    shared.mkdir()
    os.setxattr(shared, ACL_DEFAULT, _posix_acl(4))
    p = shared / "project.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    os.removexattr(p, ACL_ACCESS)
    os.chmod(p, 0o640)
    before = _access(p)
    assert ACL_ACCESS not in before[3]
    assert store.rewrite_text(p, "a: 2\n")
    assert _access(p) == before


def test_an_explicit_named_user_grant_survives_a_rewrite(tmp_path, linux_acls):
    """Review 1.10.1 F6, loss: a file's own access ACL (uid 65534 may read) was dropped by the replace."""
    p = tmp_path / "shared.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    os.setxattr(p, ACL_ACCESS, _posix_acl(4))
    before = _access(p)
    assert ACL_ACCESS in before[3]
    assert store.rewrite_text(p, "a: 2\n")
    assert _access(p) == before


def test_an_attribute_that_cannot_be_kept_refuses_the_rewrite(tmp_path, linux_acls, monkeypatch):
    p = tmp_path / "shared.yaml"
    p.write_bytes(b"a: 1\n")
    os.setxattr(p, ACL_ACCESS, _posix_acl(4))

    def refuse(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "setxattr", refuse)
    with pytest.raises(contract.CannotKeep, match="shared.yaml"):
        store.rewrite_text(p, "a: 2\n")
    assert p.read_bytes() == b"a: 1\n"
    assert _leftovers(tmp_path, "shared.yaml") == []


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


def test_the_temporary_file_is_private_while_it_holds_the_new_content(tmp_path, monkeypatch):
    """Review 1.10.1 F5: the new content sat in a temporary file with the directory's (wider) access until the old
    file's access was carried over, and stayed there if the process died first. It must be private from creation:
    on Windows a protected DACL naming only the owner, on POSIX no group or other permission bits."""
    import re
    p = tmp_path / "private.yaml"
    p.write_bytes(b"secret: old\n")
    if os.name == "nt":
        user = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
        subprocess.run(["icacls", str(p), "/inheritance:r", "/grant:r", f"{user}:F"], capture_output=True, check=True)
    seen = {}
    real = contract._keep_access

    def spy(source, target):                                # the new content is written, the old access not yet applied
        seen["content"] = open(target, "rb").read()
        seen["access"] = _sddl(target) if os.name == "nt" else stat.S_IMODE(os.stat(target).st_mode)
        return real(source, target)

    monkeypatch.setattr(contract, "_keep_access", spy)
    assert store.rewrite_text(p, "secret: new\n")
    assert seen["content"].replace(b"\r\n", b"\n") == b"secret: new\n"
    if os.name == "nt":
        owner = re.match(r"O:(.+?)G:", seen["access"]).group(1)
        dacl = seen["access"].split("D:", 1)[1]
        aces = re.findall(r"\(([AD]);[^;]*;[^;]*;[^;]*;[^;]*;([^)]+)\)", dacl)
        assert dacl.startswith("P"), seen["access"]                       # nothing inherited from the directory
        assert aces and {sid for _, sid in aces} <= {"OW", owner}, seen["access"]
    else:
        assert seen["access"] & 0o077 == 0, oct(seen["access"])


@windows_only
def test_a_file_that_may_not_be_deleted_is_refused_cleanly(tmp_path):
    """A file the user may read and write but not delete, in a folder that grants no delete-child: the in-place write
    of 1.10.0 could rewrite it, an atomic replace cannot. Before this fix the replace failed as a bare PermissionError
    and the temporary file stayed behind (the old DACL, copied onto it, did not allow deleting it either)."""
    # Review 1.10.1 F8: `icacls /grant:r <user>` replaced only the user's own grant; any other explicit grant (one a
    # runner gives to a group the account is in) survived and still allowed the delete, so the test failed in CI. The
    # whole DACL is now set, protected, with exactly one entry for this account, and checked before it is relied on.
    sid = _user_sid()
    folder = tmp_path / "nodelete"
    folder.mkdir()
    p = folder / "doc.yaml"
    p.write_bytes(b"a: 1\r\n")
    for q in (folder, p):                                       # a stray explicit grant the fixture must remove
        subprocess.run(["icacls", str(q), "/grant", "*S-1-5-32-545:F"], capture_output=True, check=True)
    folder_dacl = f"D:P(A;OICI;0x1301bf;;;{sid})"               # modify, without delete-child (0x40)
    file_dacl = f"D:P(A;;0x12019f;;;{sid})"                     # read and write, without delete
    try:
        _set_dacl(folder, folder_dacl)
        _set_dacl(p, file_dacl)
        assert _sddl(folder).endswith(folder_dacl) and _sddl(p).endswith(file_dacl)
        before = _sddl(p)
        with pytest.raises(contract.CannotKeep, match="nothing was replaced"):
            store.rewrite_text(p, "a: 2\n")
        assert p.read_bytes() == b"a: 1\r\n" and _sddl(p) == before
        assert _leftovers(folder, "doc.yaml") == []
    finally:                                                     # let pytest remove its temporary tree
        for q in [folder, *folder.iterdir()]:
            _set_dacl(q, f"D:P(A;OICI;FA;;;{sid})" if q == folder else f"D:P(A;;FA;;;{sid})")


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
