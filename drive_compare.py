"""
DupliCompare: cross-platform Tkinter app to
  - compare two folders (quick: names and sizes; verified: also contents) and
    write an indexed, searchable HTML report, and
  - find duplicate files across any number of folders, and act on one copy at
    a time (quarantine, delete, rename, move) after re-checking it.

The scan/compare/duplicate logic lives in dc_engine.py, the file actions in
file_actions.py, the texts in dc_i18n.py (EN/SV/FA). Worker threads never
touch Tk: they post to a queue that the Tk loop drains with `after`.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import ai_assist
import dc_engine as eng
import file_actions as fa
from dc_i18n import LANGUAGES, RTL, TEXTS
from report_writer import write_report

VERSION = "1.1.0"
APP_NAME = "DupliCompare"
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".duplicompare", "settings.json")
human_size = eng.human_size


# ------------------------------------------------------------------ texts

def translator(lang):
    texts = TEXTS.get(lang, TEXTS["en"])

    def tr(key, **params):
        text = texts.get(key) or TEXTS["en"].get(key, key)
        try:
            return text.format(**params) if params else text
        except (KeyError, IndexError, ValueError):
            return text
    return tr


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except OSError:
        pass


# ---------------------------------------------------------------- reports

def _coverage_parts(report, tr):
    cards = [(tr("card_coverage"),
              tr("coverage_complete") if report.complete else tr("coverage_incomplete"))]
    if report.error_count:
        cards.append((tr("card_errors"), f"{report.error_count:,}"))
    tab = {
        "id": "errors", "label": tr("tab_errors"),
        "note": tr("note_errors", kept=len(report.errors), total=report.error_count),
        "cols": [("path", tr("col_path"), "text"), ("err", tr("col_error"), "text")],
        "rows": [list(e) for e in report.errors],
    }
    return cards, tab


def write_compare_report(path_a, path_b, summary, result, report, out_dir, tr, lang="en"):
    """Write the compare report folder; returns the index.html path."""
    info = [(tr("folder_a"), path_a), (tr("folder_b"), path_b),
            (tr("report_mode"), tr("mode_" + summary["mode"]))]
    cards = [
        (tr("card_verdict"), tr("verdict_" + summary["verdict"])),
        (tr("card_files_a"), f"{summary['a_files']:,}"),
        (tr("card_files_b"), f"{summary['b_files']:,}"),
        (tr("card_size_a"), human_size(summary["a_size"])),
        (tr("card_size_b"), human_size(summary["b_size"])),
        (tr("card_only_a"), tr("n_files_size", n=f"{summary['only_a']:,}",
                               size=human_size(summary["only_a_size"]))),
        (tr("card_only_b"), tr("n_files_size", n=f"{summary['only_b']:,}",
                               size=human_size(summary["only_b_size"]))),
        (tr("card_diff_size"), f"{summary['mismatch']:,}"),
    ]
    if summary["mode"] == eng.MODE_VERIFIED:
        cards += [
            (tr("card_verified_same"), f"{summary['verified_same']:,}"),
            (tr("card_content_diff"), f"{summary['content_different']:,}"),
            (tr("card_unreadable"), f"{summary['unreadable']:,}"),
            (tr("card_changed"), f"{summary['changed']:,}"),
        ]
    else:
        cards.append((tr("card_unverified"), f"{summary['unverified']:,}"))
    cov_cards, err_tab = _coverage_parts(report, tr)
    cards = cards[:1] + cov_cards + cards[1:]

    folder_cols = [("path", tr("col_folder"), "text"), ("files", tr("col_files"), "num"),
                   ("size", tr("col_size"), "size")]
    file_cols = [("path", tr("col_file"), "text"), ("size", tr("col_size"), "size")]
    tabs = [
        {"id": "a-folders", "label": tr("tab_folders_a"), "note": tr("note_folders_a"),
         "cols": folder_cols, "rows": [list(r) for r in result["folders_only_a"]]},
        {"id": "b-folders", "label": tr("tab_folders_b"), "note": tr("note_folders_b"),
         "cols": folder_cols, "rows": [list(r) for r in result["folders_only_b"]]},
        {"id": "a-files", "label": tr("tab_files_a"), "note": tr("note_files_a"),
         "cols": file_cols, "rows": [list(r) for r in result["files_only_a"]]},
        {"id": "b-files", "label": tr("tab_files_b"), "note": tr("note_files_b"),
         "cols": file_cols, "rows": [list(r) for r in result["files_only_b"]]},
        {"id": "size", "label": tr("tab_diff_size"), "note": tr("note_diff_size"),
         "cols": [("path", tr("col_file"), "text"), ("a", tr("col_size_a"), "size"),
                  ("b", tr("col_size_b"), "size")],
         "rows": [list(r) for r in result["mismatch"]]},
    ]
    if summary["mode"] == eng.MODE_VERIFIED:
        tabs += [
            {"id": "content", "label": tr("tab_content_diff"), "note": tr("note_content_diff"),
             "cols": file_cols, "rows": [list(r) for r in result["content_different"]]},
            {"id": "changed", "label": tr("tab_changed"), "note": tr("note_changed"),
             "cols": file_cols, "rows": [list(r) for r in result["content_changed"]]},
        ]
    tabs.append(err_tab)
    return write_report(out_dir, tr("report_compare_title"), info, cards, tabs,
                        ui=_report_ui(tr), lang=lang, rtl=lang in RTL)


def write_duplicates_report(rootmap, groups, hardlinks, report, out_dir, tr, lang="en"):
    """Write the duplicates report folder; returns the index.html path."""
    rows_cross, rows_same = [], []
    total_wasted = 0
    for g in groups:
        paths = g.paths
        wasted = max(g.size, 0) * (len(paths) - 1)
        total_wasted += wasted
        row = [len(paths), g.size, wasted, rootmap.where(paths),
               [rootmap.describe(p) for p in paths]]
        (rows_cross if rootmap.is_cross(paths) else rows_same).append(row)

    def finish(rows):
        rows.sort(key=lambda r: -r[2])
        for n, r in enumerate(rows, start=1):
            r.insert(0, n)
        return rows

    cols = [("n", "#", "num"), ("copies", tr("col_copies"), "num"),
            ("size", tr("col_size_each"), "size"), ("wasted", tr("col_wasted"), "size"),
            ("where", tr("col_where"), "text"), ("files", tr("col_files_list"), "lines")]
    info = [(tr("folders"), "")] + [(RootMap.tag(i), p) for i, p in enumerate(rootmap.roots)]
    cards = [
        (tr("card_groups"), f"{len(groups):,}"),
        (tr("show_cross"), f"{len(rows_cross):,}"),
        (tr("show_same"), f"{len(rows_same):,}"),
        (tr("card_wasted"), human_size(total_wasted)),
        (tr("card_hardlinks"), f"{len(hardlinks):,}"),
    ]
    cov_cards, err_tab = _coverage_parts(report, tr)
    cards = cov_cards + cards
    tabs = [
        {"id": "across", "label": tr("show_cross"), "note": tr("note_across"),
         "cols": cols, "rows": finish(rows_cross)},
        {"id": "inside", "label": tr("show_same"), "note": tr("note_inside"),
         "cols": cols, "rows": finish(rows_same)},
        {"id": "hardlinks", "label": tr("tab_hardlinks"), "note": tr("note_hardlinks"),
         "cols": [("files", tr("col_files_list"), "lines")],
         "rows": [[[rootmap.describe(p) for p in h]] for h in hardlinks]},
        err_tab,
    ]
    return write_report(out_dir, tr("report_dup_title"), info, cards, tabs,
                        ui=_report_ui(tr), lang=lang, rtl=lang in RTL)


def _report_ui(tr):
    keys = ["generated", "search", "csv", "loading", "rows_of", "rows", "page", "prev",
            "next", "nothing_matches", "nothing", "keep_data"]
    return {k: tr("rep_" + k) for k in keys}


RootMap = eng.RootMap


def _default_out_dir(prefix):
    base = os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(base):
        base = os.getcwd()
    return os.path.join(base, f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")


def _open_in_browser(path):
    webbrowser.open(Path(os.path.abspath(path)).as_uri())


def _open_folder(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


# ----------------------------------------------------------------------- GUI

SHOW_MODES = ("all", "cross", "same")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.lang = self.settings.get("lang") if self.settings.get("lang") in TEXTS else "en"
        self.tr = translator(self.lang)
        self.geometry("1060x680")
        self.minsize(900, 540)
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            self.iconbitmap(os.path.join(base, "assets", "icon.ico"))
        except tk.TclError:
            pass

        # state that survives a language switch
        self.path_a = tk.StringVar()
        self.path_b = tk.StringVar()
        self.compare_mode = tk.StringVar(value=self.settings.get("compare_mode", eng.MODE_QUICK))
        self.dup_threads = tk.IntVar(value=int(self.settings.get("threads", eng.DEFAULT_WORKERS)))
        self.dup_filter_key = self.settings.get("filter", "all")
        self.min_size_mb = tk.DoubleVar(value=0)
        self.dup_view_key = "all"
        self.dup_roots = []
        self.status_text = {"compare": ("status_ready", {}), "dup": ("status_ready", {})}
        self.last_compare = None    # (a, b, summary, result, report)
        self.dup_result = None      # eng.DupResult
        self.dup_groups = []        # list[DupGroup | None], index = tree iid
        self.dup_rootmap = None
        self.session = fa.session_id()

        self.jobs = queue.Queue()
        self.job = None             # {"tab", "cancel", "done"}
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()

    # ------------------------------------------------------------ building

    @property
    def rtl(self):
        return self.lang in RTL

    def _side(self, side="left"):
        if not self.rtl:
            return side
        return {"left": "right", "right": "left"}.get(side, side)

    def _sticky(self, s):
        if not self.rtl:
            return s
        return s.translate(str.maketrans("we", "ew"))

    def _build(self):
        tr = self.tr
        self.title(f"{APP_NAME} {VERSION}")
        for child in self.winfo_children():
            if not isinstance(child, tk.Toplevel):
                child.destroy()
        menubar = tk.Menu(self)
        lang_menu = tk.Menu(menubar, tearoff=False)
        self._lang_var = tk.StringVar(value=self.lang)
        for code, label in LANGUAGES.items():
            lang_menu.add_radiobutton(label=label, value=code, variable=self._lang_var,
                                      command=lambda c=code: self.set_language(c))
        tools = tk.Menu(menubar, tearoff=False)
        tools.add_command(label=tr("menu_quarantine"), command=self.open_quarantine)
        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label=tr("menu_about"), command=self.show_about)
        menubar.add_cascade(label=tr("menu_language"), menu=lang_menu)
        menubar.add_cascade(label=tr("menu_tools"), menu=tools)
        menubar.add_cascade(label=tr("menu_help"), menu=help_menu)
        self.config(menu=menubar)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)
        self.compare_tab = ttk.Frame(notebook)
        self.dup_tab = ttk.Frame(notebook)
        notebook.add(self.compare_tab, text=tr("tab_compare"))
        notebook.add(self.dup_tab, text=tr("tab_duplicates"))
        self._build_compare_tab()
        self._build_duplicates_tab()
        self._refresh_status()
        self._set_busy(self.job is not None)
        if self.dup_rootmap is not None:
            self._populate_dup_tree()

    def set_language(self, code):
        if code == self.lang or code not in TEXTS:
            return
        self.lang = code
        self.tr = translator(code)
        self.settings["lang"] = code
        save_settings(self.settings)
        self._build()

    def show_about(self):
        messagebox.showinfo(self.tr("menu_about"),
                            self.tr("about_text", name=APP_NAME, version=VERSION), parent=self)

    # ------------------------------------------------------------ jobs

    def _start_job(self, tab, work, on_done):
        """Run work(progress, cancel) in a thread; on_done(result) runs in Tk."""
        if self.job is not None:
            messagebox.showinfo(APP_NAME, self.tr("job_running"), parent=self)
            return False
        cancel = threading.Event()
        self.job = {"tab": tab, "cancel": cancel, "done": on_done}

        def progress(key, params):
            self.jobs.put(("progress", tab, key, params))

        def run():
            try:
                res = work(progress, cancel)
                self.jobs.put(("done", tab, res, None))
            except eng.Cancelled:
                self.jobs.put(("cancelled", tab, None, None))
            except fa.ActionBlocked as exc:
                self.jobs.put(("done", tab, exc, None))
            except Exception as exc:  # noqa: BLE001
                self.jobs.put(("error", tab, str(exc), None))

        self._set_busy(True)
        threading.Thread(target=run, daemon=True).start()
        return True

    def cancel_job(self):
        if self.job is not None:
            self.job["cancel"].set()
            self._status(self.job["tab"], "status_cancelling")

    def _drain(self):
        try:
            while True:
                kind, tab, a, b = self.jobs.get_nowait()
                if kind == "progress":
                    self._status(tab, a, **b)
                    continue
                job, self.job = self.job, None
                self._set_busy(False)
                if kind == "done" and job:
                    job["done"](a)
                elif kind == "cancelled":
                    self._status(tab, "status_cancelled")
                elif kind == "error":
                    self._status(tab, "status_error")
                    messagebox.showerror(self.tr("error"), a, parent=self)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _set_busy(self, busy):
        for name in ("compare_btn", "scan_btn"):
            w = getattr(self, name, None)
            if w is not None and w.winfo_exists():
                w.config(state="disabled" if busy else "normal")
        for name in ("compare_cancel", "scan_cancel"):
            w = getattr(self, name, None)
            if w is not None and w.winfo_exists():
                w.config(state="normal" if busy else "disabled")
        for name in ("progress", "dup_progress"):
            w = getattr(self, name, None)
            if w is not None and w.winfo_exists():
                (w.start(10) if busy else w.stop())

    def _status(self, tab, key, **params):
        self.status_text[tab] = (key, params)
        self._refresh_status()

    def _refresh_status(self):
        for tab, var in (("compare", getattr(self, "status", None)),
                         ("dup", getattr(self, "dup_status", None))):
            if var is not None:
                key, params = self.status_text[tab]
                var.set(self.tr(key, **params))

    def _on_close(self):
        if self.job is not None:
            if not messagebox.askyesno(APP_NAME, self.tr("confirm_quit_running"), parent=self):
                return
            self.job["cancel"].set()
        self.settings.update({"compare_mode": self.compare_mode.get(),
                              "threads": self._workers(), "filter": self.dup_filter_key})
        save_settings(self.settings)
        self.destroy()

    def _workers(self):
        try:
            return max(1, min(32, int(self.dup_threads.get())))
        except (tk.TclError, ValueError):
            return eng.DEFAULT_WORKERS

    # ---------------------------------------------------------------- Compare

    def _build_compare_tab(self):
        tr = self.tr
        self.status = tk.StringVar()
        pad = {"padx": 10, "pady": 6}
        frm = ttk.Frame(self.compare_tab)
        frm.pack(fill="both", expand=True)
        c = (lambda i: 3 - i) if self.rtl else (lambda i: i)
        justify = "right" if self.rtl else "left"

        for row, (label, var, cmd) in enumerate(((tr("folder_a"), self.path_a, self.browse_a),
                                                 (tr("folder_b"), self.path_b, self.browse_b))):
            ttk.Label(frm, text=label).grid(row=row, column=c(0), sticky=self._sticky("w"), **pad)
            ttk.Entry(frm, textvariable=var, width=60, justify=justify).grid(
                row=row, column=c(1), sticky="we", **pad)
            ttk.Button(frm, text=tr("browse"), command=cmd).grid(row=row, column=c(2), **pad)
        frm.columnconfigure(c(1), weight=1)

        modes = ttk.Frame(frm)
        modes.grid(row=2, column=c(1), sticky=self._sticky("w"), **pad)
        for value in (eng.MODE_QUICK, eng.MODE_VERIFIED):
            ttk.Radiobutton(modes, text=tr("mode_" + value), value=value,
                            variable=self.compare_mode).pack(anchor=self._sticky("w"))
        ttk.Label(frm, text=tr("mode_hint"), foreground="#777", wraplength=640,
                  justify=justify).grid(row=3, column=c(1), sticky=self._sticky("w"), padx=10)

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=c(1), pady=16)
        self.compare_btn = ttk.Button(btns, text=tr("compare"), command=self.start_compare)
        self.compare_btn.pack(side=self._side(), padx=4)
        self.compare_cancel = ttk.Button(btns, text=tr("cancel"), command=self.cancel_job,
                                         state="disabled")
        self.compare_cancel.pack(side=self._side(), padx=4)
        ttk.Button(btns, text=tr("ai_button"), command=self.ask_ai_compare).pack(
            side=self._side(), padx=4)

        ttk.Label(frm, textvariable=self.status, foreground="#555", wraplength=900,
                  justify=justify).grid(row=5, column=0, columnspan=4,
                                        sticky=self._sticky("w"), **pad)
        self.progress = ttk.Progressbar(frm, mode="indeterminate", length=500)
        self.progress.grid(row=6, column=0, columnspan=4, padx=10, pady=6)

    def browse_a(self):
        d = filedialog.askdirectory(title=self.tr("folder_a"), parent=self)
        if d:
            self.path_a.set(d)

    def browse_b(self):
        d = filedialog.askdirectory(title=self.tr("folder_b"), parent=self)
        if d:
            self.path_b.set(d)

    def start_compare(self):
        tr = self.tr
        a, b = self.path_a.get().strip(), self.path_b.get().strip()
        if not a or not b:
            messagebox.showwarning(tr("missing_input"), tr("pick_both"), parent=self)
            return
        for p in (a, b):
            if not os.path.isdir(p):
                messagebox.showerror(tr("invalid_path"), tr("folder_missing", path=p), parent=self)
                return
        mode = self.compare_mode.get()
        workers = self._workers()

        def work(progress, cancel):
            summary, result, report = eng.compare_folders(a, b, mode, progress, cancel, workers)
            progress("status_writing", {})
            out_dir = _default_out_dir("compare-report")
            index = write_compare_report(a, b, summary, result, report, out_dir,
                                         self.tr, self.lang)
            return a, b, summary, result, report, out_dir, index

        def done(res):
            a_, b_, summary, result, report, out_dir, index = res
            self.last_compare = (a_, b_, summary, result, report)
            self._status("compare", "compare_done", verdict=self.tr("verdict_" + summary["verdict"]),
                         n=summary["diff_count"], errors=report.error_count, path=out_dir)
            _open_in_browser(index)

        self._start_job("compare", work, done)

    # ------------------------------------------------------------- Duplicates

    def _build_duplicates_tab(self):
        tr = self.tr
        self.dup_status = tk.StringVar()
        pad = {"padx": 10, "pady": 4}
        c = (lambda i, n=6: n - 1 - i) if self.rtl else (lambda i, n=6: i)

        folders = ttk.Frame(self.dup_tab)
        folders.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(folders, text=tr("folders_to_scan")).grid(row=0, column=c(0, 4), sticky="n")
        self.dup_list = tk.Listbox(folders, height=4, selectmode="extended", activestyle="none",
                                   justify="right" if self.rtl else "left")
        self.dup_list.grid(row=0, column=c(1, 4), sticky="we", padx=8)
        for r in self.dup_roots:
            self.dup_list.insert("end", r)
        lsb = ttk.Scrollbar(folders, orient="vertical", command=self.dup_list.yview)
        self.dup_list.configure(yscrollcommand=lsb.set)
        lsb.grid(row=0, column=c(2, 4), sticky="ns")
        btns = ttk.Frame(folders)
        btns.grid(row=0, column=c(3, 4), sticky="n", padx=8)
        ttk.Button(btns, text=tr("add_folder"), command=self.add_dup_folder).pack(fill="x", pady=1)
        ttk.Button(btns, text=tr("remove_selected"), command=self.remove_dup_folders).pack(
            fill="x", pady=1)
        ttk.Button(btns, text=tr("clear"), command=self.clear_dup_folders).pack(fill="x", pady=1)
        folders.columnconfigure(c(1, 4), weight=1)

        top = ttk.Frame(self.dup_tab)
        top.pack(fill="x")
        ttk.Label(top, text=tr("type")).grid(row=0, column=c(0), sticky=self._sticky("w"), **pad)
        self._filter_labels = {k: tr("filter_" + k) for k in eng.FILE_TYPE_FILTERS}
        self.dup_filter = tk.StringVar(value=self._filter_labels.get(self.dup_filter_key,
                                                                     self._filter_labels["all"]))
        fbox = ttk.Combobox(top, textvariable=self.dup_filter,
                            values=list(self._filter_labels.values()), state="readonly", width=30)
        fbox.grid(row=0, column=c(1), **pad)
        fbox.bind("<<ComboboxSelected>>", self._on_filter)
        ttk.Label(top, text=tr("threads")).grid(row=0, column=c(2), sticky="e", **pad)
        ttk.Spinbox(top, from_=1, to=32, width=4, textvariable=self.dup_threads).grid(
            row=0, column=c(3), **pad)
        ttk.Label(top, text=tr("threads_hint"), foreground="#777").grid(row=0, column=c(4))
        sbtn = ttk.Frame(top)
        sbtn.grid(row=0, column=c(5), **pad)
        self.scan_btn = ttk.Button(sbtn, text=tr("scan"), command=self.start_duplicates)
        self.scan_btn.pack(side=self._side())
        self.scan_cancel = ttk.Button(sbtn, text=tr("cancel"), command=self.cancel_job,
                                      state="disabled")
        self.scan_cancel.pack(side=self._side(), padx=4)

        ttk.Label(top, text=tr("min_size")).grid(row=1, column=c(0), sticky=self._sticky("w"), **pad)
        self.min_size_label = tk.StringVar()
        ttk.Scale(top, from_=0, to=1024, orient="horizontal", variable=self.min_size_mb,
                  command=self._on_min_size_change, length=300).grid(
            row=1, column=c(1), sticky="we", **pad)
        ttk.Label(top, textvariable=self.min_size_label, width=14).grid(
            row=1, column=c(2), sticky=self._sticky("w"), **pad)
        self._on_min_size_change()
        ttk.Label(top, text=tr("show")).grid(row=1, column=c(4), sticky="e", **pad)
        self._show_labels = {k: tr("show_" + k) for k in SHOW_MODES}
        self.dup_view = tk.StringVar(value=self._show_labels[self.dup_view_key])
        show = ttk.Combobox(top, textvariable=self.dup_view, values=list(self._show_labels.values()),
                            state="readonly", width=24)
        show.grid(row=1, column=c(5), **pad)
        show.bind("<<ComboboxSelected>>", self._on_show)

        self.dup_progress = ttk.Progressbar(self.dup_tab, mode="indeterminate", length=500)
        self.dup_progress.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(self.dup_tab, textvariable=self.dup_status, foreground="#555",
                  anchor="e" if self.rtl else "w").pack(fill="x", padx=10)

        list_frame = ttk.Frame(self.dup_tab)
        list_frame.pack(fill="both", expand=True, padx=10, pady=10)
        columns = ("group", "copies", "size", "where", "name")
        if self.rtl:
            columns = tuple(reversed(columns))
        self.dup_tree = ttk.Treeview(list_frame, columns=columns, show="headings",
                                     selectmode="browse")
        self.dup_sort_state = {"col": None, "reverse": False}
        self._col_labels = {"group": "#", "copies": tr("col_copies"),
                            "size": tr("col_size_each"), "where": tr("col_where"),
                            "name": tr("col_name")}
        for col in columns:
            self.dup_tree.heading(col, text=self._col_labels[col],
                                  command=lambda c_=col: self.sort_dup_tree(c_))
        widths = {"group": 50, "copies": 70, "size": 100, "where": 130, "name": 420}
        for col in columns:
            anchor = ("e" if self.rtl else "w") if col == "name" else "center"
            self.dup_tree.column(col, width=widths[col], anchor=anchor)
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.dup_tree.yview)
        self.dup_tree.configure(yscrollcommand=vsb.set)
        self.dup_tree.pack(side=self._side(), fill="both", expand=True)
        vsb.pack(side=self._side("right"), fill="y")
        self.dup_tree.bind("<Double-1>", lambda e: self.open_selected_group())
        self.dup_tree.bind("<Return>", lambda e: self.open_selected_group())

        bottom = ttk.Frame(self.dup_tab)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        for text, cmd in ((tr("open_group"), self.open_selected_group),
                          (tr("export_report"), self.export_dup_report),
                          (tr("ai_button"), self.ask_ai_duplicates)):
            ttk.Button(bottom, text=text, command=cmd).pack(side=self._side(), padx=4)

    def _on_filter(self, _e=None):
        rev = {v: k for k, v in self._filter_labels.items()}
        self.dup_filter_key = rev.get(self.dup_filter.get(), "all")

    def _on_show(self, _e=None):
        rev = {v: k for k, v in self._show_labels.items()}
        self.dup_view_key = rev.get(self.dup_view.get(), "all")
        self._populate_dup_tree()

    def add_dup_folder(self):
        d = filedialog.askdirectory(title=self.tr("add_folder"), parent=self)
        if not d:
            return
        d = os.path.abspath(d)
        if os.path.normcase(d) not in {os.path.normcase(p) for p in self.dup_roots}:
            self.dup_roots.append(d)
            self.dup_list.insert("end", d)

    def remove_dup_folders(self):
        for i in reversed(self.dup_list.curselection()):
            self.dup_list.delete(i)
            del self.dup_roots[i]

    def clear_dup_folders(self):
        self.dup_list.delete(0, "end")
        self.dup_roots = []

    def _on_min_size_change(self, _value=None):
        mb = self.min_size_mb.get()
        self.min_size_label.set(self.tr("no_limit") if mb < 1 else f"≥ {mb:.0f} MB")

    def start_duplicates(self):
        tr = self.tr
        roots = list(self.dup_roots)
        if not roots:
            messagebox.showwarning(tr("missing_input"), tr("add_one_folder"), parent=self)
            return
        for r in roots:
            if not os.path.isdir(r):
                messagebox.showerror(tr("invalid_path"), tr("folder_missing", path=r), parent=self)
                return
        workers = self._workers()
        extensions = eng.FILE_TYPE_FILTERS.get(self.dup_filter_key)
        min_size_bytes = int(self.min_size_mb.get() * 1024 * 1024)

        def work(progress, cancel):
            return eng.find_duplicates(roots, progress, extensions, min_size_bytes,
                                       workers, cancel), RootMap(roots)

        def done(res):
            result, rootmap = res
            self.dup_result = result
            self.dup_groups = list(result.groups)
            self.dup_rootmap = rootmap
            self._populate_dup_tree()

        self._start_job("dup", work, done)

    def _visible_indexes(self):
        out = []
        for idx, g in enumerate(self.dup_groups):
            if g is None:
                continue
            cross = self.dup_rootmap.is_cross(g.paths)
            if self.dup_view_key == "cross" and not cross:
                continue
            if self.dup_view_key == "same" and cross:
                continue
            out.append(idx)
        return out

    def _row_values(self, idx):
        g = self.dup_groups[idx]
        vals = (idx + 1, len(g.files), human_size(g.size), self.dup_rootmap.where(g.paths),
                os.path.basename(g.files[0].path))
        return tuple(reversed(vals)) if self.rtl else vals

    def _done_status(self, shown):
        r = self.dup_result.report if self.dup_result else eng.ScanReport()
        found = sum(1 for g in self.dup_groups if g is not None)
        key = "dup_done" if r.complete else "dup_done_incomplete"
        self._status("dup", key, shown=shown, found=found, errors=r.error_count,
                     links=len(self.dup_result.hardlinks) if self.dup_result else 0)

    def _populate_dup_tree(self, chunk_size=200):
        """Insert rows in chunks via `after` so the UI stays responsive."""
        self.dup_tree.delete(*self.dup_tree.get_children())
        self.dup_sort_state = {"col": None, "reverse": False}
        for col, label in self._col_labels.items():
            self.dup_tree.heading(col, text=label)
        if self.dup_rootmap is None:
            return
        indexes = self._visible_indexes()
        total = len(indexes)
        token = object()
        self._populate_token = token
        tree = self.dup_tree

        def load_chunk(start):
            if self._populate_token is not token or not tree.winfo_exists():
                return
            end = min(start + chunk_size, total)
            for idx in indexes[start:end]:
                tree.insert("", "end", iid=str(idx), values=self._row_values(idx))
            if end < total:
                self._status("dup", "loading_groups", done=end, total=total)
                self.after(1, lambda: load_chunk(end))
            else:
                self._done_status(total)

        load_chunk(0)

    def sort_dup_tree(self, col):
        state = self.dup_sort_state
        reverse = not state["reverse"] if state["col"] == col else False
        state["col"], state["reverse"] = col, reverse

        def sort_key(idx):
            g = self.dup_groups[idx]
            if g is None:
                return 0
            return {"group": idx, "copies": len(g.files), "size": g.size,
                    "where": self.dup_rootmap.where(g.paths),
                    "name": os.path.basename(g.files[0].path).lower()}.get(col, idx)

        ids = list(self.dup_tree.get_children(""))
        ids.sort(key=lambda iid: sort_key(int(iid)), reverse=reverse)
        for pos, iid in enumerate(ids):
            self.dup_tree.move(iid, "", pos)
        for c_, label in self._col_labels.items():
            if c_ == col:
                label += " ▼" if reverse else " ▲"
            self.dup_tree.heading(c_, text=label)

    # ------------------------------------------------------------------ AI

    def ask_ai_compare(self):
        if not self.last_compare:
            messagebox.showinfo(self.tr("ai_title"), self.tr("ai_run_compare"), parent=self)
            return
        a, b, summary, result, _report = self.last_compare
        self._open_ai("compare", lambda redact: ai_assist.build_compare_digest(
            a, b, summary, result, redact))

    def ask_ai_duplicates(self):
        groups = [g for g in self.dup_groups if g is not None]
        if not self.dup_rootmap or not groups:
            messagebox.showinfo(self.tr("ai_title"), self.tr("ai_run_scan"), parent=self)
            return
        rootmap = self.dup_rootmap
        links = len(self.dup_result.hardlinks) if self.dup_result else 0
        self._open_ai("duplicates", lambda redact: ai_assist.build_duplicates_digest(
            rootmap, groups, redact, links))

    def _open_ai(self, kind, make_digest):
        cfg = ai_assist.load_config()
        if not cfg:
            messagebox.showinfo(self.tr("ai_title"), ai_assist.SETUP_HELP, parent=self)
            return
        AiWindow(self, kind, make_digest, cfg)

    # ------------------------------------------------------- groups/actions

    def describe_path(self, path):
        return self.dup_rootmap.describe(path) if self.dup_rootmap else path

    def open_selected_group(self):
        sel = self.dup_tree.selection()
        if not sel:
            messagebox.showinfo(self.tr("no_selection"), self.tr("select_group"), parent=self)
            return
        idx = int(sel[0])
        if self.dup_groups[idx] is not None:
            DuplicateGroupWindow(self, idx)

    def on_group_changed(self, idx):
        g = self.dup_groups[idx]
        if g is not None and len(g.files) < 2:
            self.dup_groups[idx] = None
            g = None
        if not self.dup_tree.winfo_exists():
            return
        if g is None:
            if self.dup_tree.exists(str(idx)):
                self.dup_tree.delete(str(idx))
        elif self.dup_tree.exists(str(idx)):
            self.dup_tree.item(str(idx), values=self._row_values(idx))

    def export_dup_report(self):
        if not self.dup_rootmap or self.dup_result is None:
            messagebox.showinfo(self.tr("nothing_to_export"), self.tr("ai_run_scan"), parent=self)
            return
        groups = [g for g in self.dup_groups if g is not None]
        out_dir = _default_out_dir("duplicates-report")
        index = write_duplicates_report(self.dup_rootmap, groups, self.dup_result.hardlinks,
                                        self.dup_result.report, out_dir, self.tr, self.lang)
        self._status("dup", "report_saved", path=out_dir)
        _open_in_browser(index)

    def open_quarantine(self):
        QuarantineWindow(self)


class AiWindow(tk.Toplevel):
    """Shows exactly what will be sent to the AI, sends it on request, shows the answer."""

    def __init__(self, parent, kind, make_digest, cfg):
        super().__init__(parent)
        tr = parent.tr
        self.tr, self.kind, self.make_digest, self.cfg = tr, kind, make_digest, cfg
        self.title(tr("ai_title"))
        self.geometry("820x720")
        side = parent._side

        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text=tr("ai_language")).pack(side=side())
        self.lang = tk.StringVar(value=list(ai_assist.LANGUAGES)[
            {"en": 0, "sv": 1, "fa": 2}.get(parent.lang, 0)])
        ttk.Combobox(top, textvariable=self.lang, values=list(ai_assist.LANGUAGES),
                     state="readonly", width=12).pack(side=side(), padx=6)
        self.redact = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text=tr("ai_counts_only"), variable=self.redact,
                        command=self._show_digest).pack(side=side(), padx=6)
        self.send_btn = ttk.Button(top, text=tr("ai_send"), command=self.send)
        self.send_btn.pack(side=side(), padx=6)
        self.status = tk.StringVar(value=tr("ai_privacy"))
        ttk.Label(self, textvariable=self.status, foreground="#555", wraplength=780).pack(
            fill="x", padx=10)

        ttk.Label(self, text=tr("ai_what_sent")).pack(anchor=parent._sticky("w"), padx=10, pady=(8, 0))
        self.sent = tk.Text(self, height=11, wrap="word")
        self.sent.pack(fill="x", padx=10)
        self._show_digest()

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(fill="x", padx=10, pady=6)
        ttk.Label(self, text=tr("ai_answer")).pack(anchor=parent._sticky("w"), padx=10)
        self.answer = tk.Text(self, wrap="word")
        self.answer.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bottom, text=tr("copy"), command=self.copy).pack(side=side(), padx=4)
        ttk.Button(bottom, text=tr("save_txt"), command=self.save).pack(side=side(), padx=4)

    def _show_digest(self):
        self.digest = self.make_digest(self.redact.get())
        self.sent.config(state="normal")
        self.sent.delete("1.0", "end")
        self.sent.insert("1.0", self.digest)
        self.sent.config(state="disabled")

    def send(self):
        self.send_btn.config(state="disabled")
        self.progress.start(10)
        self.status.set(self.tr("ai_waiting"))
        system, prompt = ai_assist.build_prompt(self.kind, self.digest, self.lang.get())
        results = queue.Queue()

        def work():
            try:
                results.put((ai_assist.ask_ai(system, prompt, self.cfg), None))
            except Exception as exc:  # noqa: BLE001
                results.put((None, str(exc)))

        def poll():
            try:
                text, err = results.get_nowait()
            except queue.Empty:
                if self.winfo_exists():
                    self.after(200, poll)
                return
            self._done(text, err)

        threading.Thread(target=work, daemon=True).start()
        self.after(200, poll)

    def _done(self, text, error):
        if not self.winfo_exists():
            return
        self.progress.stop()
        self.send_btn.config(state="normal")
        if error:
            self.status.set(error)
            return
        self.status.set(self.tr("ai_done"))
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
    """Every copy of a duplicate group side by side, each with its own actions.
    Each action re-checks the copy (and a keeper) right before it acts."""

    def __init__(self, parent, group_idx):
        super().__init__(parent)
        self.parent = parent
        self.tr = parent.tr
        self.group_idx = group_idx
        self.geometry("960x430")
        self.frame = ttk.Frame(self)
        self.frame.pack(fill="both", expand=True, padx=10, pady=10)
        self._render()

    @property
    def group(self):
        return self.parent.dup_groups[self.group_idx]

    def _render(self):
        tr = self.tr
        for child in self.frame.winfo_children():
            child.destroy()
        g = self.group
        if g is None or len(g.files) < 2:
            self.title(tr("group_title", n=self.group_idx + 1, copies=len(g.files) if g else 1))
            ttk.Label(self.frame, text=tr("one_copy_left"), foreground="#2e7d32").pack(
                padx=10, pady=10)
            return
        self.title(tr("group_title", n=self.group_idx + 1, copies=len(g.files)))
        ttk.Label(self.frame, text=tr("group_hint"), foreground="#555", wraplength=900).pack(
            anchor=self.parent._sticky("w"))
        canvas = tk.Canvas(self.frame, highlightthickness=0)
        hsb = ttk.Scrollbar(self.frame, orient="horizontal", command=canvas.xview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(xscrollcommand=hsb.set)
        canvas.pack(side="top", fill="both", expand=True)
        hsb.pack(side="bottom", fill="x")
        files = list(g.files)
        if self.parent.rtl:
            files.reverse()
        for col, rec in enumerate(files):
            self._build_column(inner, col, rec, g)

    def _build_column(self, parent, col, rec, g):
        tr = self.tr
        card = ttk.Frame(parent, borderwidth=1, relief="solid", padding=10)
        card.grid(row=0, column=col, padx=6, pady=6, sticky="n")
        n = g.files.index(rec) + 1
        ttk.Label(card, text=tr("copy_n", n=n), font=("Segoe UI", 10, "bold")).pack(anchor="w")
        text = tk.Text(card, width=34, height=4, wrap="char", borderwidth=0)
        text.insert("1.0", self.parent.describe_path(rec.path))
        text.config(state="disabled")
        text.pack(pady=(4, 4))
        mtime = datetime.fromtimestamp(rec.mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M")
        ttk.Label(card, text=tr("size_mtime", size=human_size(rec.size), mtime=mtime),
                  foreground="#555").pack(anchor="w")
        links = g.links.get(rec.path)
        if links:
            ttk.Label(card, text=tr("also_linked", n=len(links)), foreground="#a15c00").pack(
                anchor="w")
        btns = ttk.Frame(card)
        btns.pack(pady=(8, 0))
        p = rec.path
        ttk.Button(btns, text=tr("act_quarantine"), command=lambda: self._quarantine(p)).grid(
            row=0, column=0, padx=3, pady=2)
        ttk.Button(btns, text=tr("act_delete"), command=lambda: self._delete(p)).grid(
            row=0, column=1, padx=3, pady=2)
        ttk.Button(btns, text=tr("act_rename"), command=lambda: self._rename(p)).grid(
            row=1, column=0, padx=3, pady=2)
        ttk.Button(btns, text=tr("act_move"), command=lambda: self._move(p)).grid(
            row=1, column=1, padx=3, pady=2)
        ttk.Button(btns, text=tr("act_show"),
                   command=lambda: _open_folder(os.path.dirname(p))).grid(
            row=2, column=0, columnspan=2, pady=2)

    def _run(self, path, work, after_ok):
        """Run an action in the background; update the group when it ends."""
        g = self.group

        def done(res):
            if isinstance(res, fa.ActionBlocked):
                if res.target_invalid and g is self.group:
                    g.files = [f for f in g.files if f.path != path]
                    self.parent.on_group_changed(self.group_idx)
                if self.winfo_exists():
                    messagebox.showwarning(self.tr("action_blocked"),
                                           self.tr(res.key, **res.params), parent=self)
                    self._render()
                self.parent._status("dup", res.key, **res.params)
                return
            after_ok(res)
            self.parent.on_group_changed(self.group_idx)
            if self.winfo_exists():
                self._render()

        self.parent._start_job("dup", lambda progress, cancel: (
            progress("status_checking", {"path": os.path.basename(path)}) or work(cancel)), done)

    def _drop(self, path):
        g = self.group
        g.files = [f for f in g.files if f.path != path]
        g.links.pop(path, None)

    def _replace(self, path, new_path):
        g = self.group
        rec = g.rec(path)
        rec.path = new_path
        if path in g.links:
            g.links[new_path] = g.links.pop(path)

    def _quarantine(self, path):
        tr = self.tr
        if not messagebox.askyesno(tr("confirm"), tr("confirm_quarantine", path=path), parent=self):
            return
        root = self.parent.dup_rootmap.root_of(path) or os.path.dirname(path)
        g, session = self.group, self.parent.session

        def ok(dest):
            self._drop(path)
            self.parent._status("dup", "done_quarantine", path=dest)

        self._run(path, lambda cancel: fa.quarantine(g, path, root, session, cancel), ok)

    def _delete(self, path):
        tr = self.tr
        if not messagebox.askyesno(tr("confirm"), tr("confirm_delete", path=path),
                                   icon="warning", parent=self):
            return
        g = self.group

        def ok(_res):
            self._drop(path)
            self.parent._status("dup", "done_delete", path=path)

        self._run(path, lambda cancel: fa.delete_permanently(g, path, cancel), ok)

    def _rename(self, path):
        tr = self.tr
        old = os.path.basename(path)
        new_name = simpledialog.askstring(tr("act_rename"), tr("new_name"), initialvalue=old,
                                          parent=self)
        if not new_name or new_name == old:
            return
        try:
            fa.validate_basename(new_name)
        except fa.ActionBlocked as exc:
            messagebox.showerror(tr("action_blocked"), tr(exc.key, **exc.params), parent=self)
            return
        g = self.group

        def ok(new_path):
            self._replace(path, new_path)
            self.parent._status("dup", "done_rename", path=new_path)

        self._run(path, lambda cancel: fa.rename(g, path, new_name, cancel), ok)

    def _move(self, path):
        dest_dir = filedialog.askdirectory(title=self.tr("act_move"), parent=self)
        if not dest_dir:
            return
        g = self.group

        def ok(new_path):
            self._replace(path, new_path)
            self.parent._status("dup", "done_move", path=new_path)

        self._run(path, lambda cancel: fa.move(g, path, dest_dir, cancel), ok)


class QuarantineWindow(tk.Toplevel):
    """Files this app put in quarantine (from the journal), with Restore."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent, self.tr = parent, parent.tr
        tr = self.tr
        self.title(tr("menu_quarantine"))
        self.geometry("900x420")
        ttk.Label(self, text=tr("quarantine_hint", path=fa.JOURNAL_PATH), wraplength=860,
                  foreground="#555").pack(fill="x", padx=10, pady=(10, 4))
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10)
        self.tree = ttk.Treeview(frame, columns=("time", "path", "to"), show="headings")
        for col, label, w in (("time", tr("col_time"), 140), ("path", tr("col_original"), 360),
                              ("to", tr("col_quarantined"), 360)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=w)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=10)
        ttk.Button(btns, text=tr("restore"), command=self.restore).pack(side=parent._side(), padx=4)
        ttk.Button(btns, text=tr("act_show"), command=self.show).pack(side=parent._side(), padx=4)
        self.load()

    def load(self):
        self.entries = fa.pending_quarantine()
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(self.entries):
            self.tree.insert("", "end", iid=str(i), values=(e.get("time", ""), e["path"], e["to"]))

    def _selected(self):
        sel = self.tree.selection()
        return [self.entries[int(i)] for i in sel]

    def restore(self):
        for e in self._selected():
            try:
                fa.restore(e)
            except fa.ActionBlocked as exc:
                messagebox.showwarning(self.tr("action_blocked"), self.tr(exc.key, **exc.params),
                                       parent=self)
        self.load()

    def show(self):
        for e in self._selected()[:1]:
            _open_folder(os.path.dirname(e["to"]))


if __name__ == "__main__":
    App().mainloop()
