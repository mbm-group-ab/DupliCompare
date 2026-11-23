"""
Safe file actions for duplicate groups: quarantine, permanent delete, move, rename.

Every action re-checks the file right before it acts: still there, still a
regular file (not a link), same size, mtime and file identity as when it was
scanned, and the same content (full hash). Actions that remove a copy
(quarantine, delete) also need another copy in the group that passes the same
check and is a different file on disk: the keeper. Anything that fails raises
ActionBlocked and changes nothing.

- Quarantine (the default "remove") renames the file into
  <scanned folder>/.duplicompare-quarantine/<session>/<relative path> on the
  same volume, so it is instant and can be restored.
- Permanent delete is a separate, explicit action.
- A move to another volume copies to a temporary file, verifies the copy's
  hash, renames it into place and only then removes the source (and only if
  the source is still unchanged). A failed copy never removes the source.
- Rename takes a file name only; moving to another folder is Move.

Every action is appended to a JSON-lines journal (~/.duplicompare/journal.jsonl).
"""

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime

from dc_engine import (QUARANTINE_DIR, READ_BUFFER_BYTES, Cancelled, file_digest,
                       same_state, stat_rec)

JOURNAL_PATH = os.path.join(os.path.expanduser("~"), ".duplicompare", "journal.jsonl")
PART_SUFFIX = ".duplicompare-part"
_WINDOWS_BAD = set('<>:"|?*')


class ActionBlocked(Exception):
    """An action was refused; `key` is a translation key, `params` its values."""

    def __init__(self, key, **params):
        super().__init__(key)
        self.key = key
        self.params = params
        self.target_invalid = key in {"blocked_missing", "blocked_changed",
                                      "blocked_content", "blocked_not_regular"}


def session_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ------------------------------------------------------------------ journal

