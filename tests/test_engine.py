"""Engine tests on generated files only (tmp_path)."""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import dc_engine as eng  # noqa: E402


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def test_quick_compare_names_sizes_only(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(str(a / "same.txt"), b"AAAA")
    write(str(b / "same.txt"), b"BBBB")
    summary, _res, report = eng.compare_folders(str(a), str(b))
    assert summary["diff_count"] == 0
    assert summary["unverified"] == 1
    assert summary["verdict"] == "same_names_sizes"   # never "identical" in quick mode
    assert report.complete


def test_verified_compare_finds_content_difference(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(str(a / "same.txt"), b"AAAA")
    write(str(b / "same.txt"), b"BBBB")
    write(str(a / "sub" / "ok.bin"), b"x" * 200_000)
    write(str(b / "sub" / "ok.bin"), b"x" * 200_000)
    summary, res, _report = eng.compare_folders(str(a), str(b), mode=eng.MODE_VERIFIED)
    assert summary["content_different"] == 1
    assert summary["verified_same"] == 1
    assert summary["verdict"] == "different"
    assert res["content_different"] == [("same.txt", 4)]


def test_verified_compare_identical(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        write(str(root / "x" / "1.txt"), b"one")
        write(str(root / "2.txt"), b"two")
    summary, _res, _rep = eng.compare_folders(str(a), str(b), mode=eng.MODE_VERIFIED)
    assert summary["verdict"] == "identical"
    assert summary["verified_same"] == 2


def test_missing_root_marks_incomplete(tmp_path):
    a = tmp_path / "a"
    write(str(a / "f.txt"), b"1")
    summary, _res, report = eng.compare_folders(str(a), str(tmp_path / "gone"),
                                                mode=eng.MODE_VERIFIED)
    assert summary["verdict"] == "incomplete"
    assert report.roots_failed and not report.complete


def test_scan_records_unreadable_dir(tmp_path, monkeypatch):
    root = tmp_path / "r"
    write(str(root / "ok.txt"), b"1")
    (root / "locked").mkdir()
    real = os.scandir

    def fake(path):
        if str(path).endswith("locked"):
            raise PermissionError(13, "Access is denied", str(path))
        return real(path)

    monkeypatch.setattr(eng.os, "scandir", fake)
    report = eng.ScanReport()
    files = list(eng.scan_tree(str(root), report=report))
    assert len(files) == 1
    assert report.error_count == 1 and not report.complete
    assert report.errors[0][0].endswith("locked")


def test_scan_skips_quarantine(tmp_path):
    root = tmp_path / "r"
    write(str(root / "a.txt"), b"1")
    write(str(root / eng.QUARANTINE_DIR / "s" / "a.txt"), b"1")
    assert len(list(eng.scan_tree(str(root)))) == 1


def test_duplicates_basic_and_partial_collision(tmp_path):
    big = os.urandom(300_000)
    write(str(tmp_path / "r1" / "a.bin"), big)
    write(str(tmp_path / "r2" / "b.bin"), big)
    # same size, same head and tail, different middle: not a duplicate
    other = bytearray(big)
    other[150_000] ^= 0xFF
    write(str(tmp_path / "r2" / "c.bin"), bytes(other))
    write(str(tmp_path / "r1" / "unique.txt"), b"u")
    res = eng.find_duplicates([str(tmp_path / "r1"), str(tmp_path / "r2")])
    assert len(res.groups) == 1
    g = res.groups[0]
    assert sorted(os.path.basename(p) for p in g.paths) == ["a.bin", "b.bin"]
    assert g.size == 300_000 and len(g.digest) == 16
    assert res.report.complete


def test_overlapping_roots_count_once(tmp_path):
    write(str(tmp_path / "r" / "a.txt"), b"same")
    write(str(tmp_path / "r" / "sub" / "b.txt"), b"same")
    res = eng.find_duplicates([str(tmp_path / "r"), str(tmp_path / "r" / "sub")])
    assert len(res.groups) == 1 and len(res.groups[0].files) == 2


def test_hardlinks_are_not_duplicates(tmp_path):
    a = tmp_path / "r" / "a.bin"
    write(str(a), b"z" * 1000)
    link = tmp_path / "r" / "a-link.bin"
    try:
        os.link(str(a), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks not supported here")
    res = eng.find_duplicates([str(tmp_path / "r")])
    assert res.groups == []
    assert len(res.hardlinks) == 1 and len(res.hardlinks[0]) == 2

    write(str(tmp_path / "r" / "copy.bin"), b"z" * 1000)
    res = eng.find_duplicates([str(tmp_path / "r")])
    assert len(res.groups) == 1 and len(res.groups[0].files) == 2   # link collapsed
    assert res.groups[0].links


def test_cancel(tmp_path):
    for i in range(5):
        write(str(tmp_path / "r" / f"{i}.txt"), b"same")
    ev = threading.Event()
    ev.set()
    with pytest.raises(eng.Cancelled):
        eng.find_duplicates([str(tmp_path / "r")], cancel=ev)
