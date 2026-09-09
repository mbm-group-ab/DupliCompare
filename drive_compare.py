"""
Drive/Folder Compare Tool
Cross-platform GUI (Tkinter) app to compare two folders (recursively) and
produce an HTML report showing missing, extra, and mismatched files.
"""

import os
import sys
import threading
import webbrowser
import html
import hashlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import shutil

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk


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


def build_file_map(root):
    """Return dict: relative_path -> size_in_bytes for every file under root."""
    file_map = {}
    root = os.path.abspath(root)
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                size = -1
            file_map[rel] = size
    return file_map


def human_size(num_bytes):
    if num_bytes < 0:
        return "N/A"
    step = 1024.0
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num_bytes < step:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= step
    return f"{num_bytes:.2f} PB"


def compare_folders(path_a, path_b, progress_cb=None):
    if progress_cb:
        progress_cb(f"Scanning: {path_a}")
    map_a = build_file_map(path_a)
    if progress_cb:
        progress_cb(f"Scanning: {path_b}")
    map_b = build_file_map(path_b)

    all_keys = set(map_a.keys()) | set(map_b.keys())

    rows = []
    for key in sorted(all_keys):
        in_a = key in map_a
        in_b = key in map_b
        if in_a and in_b:
            if map_a[key] != map_b[key]:
                rows.append((key, "Size mismatch", map_a[key], map_b[key]))
        elif in_a and not in_b:
            rows.append((key, "Missing in B", map_a[key], None))
        elif in_b and not in_a:
            rows.append((key, "Missing in A", None, map_b[key]))

    summary = {
        "a_files": len(map_a),
        "b_files": len(map_b),
        "a_size": sum(v for v in map_a.values() if v > 0),
        "b_size": sum(v for v in map_b.values() if v > 0),
        "diff_count": len(rows),
    }
    return summary, rows


def write_html_report(path_a, path_b, summary, rows, out_path):
    diff_class = {
        "Missing in B": "missing",
        "Missing in A": "extra",
        "Size mismatch": "mismatch",
    }

    row_html = []
    for rel, status, size_a, size_b in rows:
        cls = diff_class.get(status, "")
        row_html.append(
            "<tr><td>{}</td><td class='{}'>{}</td><td>{}</td><td>{}</td></tr>".format(
                html.escape(rel),
                cls,
                html.escape(status),
                human_size(size_a) if size_a is not None else "-",
                human_size(size_b) if size_b is not None else "-",
            )
        )

    diff_table = "\n".join(row_html) if row_html else (
        "<tr><td colspan='4' style='text-align:center;color:#2e7d32;font-weight:bold'>"
        "No differences found - folders are identical.</td></tr>"
    )

    html_doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Folder Compare Report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:20px;background:#fafafa;color:#222}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:30px}}