def journal(entry, path=None):
    path = path or JOURNAL_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = dict(entry, time=datetime.now().isoformat(timespec="seconds"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_journal(path=None):
    path = path or JOURNAL_PATH
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def pending_quarantine(path=None):
    """Quarantine entries whose file is still in quarantine (not restored)."""
    entries = read_journal(path)
    restored = {e.get("from") for e in entries if e.get("action") == "restore"}
    return [e for e in entries if e.get("action") == "quarantine"
            and e.get("to") not in restored and os.path.exists(e.get("to", ""))]


# ------------------------------------------------------------------ checks

def check_member(group, path, cancel=None):
    """Re-check one file of the group against the scan; returns its current FileRec."""
    rec = group.rec(path)
    if rec is None:
        raise ActionBlocked("blocked_missing", path=path)
    try:
        now = stat_rec(path)
    except FileNotFoundError:
        raise ActionBlocked("blocked_missing", path=path)
    except OSError:
        if os.path.lexists(path):
            raise ActionBlocked("blocked_not_regular", path=path)
        raise ActionBlocked("blocked_missing", path=path)
    if not same_state(rec, now):
        raise ActionBlocked("blocked_changed", path=path)
    try:
        digest = file_digest(path, now.size, cancel=cancel)
    except OSError:
        raise ActionBlocked("blocked_unreadable", path=path)
    if digest != group.digest:
        raise ActionBlocked("blocked_content", path=path)
    return now


def find_keeper(group, target, target_now, cancel=None):
    """Another copy that still verifies and is a different file on disk."""
    for f in group.files:
        if f.path == target:
            continue
        try:
            now = check_member(group, f.path, cancel)
        except ActionBlocked:
            continue
        if target_now.identity is not None and now.identity == target_now.identity:
            continue
        return now
    raise ActionBlocked("blocked_no_keeper", path=target)


def _same_file(now, rec):
    return rec.size == now.size and (rec.identity is None or rec.identity == now.identity)


def validate_basename(name):
    """Raise ActionBlocked unless `name` is a plain file name."""
    bad = (not name or name in (".", "..") or os.path.isabs(name)
           or "/" in name or "\\" in name
           or (os.altsep and os.altsep in name) or "\0" in name)
    if sys.platform == "win32" and not bad:
        bad = (any(c in _WINDOWS_BAD or ord(c) < 32 for c in name)
               or name.endswith((" ", ".")))
    if bad:
        raise ActionBlocked("blocked_bad_name", name=name)


# ------------------------------------------------------------------ actions

def quarantine(group, path, root, session, cancel=None, journal_path=None):
    """Move one copy into the quarantine folder of its scanned root. Returns the new path."""
    target_now = check_member(group, path, cancel)
    keeper = find_keeper(group, path, target_now, cancel)
    root = os.path.abspath(root)
    rel = os.path.relpath(path, root)
    if rel.startswith(".."):
        raise ActionBlocked("blocked_outside_root", path=path)
    dest = os.path.join(root, QUARANTINE_DIR, session, rel)
    if os.path.lexists(dest):
        raise ActionBlocked("blocked_exists", path=dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        os.rename(path, dest)
    except OSError as exc:
        raise ActionBlocked("blocked_os", path=path, error=str(exc))
    # The path could have been swapped between the check and the rename.
    try:
        moved = stat_rec(dest)
    except OSError:
        moved = None
    if moved is None or not _same_file(moved, target_now):
        try:
            os.rename(dest, path)
        except OSError:
            pass
        raise ActionBlocked("blocked_replaced", path=path)
    journal({"action": "quarantine", "path": path, "to": dest, "size": target_now.size,
             "digest": group.digest.hex(), "keeper": keeper.path}, journal_path)
    return dest


def delete_permanently(group, path, cancel=None, journal_path=None):
    """Delete one copy for good (after the same checks as quarantine)."""
    target_now = check_member(group, path, cancel)
    keeper = find_keeper(group, path, target_now, cancel)
    tmp = path + ".duplicompare-del"
    if os.path.lexists(tmp):
        raise ActionBlocked("blocked_exists", path=tmp)
    try:
        os.rename(path, tmp)
    except OSError as exc:
        raise ActionBlocked("blocked_os", path=path, error=str(exc))
    try:
        moved = stat_rec(tmp)
    except OSError:
        moved = None
    if moved is None or not _same_file(moved, target_now):
        try:
            os.rename(tmp, path)
        except OSError:
            pass
        raise ActionBlocked("blocked_replaced", path=path)
    try:
        os.remove(tmp)
    except OSError as exc:
        try:
            os.rename(tmp, path)
        except OSError:
            pass
        raise ActionBlocked("blocked_os", path=path, error=str(exc))
    journal({"action": "delete", "path": path, "size": target_now.size,
             "digest": group.digest.hex(), "keeper": keeper.path}, journal_path)


def _copy_verified(src, dest_dir, name, expected_digest, cancel):
    """Copy src into dest_dir under a temporary name, hash it back from disk,
    and return the temp path. Removes the temp file on any failure."""
    tmp = os.path.join(dest_dir, "." + name + PART_SUFFIX)
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(src, "rb") as fin, open(tmp, "xb") as fout:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = fin.read(READ_BUFFER_BYTES)
                if not chunk:
                    break
                h.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        if h.digest() != expected_digest:
            raise ActionBlocked("blocked_content", path=src)
        if file_digest(tmp, os.path.getsize(tmp), cancel=cancel) != expected_digest:
            raise ActionBlocked("blocked_copy_verify", path=tmp)
        shutil.copystat(src, tmp)
        return tmp
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def move(group, path, dest_dir, cancel=None, journal_path=None):
    """Move one copy to dest_dir (same name). Returns the new path."""
    target_now = check_member(group, path, cancel)
    name = os.path.basename(path)
    dest_dir = os.path.abspath(dest_dir)
    new_path = os.path.join(dest_dir, name)
    if os.path.normcase(os.path.abspath(new_path)) == os.path.normcase(os.path.abspath(path)):
        return path
    if os.path.lexists(new_path):
        raise ActionBlocked("blocked_exists", path=new_path)
    try:
        same_volume = os.stat(dest_dir).st_dev == target_now.dev and target_now.dev != 0
    except OSError as exc:
        raise ActionBlocked("blocked_os", path=dest_dir, error=str(exc))

    if same_volume:
        try:
            os.rename(path, new_path)
        except OSError as exc:
            raise ActionBlocked("blocked_os", path=path, error=str(exc))
        mode = "rename"
    else:
        try:
            tmp = _copy_verified(path, dest_dir, name, group.digest, cancel)
        except OSError as exc:
            raise ActionBlocked("blocked_os", path=path, error=str(exc))
        if os.path.lexists(new_path):
            os.remove(tmp)
            raise ActionBlocked("blocked_exists", path=new_path)
        try:
            os.rename(tmp, new_path)
        except OSError as exc:
            os.remove(tmp)
            raise ActionBlocked("blocked_os", path=new_path, error=str(exc))
        # Only now remove the source, and only if nobody touched it meanwhile.
        try:
            still = stat_rec(path)
        except OSError:
            still = None
        if still is None or not same_state(target_now, still):
            journal({"action": "copy", "path": path, "to": new_path,
                     "size": target_now.size, "digest": group.digest.hex(),
                     "note": "source changed after copy; source kept"}, journal_path)
            raise ActionBlocked("blocked_source_changed", path=path, copy=new_path)
        try:
            os.remove(path)
        except OSError as exc:
            journal({"action": "copy", "path": path, "to": new_path,
                     "size": target_now.size, "digest": group.digest.hex(),
                     "note": "source could not be removed"}, journal_path)
            raise ActionBlocked("blocked_os", path=path, error=str(exc))
        mode = "copy+verify"
    journal({"action": "move", "path": path, "to": new_path, "size": target_now.size,
             "digest": group.digest.hex(), "mode": mode}, journal_path)
    return new_path


def rename(group, path, new_name, cancel=None, journal_path=None):
    """Rename one copy in place. Returns the new path."""
    validate_basename(new_name)
    check_member(group, path, cancel)
    directory = os.path.dirname(path)
    new_path = os.path.join(directory, new_name)
    if new_path == path:
        return path
    if os.path.lexists(new_path):
        try:
            case_only = os.path.samefile(path, new_path)
        except OSError:
            case_only = False
        if not case_only:
            raise ActionBlocked("blocked_exists", path=new_path)
    try:
        os.rename(path, new_path)
    except OSError as exc:
        raise ActionBlocked("blocked_os", path=path, error=str(exc))
    journal({"action": "rename", "path": path, "to": new_path,
             "digest": group.digest.hex()}, journal_path)
    return new_path


def restore(entry, journal_path=None):
    """Put a quarantined file back where it was. Returns the restored path."""
    src, dest = entry["to"], entry["path"]
    if os.path.lexists(dest):
        raise ActionBlocked("blocked_exists", path=dest)
    if not os.path.exists(src):
        raise ActionBlocked("blocked_missing", path=src)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        os.rename(src, dest)
    except OSError as exc:
        raise ActionBlocked("blocked_os", path=src, error=str(exc))
    journal({"action": "restore", "from": src, "path": dest}, journal_path)
    return dest
