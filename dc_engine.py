"""
DupliCompare engine: scanning, folder comparison and duplicate finding.

No Tk in here, so it can be tested on generated files. Every long call takes
`progress` (callable receiving (key, params) for the UI to translate) and
`cancel` (threading.Event or None); a cancelled run raises Cancelled.

Results always carry a ScanReport: which folders/files could not be read, how
many files changed while they were being read, and whether the result is
complete. A result that is not complete never claims "identical".
"""

import hashlib
import os
import stat
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

PARTIAL_HASH_BYTES = 64 * 1024       # head + tail of a file, cheap pre-filter
READ_BUFFER_BYTES = 4 * 1024 * 1024  # full-hash read size
DEFAULT_WORKERS = 4                  # fine for HDD and SSD; raise for fast SSD/NVMe
MAX_ERRORS_KEPT = 1000               # errors listed in reports; the count is always exact
QUARANTINE_DIR = ".duplicompare-quarantine"

FILE_TYPE_FILTERS = {
    "all": None,
    "archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
    "photos": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".tiff", ".raw"},
    "documents": {".txt", ".doc", ".docx", ".pdf", ".rtf", ".odt", ".md", ".csv", ".xls", ".xlsx"},
}


class Cancelled(Exception):
    pass


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _say(progress, key, **params):
    if progress:
        progress(key, params)


# ------------------------------------------------------------------ report

@dataclass
class ScanReport:
    errors: list = field(default_factory=list)   # (path, message), at most MAX_ERRORS_KEPT
    error_count: int = 0
    roots_failed: list = field(default_factory=list)
    unreadable: int = 0          # files that could not be hashed
    changed: int = 0             # files whose size/mtime changed while being read
    dirs_scanned: int = 0
    files_seen: int = 0

    def add_error(self, path, exc):
        self.error_count += 1
        if len(self.errors) < MAX_ERRORS_KEPT:
            msg = getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__
            self.errors.append((str(path), msg))

    @property
    def complete(self):
        return not self.roots_failed and self.error_count == 0 and \
            self.unreadable == 0 and self.changed == 0

    def coverage(self):
        """Coverage block of the shared storage report contract (mbm.storage.report/1).

        Files in a folder that could not be listed are never seen, so `not_checked`
        counts failed roots and listing errors only as a lower bound (said in the
        report's limitations)."""
        unsure = self.unreadable + self.changed
        return {
            "total": self.files_seen,
            "checked": max(0, self.files_seen - unsure),
            "errors": unsure,
            "not_checked": len(self.roots_failed) + self.error_count,
            "complete": self.complete,
            "dirs_scanned": self.dirs_scanned,
        }

    def merge(self, other):
        for p, m in other.errors:
            if len(self.errors) < MAX_ERRORS_KEPT:
                self.errors.append((p, m))
        self.error_count += other.error_count
        self.roots_failed += other.roots_failed
        self.unreadable += other.unreadable
        self.changed += other.changed
        self.dirs_scanned += other.dirs_scanned
        self.files_seen += other.files_seen


# ------------------------------------------------------------------ scanning

def scan_tree(root, extensions=None, min_size=0, report=None, cancel=None):
    """Yield (full_path, size) for every regular file under root.

    Uses os.scandir so sizes come with the directory listing (no extra system
    call per file on Windows). Symlinks/junctions are not followed and the
    app's own quarantine folders are skipped. Unreadable folders and entries
    are recorded in `report` instead of being dropped silently.
    """
    report = report if report is not None else ScanReport()
    try:
        if not os.path.isdir(root):
            raise FileNotFoundError(2, "Folder not found or not reachable", root)
        os.scandir(root).close()
    except OSError as exc:
        report.roots_failed.append(root)
        report.add_error(root, exc)
        return
    stack = [root]
    n = 0
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError as exc:
            report.add_error(current, exc)
            continue
        report.dirs_scanned += 1
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError as exc:  # listing broke off half way
                    report.add_error(current, exc)
                    break
                n += 1
                if n % 2000 == 0:
                    _check(cancel)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name != QUARANTINE_DIR:
                            stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        report.files_seen += 1
                        if extensions is not None and \
                                os.path.splitext(entry.name)[1].lower() not in extensions:
                            continue
                        size = entry.stat(follow_symlinks=False).st_size
                        if size >= min_size:
                            yield entry.path, size
                except OSError as exc:
                    report.add_error(entry.path, exc)


