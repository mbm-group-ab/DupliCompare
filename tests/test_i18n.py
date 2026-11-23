"""Every language has the same keys and placeholders; every key the code uses exists."""
import os
import re
import string
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from dc_i18n import TEXTS  # noqa: E402


def fields(text):
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def test_same_keys_and_placeholders():
    en = TEXTS["en"]
    for lang, texts in TEXTS.items():
        assert set(texts) == set(en), (lang, set(en) ^ set(texts))
        for key, text in texts.items():
            assert fields(text) == fields(en[key]), (lang, key)


def _source(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def test_code_keys_exist():
    en = TEXTS["en"]
    used = set(re.findall(r'tr\("([a-z_]+)"\)?', _source("drive_compare.py")))
    used |= set(re.findall(r'_status\("[a-z]+", "([a-z_]+)"', _source("drive_compare.py")))
    used |= set(re.findall(r'"(progress_[a-z_]+)"', _source("dc_engine.py")))
    used |= set(re.findall(r'ActionBlocked\("([a-z_]+)"', _source("file_actions.py")))
    prefixes = {k for k in used if k.endswith("_")}
    missing = {k for k in used - prefixes if k not in en}
    assert not missing, missing
    for key in ("mode_quick", "mode_verified", "verdict_identical", "verdict_same_names_sizes",
                "verdict_different", "verdict_incomplete", "filter_all", "filter_archives",
                "filter_photos", "filter_documents", "show_all", "show_cross", "show_same"):
        assert key in en


def test_report_ui_keys():
    from report_writer import UI_EN
    for k in UI_EN:
        assert "rep_" + k in TEXTS["en"]
