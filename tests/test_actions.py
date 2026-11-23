"""File action tests on generated files only: every refusal must leave the files untouched."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import dc_engine as eng  # noqa: E402
import file_actions as fa  # noqa: E402


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def read(path):
    with open(path, "rb") as f:
        return f.read()


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "root"
    a, b = str(root / "a" / "f.bin"), str(root / "b" / "f.bin")
    data = os.urandom(100_000)
    write(a, data)
    write(b, data)
    res = eng.find_duplicates([str(root)])
    assert len(res.groups) == 1
    return {"root": str(root), "a": a, "b": b, "data": data, "group": res.groups[0],
            "journal": str(tmp_path / "journal.jsonl"), "tmp": tmp_path}


def bump_mtime(path):
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_quarantine_and_restore(setup):
    s = setup
    dest = fa.quarantine(s["group"], s["a"], s["root"], "sess", journal_path=s["journal"])
    assert not os.path.exists(s["a"]) and read(dest) == s["data"]
    assert eng.QUARANTINE_DIR in dest
    pend = fa.pending_quarantine(s["journal"])
    assert len(pend) == 1
    fa.restore(pend[0], journal_path=s["journal"])
    assert read(s["a"]) == s["data"]
    assert fa.pending_quarantine(s["journal"]) == []


def test_changed_target_blocks(setup):
    s = setup
    write(s["a"], bytes(len(s["data"])))          # same size, different content
    with pytest.raises(fa.ActionBlocked) as e:
        fa.quarantine(s["group"], s["a"], s["root"], "sess", journal_path=s["journal"])
    assert e.value.target_invalid
    assert os.path.exists(s["a"]) and read(s["b"]) == s["data"]


def test_same_mtime_content_swap_blocks(setup):
    s = setup
    st = os.stat(s["a"])
    write(s["a"], bytes(len(s["data"])))
    os.utime(s["a"], ns=(st.st_atime_ns, st.st_mtime_ns))
    group = s["group"]
    rec = group.rec(s["a"])
    rec.ino = os.stat(s["a"]).st_ino or rec.ino   # rewrite in place keeps identity
    with pytest.raises(fa.ActionBlocked) as e:
        fa.delete_permanently(group, s["a"], journal_path=s["journal"])
    assert e.value.key == "blocked_content"
    assert os.path.exists(s["a"])


def test_changed_keeper_blocks(setup):
    s = setup
    write(s["b"], bytes(len(s["data"])))          # the only other copy no longer matches
    with pytest.raises(fa.ActionBlocked) as e:
        fa.delete_permanently(s["group"], s["a"], journal_path=s["journal"])
    assert e.value.key == "blocked_no_keeper"
    assert read(s["a"]) == s["data"]


def test_removed_keeper_blocks(setup):
    s = setup
    os.remove(s["b"])
    with pytest.raises(fa.ActionBlocked):
        fa.quarantine(s["group"], s["a"], s["root"], "sess", journal_path=s["journal"])
    assert read(s["a"]) == s["data"]


def test_touched_mtime_blocks(setup):
    s = setup
    bump_mtime(s["a"])
    with pytest.raises(fa.ActionBlocked) as e:
        fa.delete_permanently(s["group"], s["a"], journal_path=s["journal"])
    assert e.value.key == "blocked_changed"


def test_permanent_delete(setup):
    s = setup
    fa.delete_permanently(s["group"], s["a"], journal_path=s["journal"])
    assert not os.path.exists(s["a"]) and read(s["b"]) == s["data"]
    assert fa.read_journal(s["journal"])[-1]["action"] == "delete"


def test_rename_basename_only(setup):
    s = setup
    for bad in ("../escape.bin", "sub/x.bin", os.path.abspath("x.bin"), "", ".."):
        with pytest.raises(fa.ActionBlocked):
            fa.rename(s["group"], s["a"], bad, journal_path=s["journal"])
    assert os.path.exists(s["a"])
    new = fa.rename(s["group"], s["a"], "g.bin", journal_path=s["journal"])
    assert os.path.dirname(new) == os.path.dirname(s["a"]) and read(new) == s["data"]


def test_rename_refuses_existing(setup):
    s = setup
    write(os.path.join(os.path.dirname(s["a"]), "taken.bin"), b"x")
    with pytest.raises(fa.ActionBlocked) as e:
        fa.rename(s["group"], s["a"], "taken.bin", journal_path=s["journal"])
    assert e.value.key == "blocked_exists"


def test_move_same_volume(setup):
    s = setup
    dest = s["tmp"] / "dest"
    dest.mkdir()
    new = fa.move(s["group"], s["a"], str(dest), journal_path=s["journal"])
    assert read(new) == s["data"] and not os.path.exists(s["a"])


def test_cross_volume_move_verifies_then_removes(setup, monkeypatch):
    s = setup
    dest = s["tmp"] / "dest"
    dest.mkdir()
    real_stat = os.stat

    class Fake:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return 999 if name == "st_dev" else getattr(self._st, name)

    monkeypatch.setattr(fa.os, "stat",
                        lambda p, *a, **k: Fake(real_stat(p, *a, **k))
                        if os.path.abspath(p) == str(dest) else real_stat(p, *a, **k))
    new = fa.move(s["group"], s["a"], str(dest), journal_path=s["journal"])
    assert read(new) == s["data"] and not os.path.exists(s["a"])
    assert fa.read_journal(s["journal"])[-1]["mode"] == "copy+verify"
    assert not [n for n in os.listdir(dest) if n.endswith(fa.PART_SUFFIX)]


def test_failed_copy_keeps_source(setup, monkeypatch):
    s = setup
    dest = s["tmp"] / "dest"
    dest.mkdir()
    monkeypatch.setattr(fa, "_copy_verified",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left")))
    real_stat = os.stat

    class Fake:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return 999 if name == "st_dev" else getattr(self._st, name)

    monkeypatch.setattr(fa.os, "stat",
                        lambda p, *a, **k: Fake(real_stat(p, *a, **k))
                        if os.path.abspath(p) == str(dest) else real_stat(p, *a, **k))
    with pytest.raises(fa.ActionBlocked):
        fa.move(s["group"], s["a"], str(dest), journal_path=s["journal"])
    assert read(s["a"]) == s["data"]
    assert os.listdir(dest) == []


def test_corrupt_copy_detected(setup, monkeypatch):
    s = setup
    dest = s["tmp"] / "dest"
    dest.mkdir()
    real_digest = fa.file_digest

    def bad_digest(path, size, partial=False, cancel=None):
        if path.endswith(fa.PART_SUFFIX):
            return b"\0" * 16
        return real_digest(path, size, partial, cancel)

    monkeypatch.setattr(fa, "file_digest", bad_digest)
    with pytest.raises(fa.ActionBlocked) as e:
        fa._copy_verified(s["a"], str(dest), "f.bin", s["group"].digest, None)
    assert e.value.key == "blocked_copy_verify"
    assert os.listdir(dest) == [] and read(s["a"]) == s["data"]