def build_file_map(root, report=None, cancel=None):
    """Return dict: relative_path ('/'-separated) -> size for every file under root."""
    root = os.path.abspath(root)
    prefix = root if root.endswith(os.sep) else root + os.sep
    cut = len(prefix)
    return {
        full[cut:].replace("\\", "/"): size
        for full, size in scan_tree(root, report=report, cancel=cancel)
    }


def human_size(num_bytes):
    if num_bytes is None or num_bytes < 0:
        return "N/A"
    step = 1024.0
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num_bytes < step:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= step
    return f"{num_bytes:.2f} PB"


# ------------------------------------------------------------------- hashing

def file_digest(path, size, partial=False, cancel=None):
    """Content digest of a file. partial=True hashes only the first and last
    PARTIAL_HASH_BYTES (a cheap pre-filter); otherwise the whole file. For a
    file of at most PARTIAL_HASH_BYTES both give the same digest."""
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb", buffering=0) as f:
        if partial:
            h.update(f.read(PARTIAL_HASH_BYTES))
            if size > PARTIAL_HASH_BYTES:
                f.seek(max(size - PARTIAL_HASH_BYTES, PARTIAL_HASH_BYTES))
                h.update(f.read(PARTIAL_HASH_BYTES))
        else:
            buf = bytearray(READ_BUFFER_BYTES)
            view = memoryview(buf)
            while True:
                _check(cancel)
                n = f.readinto(buf)
                if not n:
                    break
                h.update(view[:n])
    return h.digest()


@dataclass
class FileRec:
    """One file as it was when scanned. dev/ino are 0 where the OS has none."""
    path: str
    size: int
    mtime_ns: int
    dev: int
    ino: int
    nlink: int = 1

    @property
    def identity(self):
        return (self.dev, self.ino) if self.ino else None


def stat_rec(path):
    """FileRec for a regular, non-link file; raises OSError otherwise."""
    st = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or is_reparse(st):
        raise OSError(f"not a regular file: {path}")
    return FileRec(path, st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino, st.st_nlink)


def is_reparse(st):
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def same_state(a, b):
    return a.size == b.size and a.mtime_ns == b.mtime_ns and \
        (a.identity is None or b.identity is None or a.identity == b.identity)


def _hash_pool(items, fn, workers, cancel, progress, key):
    """Run fn(item) for each item in a pool; returns results in order.
    Reports progress as (done, total, bytes_done, bytes_total)."""
    total = len(items)
    bytes_total = sum(max(getattr(i, "size", 0), 0) for i in items)
    out = []
    done_bytes = 0

    def work(item):
        if cancel is not None and cancel.is_set():
            return None
        return fn(item)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, res in enumerate(pool.map(work, items), start=1):
            _check(cancel)
            out.append(res)
            done_bytes += max(getattr(items[i - 1], "size", 0), 0)
            if i % 50 == 0 or i == total:
                _say(progress, key, done=i, total=total,
                     done_size=human_size(done_bytes), total_size=human_size(bytes_total))
    return out


# ------------------------------------------------------------------- compare

def _dir_set(file_map):
    """Every directory (relative, '/'-separated) that contains a file."""
    dirs = set()
    for rel in file_map:
        d = rel.rpartition("/")[0]
        while d and d not in dirs:
            dirs.add(d)
            d = d.rpartition("/")[0]
    return dirs


