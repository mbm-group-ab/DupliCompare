"""report.json follows the shared storage report contract. Generated files only."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import dc_engine as eng  # noqa: E402
import drive_compare as dc  # noqa: E402


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def load(out_dir):
    with open(os.path.join(out_dir, "report.json"), encoding="utf-8") as f:
        return json.load(f)


def check_envelope(doc):
    assert doc["contract"] == "mbm.storage.report/1"
    assert doc["tool"] == "duplicompare" and doc["version"] == dc.VERSION
    assert doc["generated"].endswith("Z") and doc["mode"] == "full"
    assert doc["limitations"]
    assert set(doc["coverage"]) >= {"total", "checked", "errors", "not_checked", "complete"}


def test_compare_report_json(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(str(a / "same.txt"), b"AAAA")
    write(str(b / "same.txt"), b"BBBB")
    write(str(a / "only.txt"), b"x")
    summary, result, report = eng.compare_folders(str(a), str(b))
    out = tmp_path / "rep"
    dc.write_compare_report(str(a), str(b), summary, result, report, str(out), dc.translator("en"))
    doc = load(out)
    check_envelope(doc)
    assert doc["scope"] == {"kind": "compare", "roots": [str(a), str(b)]}
    assert doc["coverage"]["complete"] is True and doc["coverage"]["total"] == 3
    assert doc["summary"]["verdict"] == summary["verdict"]
    assert doc["counts"]["a-files"] == 1
    assert any("names and sizes" in s for s in doc["limitations"])


def test_incomplete_scan_is_not_complete(tmp_path):
    a = tmp_path / "a"
    write(str(a / "f.txt"), b"1")
    summary, result, report = eng.compare_folders(str(a), str(tmp_path / "missing"))
    out = tmp_path / "rep"
    dc.write_compare_report(str(a), str(tmp_path / "missing"), summary, result, report,
                            str(out), dc.translator("en"))
    cov = load(out)["coverage"]
    assert cov["complete"] is False and cov["not_checked"] >= 1


def test_duplicates_report_json(tmp_path):
    root = tmp_path / "r"
    write(str(root / "x" / "one.bin"), b"same bytes")
    write(str(root / "y" / "two.bin"), b"same bytes")
    res = eng.find_duplicates([str(root)])
    out = tmp_path / "rep"
    dc.write_duplicates_report(eng.RootMap([str(root)]), res.groups, res.hardlinks, res.report,
                               str(out), dc.translator("en"))
    doc = load(out)
    check_envelope(doc)
    assert doc["scope"]["kind"] == "duplicates"
    assert doc["summary"]["groups"] == 1 and doc["summary"]["wasted_bytes"] == len(b"same bytes")