table{{border-collapse:collapse;width:100%;margin-bottom:20px;background:#fff}}
th,td{{border:1px solid #ddd;padding:6px 10px;font-size:13px;text-align:left;word-break:break-all}}
th{{background:#333;color:#fff}}
tr:nth-child(even){{background:#f2f2f2}}
.missing{{color:#b00020;font-weight:bold}}
.extra{{color:#006400;font-weight:bold}}
.mismatch{{color:#b8860b;font-weight:bold}}
.meta{{color:#555;font-size:12px}}
</style></head><body>
<h1>Folder Comparison Report</h1>
<p class="meta">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
<p><b>Folder A:</b> {html.escape(path_a)}<br><b>Folder B:</b> {html.escape(path_b)}</p>
<h2>Summary</h2>
<table>
<tr><th></th><th>Folder A</th><th>Folder B</th></tr>
<tr><td>Files</td><td>{summary['a_files']}</td><td>{summary['b_files']}</td></tr>
<tr><td>Total size</td><td>{human_size(summary['a_size'])}</td><td>{human_size(summary['b_size'])}</td></tr>
</table>
<h2>Differences ({summary['diff_count']} items)</h2>
<table>
<tr><th>Relative path</th><th>Status</th><th>Size A</th><th>Size B</th></tr>
{diff_table}
</table>
</body></html>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)


def file_hash(path, chunk_size=1024 * 1024, max_bytes=None):
    """MD5 of a file. If max_bytes is set, only hash the first max_bytes
    (used for a cheap partial-hash pre-filter)."""
    h = hashlib.md5()
    read_total = 0
    with open(path, "rb") as f:
        while max_bytes is None or read_total < max_bytes:
            to_read = chunk_size if max_bytes is None else min(chunk_size, max_bytes - read_total)
            chunk = f.read(to_read)
            if not chunk:
                break
            h.update(chunk)
            read_total += len(chunk)
    return h.hexdigest()


PARTIAL_HASH_BYTES = 64 * 1024


def find_duplicates(root, progress_cb=None, extensions=None, min_size_bytes=0, max_workers=8):
    """Scan root recursively and return list of duplicate groups.

    Each group is a list of full file paths that share identical size and
    content (md5 hash). Only groups with 2+ files are returned.

    extensions: optional set of lowercase extensions (e.g. {".zip", ".rar"})
    to restrict the scan to. None means all files.
    min_size_bytes: skip files smaller than this.
    """
    root = os.path.abspath(root)
    if progress_cb:
        progress_cb(f"Scanning: {root}")

    by_size = defaultdict(list)
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if extensions is not None and os.path.splitext(name)[1].lower() not in extensions:
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size < min_size_bytes:
                continue
            by_size[size].append(full)

    # Only same-size files can possibly be duplicates.
    candidates = [paths for paths in by_size.values() if len(paths) > 1]
    total_candidates = sum(len(paths) for paths in candidates)
    checked = 0
    lock = threading.Lock()

    def bump(msg_prefix):
        nonlocal checked
        with lock:
            checked += 1
            n = checked
        if progress_cb and n % 25 == 0:
            progress_cb(f"{msg_prefix} ({n}/{total_candidates})")

    # Pass 1: cheap partial hash (first 64KB) to cut down full-file reads.
    by_partial = defaultdict(list)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for paths in candidates:
            for full in paths:
                fut = pool.submit(file_hash, full, max_bytes=PARTIAL_HASH_BYTES)
                futures[fut] = full
        for fut in as_completed(futures):
            full = futures[fut]
            bump("Quick-hashing files...")
            try:
                digest = fut.result()
            except OSError:
                continue
            by_partial[digest].append(full)

    partial_groups = [paths for paths in by_partial.values() if len(paths) > 1]

    # Pass 2: full hash only for files that still collide after the quick check.
    checked = 0
    total_full = sum(len(paths) for paths in partial_groups)
    by_hash = defaultdict(list)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for paths in partial_groups:
            for full in paths:
                fut = pool.submit(file_hash, full)
                futures[fut] = full
        for fut in as_completed(futures):
            full = futures[fut]
            with lock:
                checked += 1
                n = checked
            if progress_cb and n % 25 == 0:
                progress_cb(f"Hashing files... ({n}/{total_full})")
            try:
                digest = fut.result()
            except OSError:
                continue
            by_hash[digest].append(full)

    groups = [paths for paths in by_hash.values() if len(paths) > 1]
    groups.sort(key=lambda paths: -os.path.getsize(paths[0]))
    return groups


def write_duplicates_report(root, groups, out_path):
    total_wasted = sum(
        os.path.getsize(paths[0]) * (len(paths) - 1) for paths in groups
    )

    group_html = []
    for idx, paths in enumerate(groups, start=1):
        size = os.path.getsize(paths[0])
        cols = "\n".join(
            "<td>{}</td>".format(html.escape(os.path.relpath(p, root)))
            for p in paths
        )
        group_html.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td>{}</tr>".format(
                idx, len(paths), human_size(size), cols
            )
        )

    max_cols = max((len(paths) for paths in groups), default=0)
    header_cols = "".join(f"<th>Copy {i+1}</th>" for i in range(max_cols))

    body = "\n".join(group_html) if group_html else (
        f"<tr><td colspan='{3 + max_cols}' style='text-align:center;color:#2e7d32;"
        "font-weight:bold'>No duplicate files found.</td></tr>"
    )

    html_doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Duplicate Files Report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:20px;background:#fafafa;color:#222}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:30px}}
table{{border-collapse:collapse;width:100%;margin-bottom:20px;background:#fff}}
th,td{{border:1px solid #ddd;padding:6px 10px;font-size:13px;text-align:left;word-break:break-all}}
th{{background:#333;color:#fff}}
tr:nth-child(even){{background:#f2f2f2}}
.meta{{color:#555;font-size:12px}}
</style></head><body>
<h1>Duplicate Files Report</h1>
<p class="meta">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
<p><b>Folder:</b> {html.escape(root)}</p>
<h2>Summary</h2>
<table>
<tr><th>Duplicate groups</th><th>Wasted space (extra copies)</th></tr>
<tr><td>{len(groups)}</td><td>{human_size(total_wasted)}</td></tr>
</table>
<h2>Duplicate Groups</h2>
<table>
<tr><th>#</th><th>Copies</th><th>Size (each)</th>{header_cols}</tr>
{body}
</table>
</body></html>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DupliCompare")
        self.geometry("980x560")
        self.minsize(860, 460)
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

    # ---------------------------------------------------------------- Compare

    def _build_compare_tab(self):
        self.path_a = tk.StringVar()
        self.path_b = tk.StringVar()
        self.status = tk.StringVar(value="Ready.")

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
            summary, rows = compare_folders(a, b, progress_cb)
            out_dir = os.path.join(os.path.expanduser("~"), "Desktop")
            if not os.path.isdir(out_dir):
                out_dir = os.getcwd()
            out_path = os.path.join(
                out_dir, f"compare-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
            )
            write_html_report(a, b, summary, rows, out_path)
            self.status.set(f"Done. {summary['diff_count']} difference(s) found. Report: {out_path}")
            webbrowser.open(f"file://{os.path.abspath(out_path)}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Error", str(exc))
            self.status.set("Error occurred.")
        finally:
            self.progress.stop()
            self.compare_btn.config(state="normal")

    # ------------------------------------------------------------- Duplicates

    def _build_duplicates_tab(self):
        self.dup_path = tk.StringVar()
        self.dup_status = tk.StringVar(value="Ready.")
        self.dup_groups = []  # list[list[str]] kept in sync with tree rows

        pad = {"padx": 10, "pady": 6}
        top = ttk.Frame(self.dup_tab)
        top.pack(fill="x")

        ttk.Label(top, text="Folder:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(top, textvariable=self.dup_path, width=45).grid(row=0, column=1, **pad)
        ttk.Button(top, text="Browse...", command=self.browse_dup).grid(row=0, column=2, **pad)

        ttk.Label(top, text="Type:").grid(row=0, column=3, sticky="w", **pad)
        self.dup_filter = tk.StringVar(value="All files")
        filter_combo = ttk.Combobox(
            top, textvariable=self.dup_filter, values=list(FILE_TYPE_FILTERS.keys()),
            state="readonly", width=28,
        )
        filter_combo.grid(row=0, column=4, **pad)

        self.scan_btn = ttk.Button(top, text="Scan for Duplicates", command=self.start_duplicates)
        self.scan_btn.grid(row=0, column=5, **pad)

        ttk.Label(top, text="Min file size:").grid(row=1, column=0, sticky="w", **pad)
        self.min_size_mb = tk.DoubleVar(value=0)
        self.min_size_label = tk.StringVar(value="No limit")
        min_size_scale = ttk.Scale(
            top, from_=0, to=1024, orient="horizontal", variable=self.min_size_mb,
            command=self._on_min_size_change, length=300,
        )
        min_size_scale.grid(row=1, column=1, columnspan=3, sticky="we", **pad)
        ttk.Label(top, textvariable=self.min_size_label, width=14).grid(
            row=1, column=4, sticky="w", **pad
        )

        self.dup_progress = ttk.Progressbar(self.dup_tab, mode="indeterminate", length=500)
        self.dup_progress.pack(fill="x", padx=10, pady=(0, 6))

        ttk.Label(self.dup_tab, textvariable=self.dup_status, foreground="#555").pack(
            fill="x", padx=10
        )

        list_frame = ttk.Frame(self.dup_tab)
        list_frame.pack(fill="both", expand=True, padx=10, pady=10)

        columns = ("group", "copies", "size", "name")
        self.dup_tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.dup_sort_state = {"col": None, "reverse": False}
        self.dup_tree.heading("group", text="#", command=lambda: self.sort_dup_tree("group"))
        self.dup_tree.heading(
            "copies", text="Copies", command=lambda: self.sort_dup_tree("copies")
        )
        self.dup_tree.heading(
            "size", text="Size (each)", command=lambda: self.sort_dup_tree("size")
        )
        self.dup_tree.heading("name", text="File name", command=lambda: self.sort_dup_tree("name"))
        self.dup_tree.column("group", width=40, anchor="center")
        self.dup_tree.column("copies", width=70, anchor="center")
        self.dup_tree.column("size", width=100, anchor="center")
        self.dup_tree.column("name", width=350, anchor="w")

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

    def browse_dup(self):
        d = filedialog.askdirectory(title="Select folder to scan for duplicates")
        if d:
            self.dup_path.set(d)

    def _on_min_size_change(self, _value=None):
        mb = self.min_size_mb.get()
        self.min_size_label.set("No limit" if mb < 1 else f"≥ {mb:.0f} MB")

    def start_duplicates(self):
        a = self.dup_path.get().strip()
        if not a:
            messagebox.showwarning("Missing input", "Please select a folder.")
            return
        if not os.path.isdir(a):
            messagebox.showerror("Invalid path", f"Folder does not exist:\n{a}")
            return

        self.scan_btn.config(state="disabled")
        self.dup_progress.start(10)
        extensions = FILE_TYPE_FILTERS.get(self.dup_filter.get())
        min_size_bytes = int(self.min_size_mb.get() * 1024 * 1024)
        thread = threading.Thread(
            target=self.run_duplicates, args=(a, extensions, min_size_bytes), daemon=True
        )
        thread.start()

    def run_duplicates(self, a, extensions=None, min_size_bytes=0):
        def progress_cb(msg):
            self.dup_status.set(msg)

        try:
            groups = find_duplicates(a, progress_cb, extensions=extensions, min_size_bytes=min_size_bytes)
            self.dup_root = os.path.abspath(a)
            self.after(0, lambda: self._populate_dup_tree(groups))
            self.dup_status.set(f"Done. {len(groups)} duplicate group(s) found.")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Error", str(exc))
            self.dup_status.set("Error occurred.")
        finally:
            self.dup_progress.stop()
            self.scan_btn.config(state="normal")

    def _populate_dup_tree(self, groups, chunk_size=200):
        """Insert rows in chunks via `after` so the UI stays responsive and
        the whole (potentially huge) result set still gets loaded fully."""
        self.dup_groups = groups
        self.dup_tree.delete(*self.dup_tree.get_children())
        self.dup_sort_state = {"col": None, "reverse": False}
        total = len(groups)

        def load_chunk(start):
            end = min(start + chunk_size, total)
            for idx in range(start, end):
                paths = groups[idx]
                size = os.path.getsize(paths[0]) if paths else 0
                name = os.path.basename(paths[0]) if paths else ""
                self.dup_tree.insert(
                    "", "end", iid=str(idx),
                    values=(idx + 1, len(paths), human_size(size), name),
                )
            if end < total:
                self.dup_status.set(f"Loading duplicate groups... {end}/{total}")
                self.after(1, lambda: load_chunk(end))
            else:
                self.dup_status.set(f"Done. {total} duplicate group(s) found.")

        if total:
            load_chunk(0)
        else:
            self.dup_status.set("Done. No duplicate groups found.")

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
                return os.path.getsize(paths[0])
            if col == "name":
                return os.path.basename(paths[0]).lower()
            return idx

        ids = [iid for iid in self.dup_tree.get_children("")]
        ids.sort(key=lambda iid: sort_key(int(iid)), reverse=reverse)
        for pos, iid in enumerate(ids):
            self.dup_tree.move(iid, "", pos)

        for c in ("group", "copies", "size", "name"):
            label = {"group": "#", "copies": "Copies", "size": "Size (each)", "name": "File name"}[c]
            if c == col:
                label += " ▼" if reverse else " ▲"
            self.dup_tree.heading(c, text=label)

    def open_selected_group(self):
        sel = self.dup_tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a duplicate group first.")
            return
        idx = int(sel[0])
        DuplicateGroupWindow(self, idx, self.dup_groups[idx], self.dup_root, self.on_group_changed)

    def on_group_changed(self, idx, new_paths):
        """Called by DuplicateGroupWindow after a file was deleted/renamed/moved."""
        if len(new_paths) < 2:
            self.dup_groups[idx] = []
            if self.dup_tree.exists(str(idx)):
                self.dup_tree.delete(str(idx))
            return
        self.dup_groups[idx] = new_paths
        size = os.path.getsize(new_paths[0])
        name = os.path.basename(new_paths[0])
        self.dup_tree.item(
            str(idx), values=(idx + 1, len(new_paths), human_size(size), name)
        )

    def export_dup_report(self):
        groups = [g for g in self.dup_groups if g]
        if not getattr(self, "dup_root", None):
            messagebox.showinfo("Nothing to export", "Run a scan first.")
            return
        out_dir = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.isdir(out_dir):
            out_dir = os.getcwd()
        out_path = os.path.join(
            out_dir, f"duplicates-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
        )
        write_duplicates_report(self.dup_root, groups, out_path)
        self.dup_status.set(f"Report saved: {out_path}")
        webbrowser.open(f"file://{os.path.abspath(out_path)}")


class DuplicateGroupWindow(tk.Toplevel):
    """Shows every copy in a duplicate group side by side, each with its own
    Delete / Rename / Move controls."""

    def __init__(self, parent, group_idx, paths, root, on_changed):
        super().__init__(parent)
        self.parent = parent
        self.group_idx = group_idx
        self.paths = list(paths)
        self.root = root
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

        try:
            rel = os.path.relpath(path, self.root)
        except ValueError:
            rel = path
        size = human_size(os.path.getsize(path)) if os.path.exists(path) else "N/A"

        ttk.Label(card, text=f"Copy {col + 1}", font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        path_lbl = tk.Text(card, width=32, height=4, wrap="char", borderwidth=0)
        path_lbl.insert("1.0", rel)
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