def _collapse_one_sided(only_files, other_dirs):
    """Split files that exist only on one side into
    - whole folders missing on the other side: {folder: [count, size]}
    - loose files inside folders that exist on both sides: [(rel, size)]
    """
    folders = {}
    loose = []
    top_cache = {}
    for rel, size in only_files:
        parent = rel.rpartition("/")[0]
        if parent not in top_cache:
            top = None
            acc = ""
            for part in parent.split("/") if parent else []:
                acc = f"{acc}/{part}" if acc else part
                if acc not in other_dirs:
                    top = acc
                    break
            top_cache[parent] = top
        top = top_cache[parent]
        if top is None:
            loose.append((rel, size))
        else:
            entry = folders.setdefault(top, [0, 0])
            entry[0] += 1
            entry[1] += max(size, 0)
    return folders, loose


MODE_QUICK = "quick"        # names and sizes only
MODE_VERIFIED = "verified"  # also reads and hashes every same-size pair


def _verify_pair(pair, cancel):
    """Hash both files of a same-path, same-size pair.
    Returns 'same' | 'different' | 'unreadable' | 'changed'."""
    a_path, b_path = pair.a, pair.b
    try:
        before = (stat_rec(a_path), stat_rec(b_path))
    except OSError:
        return "unreadable"
    try:
        da = file_digest(a_path, before[0].size, cancel=cancel)
        db = file_digest(b_path, before[1].size, cancel=cancel)
    except Cancelled:
        return None
    except OSError:
        return "unreadable"
    try:
        after = (stat_rec(a_path), stat_rec(b_path))
    except OSError:
        return "changed"
    if not (same_state(before[0], after[0]) and same_state(before[1], after[1])):
        return "changed"
    return "same" if da == db else "different"


@dataclass
class _Pair:
    rel: str
    a: str
    b: str
    size: int


def compare_folders(path_a, path_b, mode=MODE_QUICK, progress=None, cancel=None,
                    workers=DEFAULT_WORKERS):
    """Compare two folders by relative path and size; in verified mode also by content.

    Returns (summary, result, report). summary["verdict"] is one of
    'identical' (verified mode, complete, nothing differs), 'same_names_sizes'
    (quick mode, nothing differs by name/size), 'different', or 'incomplete'.
    """
    report = ScanReport()
    _say(progress, "progress_scanning", path=path_a)
    map_a = build_file_map(path_a, report, cancel)
    _say(progress, "progress_scanning", path=path_b)
    map_b = build_file_map(path_b, report, cancel)
    _say(progress, "progress_comparing")

    only_a = [(k, map_a[k]) for k in map_a if k not in map_b]
    only_b = [(k, map_b[k]) for k in map_b if k not in map_a]
    mismatch = sorted(
        (k, map_a[k], map_b[k]) for k in map_a
        if k in map_b and map_a[k] != map_b[k]
    )
    same_size = sorted(k for k in map_a if k in map_b and map_a[k] == map_b[k])

    content = {"same": [], "different": [], "unreadable": [], "changed": []}
    unverified = []
    if mode == MODE_VERIFIED and same_size:
        ra, rb = os.path.abspath(path_a), os.path.abspath(path_b)
        pairs = [_Pair(k, os.path.join(ra, *k.split("/")), os.path.join(rb, *k.split("/")),
                       map_a[k] * 2) for k in same_size]
        results = _hash_pool(pairs, lambda p: _verify_pair(p, cancel), workers, cancel,
                             progress, "progress_verifying")
        for pair, res in zip(pairs, results):
            if res is None:
                raise Cancelled()
            content[res].append((pair.rel, map_a[pair.rel]))
        report.unreadable += len(content["unreadable"])
        report.changed += len(content["changed"])
        for rel, _s in content["unreadable"]:
            report.add_error(rel, OSError("could not read one side to compare contents"))
    else:
        unverified = [(k, map_a[k]) for k in same_size]

    folders_a, loose_a = _collapse_one_sided(only_a, _dir_set(map_b))
    folders_b, loose_b = _collapse_one_sided(only_b, _dir_set(map_a))

    result = {
        "folders_only_a": sorted(
            ((k, v[0], v[1]) for k, v in folders_a.items()), key=lambda r: -r[2]),
        "folders_only_b": sorted(
            ((k, v[0], v[1]) for k, v in folders_b.items()), key=lambda r: -r[2]),
        "files_only_a": sorted(loose_a),
        "files_only_b": sorted(loose_b),
        "mismatch": mismatch,
        "content_different": content["different"],
        "content_unreadable": content["unreadable"],
        "content_changed": content["changed"],
    }
    diff_count = len(only_a) + len(only_b) + len(mismatch) + len(content["different"])
    if not report.complete:
        verdict = "incomplete"
    elif diff_count:
        verdict = "different"
    elif mode == MODE_VERIFIED:
        verdict = "identical"
    else:
        verdict = "same_names_sizes"
    summary = {
        "mode": mode,
        "a_files": len(map_a),
        "b_files": len(map_b),
        "a_size": sum(v for v in map_a.values() if v > 0),
        "b_size": sum(v for v in map_b.values() if v > 0),
        "only_a": len(only_a),
        "only_b": len(only_b),
        "only_a_size": sum(max(s, 0) for _k, s in only_a),
        "only_b_size": sum(max(s, 0) for _k, s in only_b),
        "mismatch": len(mismatch),
        "verified_same": len(content["same"]),
        "content_different": len(content["different"]),
        "unreadable": len(content["unreadable"]),
        "changed": len(content["changed"]),
        "unverified": len(unverified),
        "diff_count": diff_count,
        "complete": report.complete,
        "error_count": report.error_count,
        "verdict": verdict,
    }
    return summary, result, report


