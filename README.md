# DupliCompare

A free, cross-platform GUI app that:
- compares two folders recursively by file name and size and generates an
  HTML diff report, and
- scans any folder for duplicate files (by size + MD5 content hash) and
  lets you review each duplicate group side by side — delete, rename, or
  move any copy right from the app.

Landing page with download links: https://duplicompare.mbm-group.se (see
[index.html](index.html)).

## Run from source (any OS with Python 3)

```
python drive_compare.py
```

Tkinter ships with standard Python on Windows/macOS. On Linux you may need:
```
sudo apt install python3-tk
```

## Build a standalone executable yourself

```
pip install pyinstaller
pyinstaller --onefile --windowed --name DupliCompare --icon assets/icon.ico drive_compare.py
```
The result is in `dist/`. PyInstaller only builds for the OS it runs on
(a Windows build machine makes a `.exe`, macOS makes a `.app`/binary, Linux
makes an ELF binary) — it cannot cross-compile.

## Get builds for all 3 platforms without owning all 3 machines

Push this repo to GitHub. The included `.github/workflows/build.yml` will
automatically build Windows, macOS, and Linux executables on every push to
`main` (or via "Run workflow" in the Actions tab), and attach them as
downloadable Artifacts on that workflow run. Pushing a tag like `v1.0.0`
additionally publishes those builds to a GitHub Release, so the static
download links on the landing page (`releases/latest/download/...`) work.

## Usage

1. Launch the app.
2. **Compare tab**: pick "Folder A" and "Folder B", click Compare. A report
   *folder* is written to your Desktop (`compare-report-<time>/`) and its
   `index.html` opens in the browser. Tabs: folders only in A / only in B
   (whole missing folders are one row, biggest first), files missing on one
   side, and files whose size differs. Every tab has search, sorting, paging
   and "Download CSV"; the same rows are also saved as `.csv` files next to
   `index.html` (for Excel or for feeding to a script/AI).
3. **Duplicates tab**: add any number of folders ("Add folder..."), set the
   type filter, minimum size and threads (HDD: 2-4, SSD: 8+) and click
   "Scan for Duplicates". Duplicates are found across all folders together;
   the "In folders" column says which folders ([1], [2]...) hold copies, and
   "Show" filters to groups across different folders or inside one folder.
   Double-click a group to see every copy side by side and delete / rename /
   move any of them. "Export HTML Report" writes the same kind of report
   folder (tabs: across folders / inside one folder).

4. **AI summary / advice** (optional, Compare tab and Duplicates tab): turns the
   last result into a short summary and an ordered clean-up plan, in English,
   Svenska or فارسی. It shows exactly what it will send (counts, sizes, folder
   and file names, never file contents), only sends when you press "Send to AI",
   and uses mbm-ai in `private` mode (local model only). The AI only advises;
   you do the deleting. It is **off until configured**, because the app is
   public and ships no key: put `{"url": "http://127.0.0.1:8031", "key": "..."}`
   in `~/.duplicompare/ai.json` (or set `DUPLICOMPARE_MBMAI_URL` /
   `DUPLICOMPARE_MBMAI_KEY`) and keep an SSH tunnel open
   (`ssh -N -L 8031:127.0.0.1:8031 deploy@<vps>`). The key must be a *private*
   mbm-ai key; the shared tools key is public-only and gets a 403.

Keep the `data/` folder next to `index.html` when you move or send a report.

## Speed notes

Scanning uses `os.scandir` (sizes come with the listing), duplicates use a
size -> head+tail hash -> full hash pipeline (BLAKE2b, 4 MB reads, a few
threads). Empty files are ignored. On the 3-folder synthetic test (18k small
files + 13 x 16 MB) the scan went from 69 s to 12 s and compare from ~1.6 s to
0.25 s, with identical results. A C++ core was judged not worth it: the cost is
disk I/O, not Python.

## Deploying the landing page

The static landing page (`index.html` + `assets/`) is served from the VPS at
https://duplicompare.mbm-group.se (`static-sites` nginx container, see
`company-site/deploy/static-sites`). `.github/workflows/deploy-vps.yml` copies the
files there after the owner's OK in MBM Control (a push only runs the check job).

The workflow needs the repo secrets `VPS_HOST`, `VPS_USER` and `VPS_SSH_KEY`
(`owner-scripts/set-repo-secrets.sh DupliCompare`).
