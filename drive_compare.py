"""
Drive/Folder Compare Tool
Cross-platform GUI (Tkinter) app to:
  - compare two folders (recursively) and write an indexed, searchable HTML
    report (whole folders missing on one side are collapsed to one row), and
  - find duplicate files across any number of folders and say which folders
    each duplicate lives in.
"""

import os
import sys
import threading
import webbrowser
import hashlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import shutil

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import ai_assist
from report_writer import write_report


FILE_TYPE_FILTERS = {
    "All files": None,
    "Archives (.zip, .rar, .7z, .tar, .gz)": {
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"
    },
    "Photos (.jpg, .png, .gif, ...)": {
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".tiff", ".raw"
    },
    "Text/Documents (.txt, .doc, .pdf, ...)": {
        ".txt", ".doc", ".docx", ".pdf", ".rtf", ".odt", ".md", ".csv", ".xls", ".xlsx"
    },
}

PARTIAL_HASH_BYTES = 64 * 1024      # head + tail of a file, cheap pre-filter
READ_BUFFER_BYTES = 4 * 1024 * 1024  # full-hash read size
DEFAULT_WORKERS = 4                  # fine for HDD and SSD; raise for fast SSD/NVMe


# ------------------------------------------------------------------ scanning

def scan_tree(root, extensions=None, min_size=0):
    """Yield (full_path, size) for every regular file under root.

    Uses os.scandir so file sizes come with the directory listing (on Windows
    no extra system call per file). Symlinks are not followed.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        if extensions is not None and \
                                os.path.splitext(entry.name)[1].lower() not in extensions:
                            continue
                        size = entry.stat(follow_symlinks=False).st_size
                        if size >= min_size:
                            yield entry.path, size
                except OSError:
                    continue


def build_file_map(root):
    """Return dict: relative_path -> size_in_bytes for every file under root."""
    root = os.path.abspath(root)
    prefix = root if root.endswith(os.sep) else root + os.sep
    cut = len(prefix)
    return {
        full[cut:].replace("\\", "/"): size
        for full, size in scan_tree(root)
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


def compare_folders(path_a, path_b, progress_cb=None):
    if progress_cb:
        progress_cb(f"Scanning: {path_a}")
    map_a = build_file_map(path_a)
    if progress_cb:
        progress_cb(f"Scanning: {path_b}")
    map_b = build_file_map(path_b)
    if progress_cb:
        progress_cb("Comparing...")

    only_a = [(k, map_a[k]) for k in map_a if k not in map_b]
    only_b = [(k, map_b[k]) for k in map_b if k not in map_a]
    mismatch = sorted(
        (k, map_a[k], map_b[k]) for k in map_a
        if k in map_b and map_a[k] != map_b[k]
    )

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
    }
    summary = {
        "a_files": len(map_a),
        "b_files": len(map_b),
        "a_size": sum(v for v in map_a.values() if v > 0),
        "b_size": sum(v for v in map_b.values() if v > 0),
        "only_a": len(only_a),
        "only_b": len(only_b),
        "only_a_size": sum(max(s, 0) for _k, s in only_a),
        "only_b_size": sum(max(s, 0) for _k, s in only_b),
        "mismatch": len(mismatch),
        "diff_count": len(only_a) + len(only_b) + len(mismatch),
    }
    return summary, result


def write_compare_report(path_a, path_b, summary, result, out_dir):
    """Write the compare report folder; returns the index.html path."""
    info = [("Folder A:", path_a), ("Folder B:", path_b)]
    cards = [
        ("Files in A", f"{summary['a_files']:,}"),
        ("Files in B", f"{summary['b_files']:,}"),
        ("Size of A", human_size(summary["a_size"])),
        ("Size of B", human_size(summary["b_size"])),
        ("Only in A", f"{summary['only_a']:,} files ({human_size(summary['only_a_size'])})"),
        ("Only in B", f"{summary['only_b']:,} files ({human_size(summary['only_b_size'])})"),
        ("Different size", f"{summary['mismatch']:,}"),
    ]
    folder_cols = [("path", "Folder", "text"), ("files", "Files", "num"), ("size", "Size", "size")]
    file_cols = [("path", "File", "text"), ("size", "Size", "size")]
    tabs = [
        {
            "id": "a-folders", "label": "Folders only in A",
            "note": "Whole folders that exist in A and have no files at all in B (biggest first).",
            "cols": folder_cols, "rows": [list(r) for r in result["folders_only_a"]],
        },
        {
            "id": "b-folders", "label": "Folders only in B",
            "note": "Whole folders that exist in B and have no files at all in A (biggest first).",
            "cols": folder_cols, "rows": [list(r) for r in result["folders_only_b"]],
        },
        {
            "id": "a-files", "label": "Files missing in B",
            "note": "Files in A that B lacks, inside folders that exist on both sides.",
            "cols": file_cols, "rows": [list(r) for r in result["files_only_a"]],
        },
        {
            "id": "b-files", "label": "Files missing in A",
            "note": "Files in B that A lacks, inside folders that exist on both sides.",
            "cols": file_cols, "rows": [list(r) for r in result["files_only_b"]],
        },
        {
            "id": "size", "label": "Different size",
            "note": "Same relative path on both sides but the size differs.",
            "cols": [("path", "File", "text"), ("a", "Size in A", "size"), ("b", "Size in B", "size")],
            "rows": [list(r) for r in result["mismatch"]],
        },
    ]
    return write_report(out_dir, "Folder Comparison Report", info, cards, tabs)


# --------------------------------------------------------------- duplicates

def file_digest(path, size, partial=False):
    """Content digest of a file. partial=True hashes only the first and last
    PARTIAL_HASH_BYTES (a cheap pre-filter); otherwise the whole file."""
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
                n = f.readinto(buf)
                if not n:
                    break
                h.update(view[:n])
    return h.digest()


def _hash_many(items, workers, partial, progress_cb, label):
    """Digest (or None on read error) for each (path, size) item, in order."""
    def work(item):
        try:
            return file_digest(item[0], item[1], partial)
        except OSError:
            return None

    out = []
    total = len(items)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, digest in enumerate(pool.map(work, items), start=1):
            out.append(digest)
            if progress_cb and i % 200 == 0:
                progress_cb(f"{label} ({i}/{total})")
    return out


def find_duplicates(roots, progress_cb=None, extensions=None, min_size_bytes=0,
                    max_workers=DEFAULT_WORKERS):
    """Scan one or more folders and return a list of duplicate groups.

    Each group is a list of full file paths with identical size and content
    (across ALL the folders together). Empty files are ignored. If one folder
    is inside another, each file is still counted once.
    """
    if isinstance(roots, str):
        roots = [roots]
    roots = sorted({os.path.abspath(r) for r in roots}, key=len, reverse=True)

    seen = set()
    by_size = defaultdict(list)
    for root in roots:
        if progress_cb:
            progress_cb(f"Scanning: {root}")
        for path, size in scan_tree(root, extensions, max(min_size_bytes, 1)):
            key = os.path.normcase(path)
            if key in seen:
                continue
            seen.add(key)
            by_size[size].append(path)
    seen.clear()

    # Only same-size files can possibly be duplicates.
    candidates = [(p, size) for size, paths in by_size.items() if len(paths) > 1 for p in paths]

    # Pass 1: head + tail hash.
    digests = _hash_many(candidates, max_workers, True, progress_cb, "Quick-hashing files...")
    by_partial = defaultdict(list)
    for item, digest in zip(candidates, digests):
        if digest is not None:
            by_partial[(item[1], digest)].append(item)

    final = []  # (size, [paths])
    need_full = []
    for (size, _d), items in by_partial.items():
        if len(items) < 2:
            continue
        if size <= PARTIAL_HASH_BYTES:  # the partial hash already covered the whole file
            final.append((size, [p for p, _s in items]))
        else:
            need_full.extend(items)

    # Pass 2: full hash only for files that still collide.
    digests = _hash_many(need_full, max_workers, False, progress_cb, "Hashing files...")
    by_full = defaultdict(list)
    for item, digest in zip(need_full, digests):
        if digest is not None:
            by_full[(item[1], digest)].append(item[0])
    for (size, _d), paths in by_full.items():
        if len(paths) > 1:
            final.append((size, paths))

    final.sort(key=lambda g: -g[0])
    return [paths for _size, paths in final]


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

    def root_indexes(self, paths):
        return sorted({self.locate(p)[0] for p in paths})

    def where(self, paths):
        return ", ".join(self.tag(i) for i in self.root_indexes(paths))

    def is_cross(self, paths):
        return len(self.root_indexes(paths)) > 1


def write_duplicates_report(rootmap, groups, out_dir):
    """Write the duplicates report folder; returns the index.html path."""
    rows_cross, rows_same = [], []
    total_wasted = 0
    for paths in groups:
        try:
            size = os.path.getsize(paths[0])
        except OSError:
            size = -1
        wasted = max(size, 0) * (len(paths) - 1)
        total_wasted += wasted
        row = [len(paths), size, wasted, rootmap.where(paths),
               [rootmap.describe(p) for p in paths]]
        (rows_cross if rootmap.is_cross(paths) else rows_same).append(row)

    def finish(rows):
        rows.sort(key=lambda r: -r[2])
        for n, r in enumerate(rows, start=1):
            r.insert(0, n)
        return rows

    cols = [("n", "#", "num"), ("copies", "Copies", "num"), ("size", "Size each", "size"),
            ("wasted", "Wasted", "size"), ("where", "In folders", "text"),
            ("files", "Files", "lines")]
    info = [("Folders:", "")] + [
        (RootMap.tag(i), p) for i, p in enumerate(rootmap.roots)
    ]
    cards = [
        ("Duplicate groups", f"{len(groups):,}"),
        ("Across different folders", f"{len(rows_cross):,}"),
        ("Inside one folder", f"{len(rows_same):,}"),
        ("Wasted space (extra copies)", human_size(total_wasted)),
    ]
    tabs = [
        {
            "id": "across", "label": "Across different folders",
            "note": "The same file exists in two or more of the scanned folders. "
                    "'In folders' says which ones ([n] = the folder list above).",
            "cols": cols, "rows": finish(rows_cross),
        },
        {
            "id": "inside", "label": "Inside one folder",
            "note": "All copies of the file are inside the same scanned folder.",
            "cols": cols, "rows": finish(rows_same),
        },
    ]
    return write_report(out_dir, "Duplicate Files Report", info, cards, tabs)


def _default_out_dir(prefix):
    base = os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(base):
        base = os.getcwd()
    return os.path.join(base, f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")


def _open_in_browser(path):
    webbrowser.open(Path(os.path.abspath(path)).as_uri())


# ----------------------------------------------------------------------- GUI

SHOW_ALL = "All groups"
SHOW_CROSS = "Across different folders"
SHOW_SAME = "Inside one folder"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DupliCompare")
        self.geometry("1020x640")
        self.minsize(900, 520)
        try:
            self.iconbitmap(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.ico"))
        except tk.TclError:
            pass

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        self.compare_tab = ttk.Frame(notebook)
        self.dup_tab = ttk.Frame(notebook)
        notebook.add(self.compare_tab, text="Compare")
        notebook.add(self.dup_tab, text="Duplicates")

        self._build_compare_tab()
        self._build_duplicates_tab()

    def _error(self, text):
        self.after(0, lambda: messagebox.showerror("Error", text))

    # ---------------------------------------------------------------- Compare

    def _build_compare_tab(self):
        self.path_a = tk.StringVar()
        self.path_b = tk.StringVar()
        self.status = tk.StringVar(value="Ready.")
        self.last_compare = None  # (a, b, summary, result) of the last run

        pad = {"padx": 10, "pady": 6}
        frm = ttk.Frame(self.compare_tab)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Folder A:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.path_a, width=60).grid(row=0, column=1, **pad)
        ttk.Button(frm, text="Browse...", command=self.browse_a).grid(row=0, column=2, **pad)

        ttk.Label(frm, text="Folder B:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.path_b, width=60).grid(row=1, column=1, **pad)
        ttk.Button(frm, text="Browse...", command=self.browse_b).grid(row=1, column=2, **pad)

        self.compare_btn = ttk.Button(frm, text="Compare", command=self.start_compare)
        self.compare_btn.grid(row=2, column=1, pady=20)
        ttk.Button(frm, text="AI summary / advice", command=self.ask_ai_compare).grid(
            row=2, column=2, pady=20)

        ttk.Label(frm, textvariable=self.status, foreground="#555").grid(
            row=3, column=0, columnspan=3, sticky="w", **pad
        )

        self.progress = ttk.Progressbar(frm, mode="indeterminate", length=500)
        self.progress.grid(row=4, column=0, columnspan=3, padx=10, pady=6)

    def browse_a(self):
        d = filedialog.askdirectory(title="Select Folder A")
        if d:
            self.path_a.set(d)

    def browse_b(self):
        d = filedialog.askdirectory(title="Select Folder B")
        if d:
            self.path_b.set(d)

    def start_compare(self):
        a, b = self.path_a.get().strip(), self.path_b.get().strip()
        if not a or not b:
            messagebox.showwarning("Missing input", "Please select both folders.")
            return
        if not os.path.isdir(a):
            messagebox.showerror("Invalid path", f"Folder A does not exist:\n{a}")
            return
        if not os.path.isdir(b):
            messagebox.showerror("Invalid path", f"Folder B does not exist:\n{b}")
            return

        self.compare_btn.config(state="disabled")
        self.progress.start(10)
        thread = threading.Thread(target=self.run_compare, args=(a, b), daemon=True)
        thread.start()

    def run_compare(self, a, b):
        def progress_cb(msg):
            self.status.set(msg)

        try:
            summary, result = compare_folders(a, b, progress_cb)
            self.last_compare = (a, b, summary, result)
            self.status.set("Writing report...")
            out_dir = _default_out_dir("compare-report")
            index = write_compare_report(a, b, summary, result, out_dir)
            self.status.set(f"Done. {summary['diff_count']} difference(s) found. Report: {out_dir}")
            _open_in_browser(index)
        except Exception as exc:  # noqa: BLE001
            self._error(str(exc))
            self.status.set("Error occurred.")
        finally:
            self.progress.stop()
            self.compare_btn.config(state="normal")

    # ------------------------------------------------------------- Duplicates

    def _build_duplicates_tab(self):
        self.dup_status = tk.StringVar(value="Ready.")
        self.dup_groups = []        # list[list[str]] kept in sync with tree rows
        self.dup_rootmap = None
        self.dup_where = {}         # group index -> "[1], [3]"
        self.dup_cross = {}         # group index -> bool

        pad = {"padx": 10, "pady": 4}

        # --- folder list
        folders = ttk.Frame(self.dup_tab)
        folders.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(folders, text="Folders to scan\n(any number):").grid(row=0, column=0, sticky="nw")
        self.dup_list = tk.Listbox(folders, height=4, selectmode="extended", activestyle="none")
        self.dup_list.grid(row=0, column=1, sticky="we", padx=8)
        lsb = ttk.Scrollbar(folders, orient="vertical", command=self.dup_list.yview)
        self.dup_list.configure(yscrollcommand=lsb.set)
        lsb.grid(row=0, column=2, sticky="ns")
        btns = ttk.Frame(folders)
        btns.grid(row=0, column=3, sticky="n", padx=(8, 0))
        ttk.Button(btns, text="Add folder...", command=self.add_dup_folder).pack(fill="x", pady=1)
        ttk.Button(btns, text="Remove selected", command=self.remove_dup_folders).pack(fill="x", pady=1)
        ttk.Button(btns, text="Clear", command=lambda: self.dup_list.delete(0, "end")).pack(fill="x", pady=1)
        folders.columnconfigure(1, weight=1)

        # --- options
        top = ttk.Frame(self.dup_tab)
        top.pack(fill="x")

        ttk.Label(top, text="Type:").grid(row=0, column=0, sticky="w", **pad)
        self.dup_filter = tk.StringVar(value="All files")
        ttk.Combobox(
            top, textvariable=self.dup_filter, values=list(FILE_TYPE_FILTERS.keys()),
            state="readonly", width=30,
        ).grid(row=0, column=1, **pad)

        ttk.Label(top, text="Threads:").grid(row=0, column=2, sticky="e", **pad)
        self.dup_threads = tk.IntVar(value=DEFAULT_WORKERS)
        ttk.Spinbox(top, from_=1, to=32, width=4, textvariable=self.dup_threads).grid(
            row=0, column=3, **pad)
        ttk.Label(top, text="(HDD: 2-4, SSD: 8+)", foreground="#777").grid(row=0, column=4, sticky="w")

        self.scan_btn = ttk.Button(top, text="Scan for Duplicates", command=self.start_duplicates)
        self.scan_btn.grid(row=0, column=5, **pad)

        ttk.Label(top, text="Min file size:").grid(row=1, column=0, sticky="w", **pad)
        self.min_size_mb = tk.DoubleVar(value=0)
        self.min_size_label = tk.StringVar(value="No limit")
        ttk.Scale(
            top, from_=0, to=1024, orient="horizontal", variable=self.min_size_mb,
            command=self._on_min_size_change, length=300,
        ).grid(row=1, column=1, sticky="we", **pad)
        ttk.Label(top, textvariable=self.min_size_label, width=14).grid(
            row=1, column=2, columnspan=2, sticky="w", **pad
        )

        ttk.Label(top, text="Show:").grid(row=1, column=4, sticky="e", **pad)
        self.dup_view = tk.StringVar(value=SHOW_ALL)
        show = ttk.Combobox(
            top, textvariable=self.dup_view, values=[SHOW_ALL, SHOW_CROSS, SHOW_SAME],
            state="readonly", width=24,
        )
        show.grid(row=1, column=5, **pad)
        show.bind("<<ComboboxSelected>>", lambda _e: self._populate_dup_tree())

        self.dup_progress = ttk.Progressbar(self.dup_tab, mode="indeterminate", length=500)
        self.dup_progress.pack(fill="x", padx=10, pady=(0, 6))

        ttk.Label(self.dup_tab, textvariable=self.dup_status, foreground="#555").pack(
            fill="x", padx=10
        )

        list_frame = ttk.Frame(self.dup_tab)
        list_frame.pack(fill="both", expand=True, padx=10, pady=10)

        columns = ("group", "copies", "size", "where", "name")
        self.dup_tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.dup_sort_state = {"col": None, "reverse": False}
        self._col_labels = {
            "group": "#", "copies": "Copies", "size": "Size (each)",
            "where": "In folders", "name": "File name",
        }
        for c in columns:
            self.dup_tree.heading(c, text=self._col_labels[c], command=lambda c=c: self.sort_dup_tree(c))
        self.dup_tree.column("group", width=50, anchor="center")
        self.dup_tree.column("copies", width=70, anchor="center")
        self.dup_tree.column("size", width=100, anchor="center")
        self.dup_tree.column("where", width=130, anchor="center")
        self.dup_tree.column("name", width=420, anchor="w")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.dup_tree.yview)
        self.dup_tree.configure(yscrollcommand=vsb.set)
        self.dup_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.dup_tree.bind("<Double-1>", lambda e: self.open_selected_group())

        bottom = ttk.Frame(self.dup_tab)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bottom, text="Open Group...", command=self.open_selected_group).pack(
            side="left", padx=4
        )
        ttk.Button(bottom, text="Export HTML Report", command=self.export_dup_report).pack(
            side="left", padx=4
        )
        ttk.Button(bottom, text="AI summary / advice", command=self.ask_ai_duplicates).pack(
            side="left", padx=4
        )

    def add_dup_folder(self):
        d = filedialog.askdirectory(title="Add a folder to scan for duplicates")
        if not d:
            return
        d = os.path.abspath(d)
        existing = {os.path.normcase(p) for p in self.dup_list.get(0, "end")}
        if os.path.normcase(d) not in existing:
            self.dup_list.insert("end", d)

    def remove_dup_folders(self):
        for i in reversed(self.dup_list.curselection()):
            self.dup_list.delete(i)

    def _on_min_size_change(self, _value=None):
        mb = self.min_size_mb.get()
        self.min_size_label.set("No limit" if mb < 1 else f"≥ {mb:.0f} MB")

    def start_duplicates(self):
        roots = list(self.dup_list.get(0, "end"))
        if not roots:
            messagebox.showwarning("Missing input", "Add at least one folder.")
            return
        for r in roots:
            if not os.path.isdir(r):
                messagebox.showerror("Invalid path", f"Folder does not exist:\n{r}")
                return

        try:
            workers = max(1, min(32, int(self.dup_threads.get())))
        except (tk.TclError, ValueError):
            workers = DEFAULT_WORKERS

        self.scan_btn.config(state="disabled")
        self.dup_progress.start(10)
        extensions = FILE_TYPE_FILTERS.get(self.dup_filter.get())
        min_size_bytes = int(self.min_size_mb.get() * 1024 * 1024)
        thread = threading.Thread(
            target=self.run_duplicates, args=(roots, extensions, min_size_bytes, workers), daemon=True
        )
        thread.start()

    def run_duplicates(self, roots, extensions, min_size_bytes, workers):
        def progress_cb(msg):
            self.dup_status.set(msg)

        try:
            groups = find_duplicates(
                roots, progress_cb, extensions=extensions,
                min_size_bytes=min_size_bytes, max_workers=workers,
            )
            rootmap = RootMap(roots)
            self.after(0, lambda: self._set_dup_result(groups, rootmap))
        except Exception as exc:  # noqa: BLE001
            self._error(str(exc))
            self.dup_status.set("Error occurred.")
        finally:
            self.dup_progress.stop()
            self.scan_btn.config(state="normal")

    def _set_dup_result(self, groups, rootmap):
        self.dup_groups = groups
        self.dup_rootmap = rootmap
        self.dup_where = {i: rootmap.where(p) for i, p in enumerate(groups)}
        self.dup_cross = {i: rootmap.is_cross(p) for i, p in enumerate(groups)}
        self._populate_dup_tree()

    def _visible_indexes(self):
        mode = self.dup_view.get()
        out = []
        for idx, paths in enumerate(self.dup_groups):
            if not paths:
                continue
            if mode == SHOW_CROSS and not self.dup_cross.get(idx):
                continue
            if mode == SHOW_SAME and self.dup_cross.get(idx):
                continue
            out.append(idx)
        return out

    def _row_values(self, idx):
        paths = self.dup_groups[idx]
        try:
            size = os.path.getsize(paths[0])
        except OSError:
            size = -1
        return (idx + 1, len(paths), human_size(size), self.dup_where.get(idx, ""),
                os.path.basename(paths[0]))

    def _populate_dup_tree(self, chunk_size=200):
        """Insert rows in chunks via `after` so the UI stays responsive and
        the whole (potentially huge) result set still gets loaded fully."""
        self.dup_tree.delete(*self.dup_tree.get_children())
        self.dup_sort_state = {"col": None, "reverse": False}
        for c, label in self._col_labels.items():
            self.dup_tree.heading(c, text=label)
        indexes = self._visible_indexes()
        total = len(indexes)
        token = object()
        self._populate_token = token

        def load_chunk(start):
            if self._populate_token is not token:
                return  # a newer populate started
            end = min(start + chunk_size, total)
            for idx in indexes[start:end]:
                self.dup_tree.insert("", "end", iid=str(idx), values=self._row_values(idx))
            if end < total:
                self.dup_status.set(f"Loading duplicate groups... {end}/{total}")
                self.after(1, lambda: load_chunk(end))
            else:
                self.dup_status.set(f"Done. {total} duplicate group(s) shown "
                                    f"({len(self.dup_groups)} found).")

        if total:
            load_chunk(0)
        else:
            self.dup_status.set("Done. No duplicate groups to show.")

    def sort_dup_tree(self, col):
        state = self.dup_sort_state
        reverse = not state["reverse"] if state["col"] == col else False
        state["col"], state["reverse"] = col, reverse

        def sort_key(idx):
            paths = self.dup_groups[idx]
            if not paths:
                return 0
            if col == "group":
                return idx
            if col == "copies":
                return len(paths)
            if col == "size":
                try:
                    return os.path.getsize(paths[0])
                except OSError:
                    return -1
            if col == "where":
                return self.dup_where.get(idx, "")
            if col == "name":
                return os.path.basename(paths[0]).lower()
            return idx

        ids = list(self.dup_tree.get_children(""))
        ids.sort(key=lambda iid: sort_key(int(iid)), reverse=reverse)
        for pos, iid in enumerate(ids):
            self.dup_tree.move(iid, "", pos)

        for c, label in self._col_labels.items():
            if c == col:
                label += " ▼" if reverse else " ▲"
            self.dup_tree.heading(c, text=label)

    # ------------------------------------------------------------------ AI

    def ask_ai_compare(self):
        if not self.last_compare:
            messagebox.showinfo("AI help", "Run a compare first.")
            return
        a, b, summary, result = self.last_compare
        self._open_ai("compare", lambda: ai_assist.build_compare_digest(a, b, summary, result))

    def ask_ai_duplicates(self):
        groups = [g for g in self.dup_groups if g]
        if not self.dup_rootmap or not groups:
            messagebox.showinfo("AI help", "Run a scan that finds duplicates first.")
            return
        rootmap = self.dup_rootmap
        self._open_ai("duplicates", lambda: ai_assist.build_duplicates_digest(rootmap, groups))

    def _open_ai(self, kind, make_digest):
        cfg = ai_assist.load_config()
        if not cfg:
            messagebox.showinfo("AI help", ai_assist.SETUP_HELP)
            return
        AiWindow(self, kind, make_digest(), cfg)

    def describe_path(self, path):
        return self.dup_rootmap.describe(path) if self.dup_rootmap else path

    def open_selected_group(self):
        sel = self.dup_tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a duplicate group first.")
            return
        idx = int(sel[0])
        DuplicateGroupWindow(self, idx, self.dup_groups[idx], self.on_group_changed)

    def on_group_changed(self, idx, new_paths):
        """Called by DuplicateGroupWindow after a file was deleted/renamed/moved."""
        if len(new_paths) < 2:
            self.dup_groups[idx] = []
            if self.dup_tree.exists(str(idx)):
                self.dup_tree.delete(str(idx))
            return
        self.dup_groups[idx] = new_paths
        self.dup_where[idx] = self.dup_rootmap.where(new_paths)
        self.dup_cross[idx] = self.dup_rootmap.is_cross(new_paths)
        if self.dup_tree.exists(str(idx)):
            self.dup_tree.item(str(idx), values=self._row_values(idx))

    def export_dup_report(self):
        groups = [g for g in self.dup_groups if g]
        if not self.dup_rootmap:
            messagebox.showinfo("Nothing to export", "Run a scan first.")
            return
        out_dir = _default_out_dir("duplicates-report")
        index = write_duplicates_report(self.dup_rootmap, groups, out_dir)
        self.dup_status.set(f"Report saved: {out_dir}")
        _open_in_browser(index)


class AiWindow(tk.Toplevel):
    """Shows exactly what will be sent to the AI, sends it on request, shows the answer."""

    def __init__(self, parent, kind, digest, cfg):
        super().__init__(parent)
        self.kind, self.digest, self.cfg = kind, digest, cfg
        self.title("AI summary / advice")
        self.geometry("820x700")

        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="Answer language:").pack(side="left")
        self.lang = tk.StringVar(value="English")
        ttk.Combobox(top, textvariable=self.lang, values=list(ai_assist.LANGUAGES),
                     state="readonly", width=12).pack(side="left", padx=6)
        self.send_btn = ttk.Button(top, text="Send to AI", command=self.send)
        self.send_btn.pack(side="left", padx=6)
        self.status = tk.StringVar(value="Only this summary is sent: names, sizes and counts, "
                                         "never file contents. Private mode: local model only.")
        ttk.Label(self, textvariable=self.status, foreground="#555", wraplength=780).pack(
            fill="x", padx=10)

        ttk.Label(self, text="What will be sent:").pack(anchor="w", padx=10, pady=(8, 0))
        sent = tk.Text(self, height=11, wrap="word")
        sent.insert("1.0", digest)
        sent.config(state="disabled")
        sent.pack(fill="x", padx=10)

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(fill="x", padx=10, pady=6)

        ttk.Label(self, text="AI answer:").pack(anchor="w", padx=10)
        self.answer = tk.Text(self, wrap="word")
        self.answer.pack(fill="both", expand=True, padx=10, pady=(0, 4))

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bottom, text="Copy", command=self.copy).pack(side="left", padx=4)
        ttk.Button(bottom, text="Save as .txt...", command=self.save).pack(side="left", padx=4)

    def send(self):
        self.send_btn.config(state="disabled")
        self.progress.start(10)
        self.status.set("Waiting for the AI (the local model can take a minute or two)...")
        system, prompt = ai_assist.build_prompt(self.kind, self.digest, self.lang.get())

        def work():
            try:
                text = ai_assist.ask_ai(system, prompt, self.cfg)
                self.after(0, lambda: self._done(text, None))
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda: self._done(None, err))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, text, error):
        self.progress.stop()
        self.send_btn.config(state="normal")
        if error:
            self.status.set(error)
            return
        self.status.set("Done. The AI only advises; check before you delete or move anything.")
        self.answer.delete("1.0", "end")
        self.answer.insert("1.0", text)

    def copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.answer.get("1.0", "end").strip())

    def save(self):
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".txt", filetypes=[("Text", "*.txt")],
            initialfile=f"ai-advice-{self.kind}.txt")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.answer.get("1.0", "end").strip() + "\n")


class DuplicateGroupWindow(tk.Toplevel):
    """Shows every copy in a duplicate group side by side, each with its own
    Delete / Rename / Move controls."""

    def __init__(self, parent, group_idx, paths, on_changed):
        super().__init__(parent)
        self.parent = parent
        self.group_idx = group_idx
        self.paths = list(paths)
        self.on_changed = on_changed

        self.title(f"Duplicate group #{group_idx + 1} ({len(self.paths)} copies)")
        self.geometry("900x400")

        self.canvas_frame = ttk.Frame(self)
        self.canvas_frame.pack(fill="both", expand=True, padx=10, pady=10)

        self._render()

    def _render(self):
        for child in self.canvas_frame.winfo_children():
            child.destroy()

        if len(self.paths) < 2:
            ttk.Label(
                self.canvas_frame,
                text="Only one copy left in this group.",
                foreground="#2e7d32",
            ).pack(padx=10, pady=10)
            return

        canvas = tk.Canvas(self.canvas_frame, highlightthickness=0)
        hsb = ttk.Scrollbar(self.canvas_frame, orient="horizontal", command=canvas.xview)
        inner = ttk.Frame(canvas)
        inner.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(xscrollcommand=hsb.set)
        canvas.pack(side="top", fill="both", expand=True)
        hsb.pack(side="bottom", fill="x")

        for col, path in enumerate(self.paths):
            self._build_column(inner, col, path)

    def _build_column(self, parent, col, path):
        card = ttk.Frame(parent, borderwidth=1, relief="solid", padding=10)
        card.grid(row=0, column=col, padx=6, pady=6, sticky="n")

        size = human_size(os.path.getsize(path)) if os.path.exists(path) else "N/A"

        ttk.Label(card, text=f"Copy {col + 1}", font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        path_lbl = tk.Text(card, width=32, height=4, wrap="char", borderwidth=0)
        path_lbl.insert("1.0", self.parent.describe_path(path))
        path_lbl.config(state="disabled")
        path_lbl.pack(pady=(4, 4))
        ttk.Label(card, text=f"Size: {size}", foreground="#555").pack(anchor="w")

        btns = ttk.Frame(card)
        btns.pack(pady=(8, 0))
        ttk.Button(btns, text="Delete", command=lambda p=path: self._delete(p)).grid(
            row=0, column=0, padx=3
        )
        ttk.Button(btns, text="Rename", command=lambda p=path: self._rename(p)).grid(
            row=0, column=1, padx=3
        )
        ttk.Button(btns, text="Move", command=lambda p=path: self._move(p)).grid(
            row=0, column=2, padx=3
        )

    def _refresh_and_notify(self):
        self.on_changed(self.group_idx, self.paths)
        self._render()

    def _delete(self, path):
        if not messagebox.askyesno(
            "Confirm delete", f"Delete this file?\n\n{path}", parent=self
        ):
            return
        try:
            os.remove(path)
        except OSError as exc:
            messagebox.showerror("Error", str(exc), parent=self)
            return
        self.paths = [p for p in self.paths if p != path]
        self._refresh_and_notify()

    def _rename(self, path):
        directory, old_name = os.path.split(path)
        new_name = simpledialog.askstring(
            "Rename file", "New file name:", initialvalue=old_name, parent=self
        )
        if not new_name or new_name == old_name:
            return
        new_path = os.path.join(directory, new_name)
        if os.path.exists(new_path):
            messagebox.showerror("Error", "A file with that name already exists.", parent=self)
            return
        try:
            os.rename(path, new_path)
        except OSError as exc:
            messagebox.showerror("Error", str(exc), parent=self)
            return
        self.paths = [new_path if p == path else p for p in self.paths]
        self._refresh_and_notify()

    def _move(self, path):
        dest_dir = filedialog.askdirectory(title="Move file to...", parent=self)
        if not dest_dir:
            return
        new_path = os.path.join(dest_dir, os.path.basename(path))
        if os.path.abspath(new_path) == os.path.abspath(path):
            return
        if os.path.exists(new_path):
            messagebox.showerror("Error", "A file with that name already exists there.", parent=self)
            return
        try:
            shutil.move(path, new_path)
        except OSError as exc:
            messagebox.showerror("Error", str(exc), parent=self)
            return
        self.paths = [new_path if p == path else p for p in self.paths]
        self._refresh_and_notify()


if __name__ == "__main__":
    App().mainloop()
