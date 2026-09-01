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
2. **Compare tab**: pick "Folder A" and "Folder B", click Compare. An HTML
   report opens automatically (also saved to your Desktop) showing:
   - File/size summary for each folder
   - Every file missing from one side, extra on the other, or size-mismatched
3. **Duplicates tab**: pick a folder, click "Scan for Duplicates". Click any
   column header to sort; double-click a group (or "Open Group...") to see
   every copy side by side and delete/rename/move them.

## Deploying the landing page

The static landing page (`index.html` + `assets/`) deploys to **Firebase
Hosting**, site `duplicompare` in the `mbm-group-ab` Firebase project
(same project as `company-site`), via
`.github/workflows/deploy-firebase.yml` on every push to `main`.

- Firebase URL: https://duplicompare.web.app
- Custom domain: `duplicompare.mbm-group.se` (add as a custom domain in
  Firebase Hosting console for the `duplicompare` site, then point its DNS
  record — Firebase gives you the exact record to add — at Firebase).

**One-time setup:** the repo secret `FIREBASE_SERVICE_ACCOUNT` must hold
the same service-account JSON key used by `company-site` (it already has
the `firebase.sdkAdminServiceAgent` role on the `mbm-group-ab` project,
which covers Hosting deploys for every site in that project).
