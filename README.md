# DupliCompare

A free, cross-platform desktop app (Windows, macOS, Linux) that:
- compares two folders: **quick** (names and sizes) or **verified** (also reads
  and compares the contents), and writes a searchable HTML report, and
- finds duplicate files across any number of folders (size, then head+tail
  hash, then full BLAKE2b hash) and lets you review each group side by side
  and quarantine, delete, move or rename one copy at a time, each after a
  fresh check.

The app is in English, Svenska and فارسی (right to left), and shows its
version under Help > About. Landing page with downloads:
https://duplicompare.mbm-group.se (see [index.html](index.html)).

## Run from source (any OS with Python 3)

```
python drive_compare.py
```

Tkinter ships with standard Python on Windows/macOS. On Linux you may need
`sudo apt install python3-tk`.

Tests (generated data only): `pip install pytest && python -m pytest -q tests`.

## Usage

1. **Compare tab**: pick Folder A and Folder B and a mode, then Compare.
   - *Quick* never reads file contents. Two files with the same path and size
     count as the same, so the result says "same names and sizes", never
     "identical".
   - *Verified* reads every same-size pair. The result is "identical" only when
     every file was read and matched; files that differ, could not be read or
     changed during the scan get their own tabs.
   A report folder (`compare-report-<time>/`) is written to your Desktop and
   its `index.html` opens. It has a coverage card and an Errors tab listing
   every file or folder that could not be read. Every tab has search,
   sorting, paging and CSV; the CSV files are formula-safe for Excel.
2. **Duplicates tab**: add folders, set the type filter, minimum size and
   threads (1 for a single hard disk, more for SSDs), then Scan. Cancel stops
   any running scan. Overlapping folders count each file once, and hardlinks
   (one file with several names) are shown apart, not as duplicates.
   Double-click a group to see every copy:
   - **Remove (quarantine)**, the default: moves the copy to
     `<scanned folder>/.duplicompare-quarantine/<session>/...` on the same
     disk. Tools > Quarantine lists these files and restores them.
   - **Delete permanently**: a separate action with its own warning.
   - **Move**: to another disk, the app copies, verifies the copy's hash and
     only then removes the source.
   - **Rename**: a file name only; to change folders use Move.
   Before any action the app re-checks the copy (still there, same size, time,
   identity and content) and, for remove/delete, that another verified copy is
   left. If anything changed, nothing happens and the reason is shown. Every
   action is logged in `~/.duplicompare/journal.jsonl`.
3. **AI advice** (optional): turns the last result into a short summary and
   clean-up plan in English, Svenska or فارسی. It shows exactly what it will
   send (counts, sizes, folder and file names, never contents; "Counts only"
   hides the names too), sends only when you press "Send to AI", and uses
   mbm-ai in `private` mode (local model). The answer is advice only. It is off
   until configured, because the app is public and ships no key: put
   `{"url": "http://127.0.0.1:8031", "key": "..."}` in `~/.duplicompare/ai.json`
   (or set `DUPLICOMPARE_MBMAI_URL` / `DUPLICOMPARE_MBMAI_KEY`) and keep an SSH
   tunnel open. Remote endpoints must be https; plain http works only to
   127.0.0.1. On macOS/Linux the key file is made owner-readable only.

Settings (language, compare mode, threads, type filter) are kept in
`~/.duplicompare/settings.json`. Keep the `data/` folder next to `index.html`
when you move or send a report.

## Files

- `drive_compare.py`: the Tk GUI. Worker threads post to a queue that the Tk
  loop drains, so the window never freezes.
- `dc_engine.py`: scanning, compare and duplicate search (no Tk).
- `file_actions.py`: checked quarantine / delete / move / rename / restore and
  the journal.
- `dc_i18n.py`: all texts in EN/SV/FA (`tests/test_i18n.py` checks keys and
  placeholders).
- `report_writer.py`: the report folder (`index.html` + `data/` + `.csv` + `report.json`, a machine-readable
  summary: tool, version, scanned folders, coverage (total, checked, errors, not checked, complete) and
  limitations).
- `ai_assist.py`: the optional AI helper.

## Speed notes

Scanning uses `os.scandir` (sizes come with the listing); only files whose
size is shared are stat'ed and hashed (64 KB head+tail first, full hash only
for files that still collide, 4 MB reads, a few threads). Empty files are
ignored.

## Releases

`VERSION` holds the app version (it must match `VERSION` in
`drive_compare.py`; a test checks). `.github/workflows/release.yml` runs the
tests and builds Windows/macOS/Linux on every push to `main`. When `VERSION`
names a version with no tag yet, it publishes the GitHub Release
`v<VERSION>` with `DupliCompare-windows.exe`, `DupliCompare-macos` and
`DupliCompare-linux`, which the landing page links to via
`releases/latest/download/...`. To release, bump `VERSION` in both places.

Build locally:
```
pip install pyinstaller
pyinstaller --onefile --windowed --name DupliCompare --icon assets/icon.ico --add-data "assets/icon.ico:assets" drive_compare.py
```
PyInstaller only builds for the OS it runs on.

## Deploying the landing page

The landing page (`index.html` + `assets/`) is served from the VPS at
https://duplicompare.mbm-group.se (`static-sites` nginx container, see
`company-site/deploy/static-sites`). `.github/workflows/deploy-vps.yml` copies
the files there after the owner's OK in MBM Control (a push only runs the
check job). It needs the repo secrets `VPS_HOST`, `VPS_USER` and `VPS_SSH_KEY`.