# --------------------------------------------------------------- duplicates

@dataclass
class DupGroup:
    """Distinct files with identical size and content (hardlinks collapsed)."""
    size: int
    digest: bytes
    files: list          # list[FileRec], each a different file on disk
    links: dict = field(default_factory=dict)   # path -> [other paths that are the same file]

    @property
    def paths(self):
        return [f.path for f in self.files]

    def rec(self, path):
        for f in self.files:
            if f.path == path:
                return f
        return None


@dataclass
class DupResult:
    groups: list         # list[DupGroup], biggest first
    hardlinks: list      # list[list[str]]: paths that are one file (no space to reclaim)
    report: ScanReport


def find_duplicates(roots, progress=None, extensions=None, min_size_bytes=0,
                    workers=DEFAULT_WORKERS, cancel=None):
    """Scan one or more folders and return a DupResult.

    Size first, then head+tail hash, then a full hash of what still collides.
    Files that change while being read are left out and counted. Paths that are
    hardlinks of one file are not duplicates (deleting one frees nothing): they
    are listed in `hardlinks` and collapsed inside groups.
    """
    if isinstance(roots, str):
        roots = [roots]
    roots = sorted({os.path.abspath(r) for r in roots}, key=len, reverse=True)
    report = ScanReport()

    seen = set()
    by_size = defaultdict(list)
    for root in roots:
        _say(progress, "progress_scanning", path=root)
        for path, size in scan_tree(root, extensions, max(min_size_bytes, 1), report, cancel):
            key = os.path.normcase(path)
            if key in seen:
                continue
            seen.add(key)
            by_size[size].append(path)
    seen.clear()

    # Only same-size files can be duplicates; stat just those for identity.
    candidates = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        for p in paths:
            try:
                candidates.append(stat_rec(p))
            except OSError as exc:
                report.add_error(p, exc)
                report.unreadable += 1
    by_size.clear()

    # Collapse hardlinks: one representative per (dev, ino).
    by_ident = defaultdict(list)
    singles = []
    for rec in candidates:
        if rec.identity is not None and rec.nlink > 1:
            by_ident[rec.identity].append(rec)
        else:
            singles.append(rec)
    hardlinks = []
    link_map = {}
    for recs in by_ident.values():
        recs.sort(key=lambda r: r.path)
        singles.append(recs[0])
        if len(recs) > 1:
            hardlinks.append([r.path for r in recs])
            link_map[recs[0].path] = [r.path for r in recs[1:]]
    sizes = defaultdict(list)
    for rec in singles:
        sizes[rec.size].append(rec)
    candidates = [r for recs in sizes.values() if len(recs) > 1 for r in recs]

    def digest_of(partial):
        def fn(rec):
            try:
                return file_digest(rec.path, rec.size, partial, cancel)
            except Cancelled:
                return None
            except OSError as exc:
                return exc
        return fn

    # Pass 1: head + tail hash.
    digests = _hash_pool(candidates, digest_of(True), workers, cancel, progress,
                         "progress_quick_hash")
    by_partial = defaultdict(list)
    for rec, d in zip(candidates, digests):
        if isinstance(d, bytes):
            by_partial[(rec.size, d)].append(rec)
        elif isinstance(d, OSError):
            report.add_error(rec.path, d)
            report.unreadable += 1

    final = []        # (size, digest, [recs])
    need_full = []
    for (size, d), recs in by_partial.items():
        if len(recs) < 2:
            continue
        if size <= PARTIAL_HASH_BYTES:  # the partial hash covered the whole file
            final.append((size, d, recs))
        else:
            need_full.extend(recs)

    # Pass 2: full hash only for files that still collide.
    digests = _hash_pool(need_full, digest_of(False), workers, cancel, progress,
                         "progress_full_hash")
    by_full = defaultdict(list)
    for rec, d in zip(need_full, digests):
        if isinstance(d, bytes):
            by_full[(rec.size, d)].append(rec)
        elif isinstance(d, OSError):
            report.add_error(rec.path, d)
            report.unreadable += 1
    for (size, d), recs in by_full.items():
        if len(recs) > 1:
            final.append((size, d, recs))

    # Drop files that changed while we read them.
    groups = []
    for size, d, recs in final:
        kept = []
        for rec in recs:
            try:
                now = stat_rec(rec.path)
            except OSError as exc:
                report.add_error(rec.path, exc)
                report.changed += 1
                continue
            if same_state(rec, now):
                kept.append(rec)
            else:
                report.changed += 1
                report.add_error(rec.path, OSError("changed while it was being read"))
        if len(kept) > 1:
            kept.sort(key=lambda r: r.path)
            links = {r.path: link_map[r.path] for r in kept if r.path in link_map}
            groups.append(DupGroup(size, d, kept, links))

    groups.sort(key=lambda g: (-g.size, g.files[0].path))
    hardlinks.sort()
    return DupResult(groups, hardlinks, report)


class RootMap:
    """Maps a file path to the scanned folder it belongs to ([1], [2], ...)."""

    def __init__(self, roots):
        self.roots = [os.path.abspath(r) for r in roots]
        self._keys = [os.path.normcase(r).rstrip("\\/") + os.sep for r in self.roots]
        self._order = sorted(range(len(self.roots)), key=lambda i: -len(self._keys[i]))

    def locate(self, path):
        """Return (root_index, path relative to that root); (-1, path) if none."""
        p = os.path.normcase(path)
        for i in self._order:
            if p.startswith(self._keys[i]):
                return i, path[len(self._keys[i]):]
        return -1, path

    @staticmethod
    def tag(index):
        return f"[{index + 1}]" if index >= 0 else "[?]"

    def describe(self, path):
        i, rel = self.locate(path)
        return f"{self.tag(i)} {rel}"

    def root_of(self, path):
        i, _rel = self.locate(path)
        return self.roots[i] if i >= 0 else None

    def root_indexes(self, paths):
        return sorted({self.locate(p)[0] for p in paths})

    def where(self, paths):
        return ", ".join(self.tag(i) for i in self.root_indexes(paths))

    def is_cross(self, paths):
        return len(self.root_indexes(paths)) > 1
