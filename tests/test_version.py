"""The app's VERSION constant and the VERSION file (used by the release workflow) agree."""
import os
import re

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def test_version_matches():
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
        file_version = f.read().strip()
    with open(os.path.join(ROOT, "drive_compare.py"), encoding="utf-8") as f:
        code_version = re.search(r'^VERSION = "([^"]+)"', f.read(), re.M).group(1)
    assert file_version == code_version
