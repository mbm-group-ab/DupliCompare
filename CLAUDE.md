# DupliCompare (folder drive-compare-tool, repo mbm-group-ab/DupliCompare): rules for agents

Free, public, cross-platform Tkinter app (EN/SV/FA, version in Help > About): compares two folders (quick:
names and sizes; verified: also contents) into an HTML/CSV report, and finds duplicate files across folders,
with quarantine / delete / move / rename per copy. Read `README.md` first (usage, speed notes, AI part).

## Files
- `drive_compare.py`: the Tk GUI only. Worker threads never touch Tk; they post to a queue drained with `after`.
- `dc_engine.py`: scan, compare (quick/verified) and duplicate pipeline (size, head+tail hash, full BLAKE2b),
  error/coverage report, hardlink collapse, cancel. No Tk.
- `file_actions.py`: quarantine (default), permanent delete, verified move, basename-only rename, restore,
  journal `~/.duplicompare/journal.jsonl`.
- `dc_i18n.py`: every UI and report text in EN/SV/FA; `tests/test_i18n.py` fails on a missing key/placeholder.
- `report_writer.py`: the report folder (`index.html` + `data/` + formula-safe `.csv`).
- `ai_assist.py`: optional summary/advice through mbm-ai.
- `index.html`, `robots.txt`, `sitemap.xml`, `assets/`: landing page `duplicompare.mbm-group.se`, English only
  (root rule for non-app sites).

## Rules
- **Never delete, move or rename a user's file without an explicit click** in the duplicate group view. The AI only
  advises; it never acts on files.
- Every file action goes through `file_actions.py`, which re-checks the target (stat + identity + full hash) and,
  for remove/delete, a verified keeper first. Never call `os.remove`/`os.rename`/`shutil.move` on user files from
  the GUI. Quick compare must never say "identical"; only verified mode with full coverage can.
- New texts go in `dc_i18n.py` in all three languages. Tests use generated files only.
- **AI**: only through mbm-ai `/v1/generate`, always privacy `private` (local model). It sends counts, sizes and
  folder/file names, never file contents, and only after the user presses "Send to AI". The repo is public:
  never commit a key or a default key. The key comes from `~/.duplicompare/ai.json` or
  `DUPLICOMPARE_MBMAI_URL` / `DUPLICOMPARE_MBMAI_KEY`.
- Keep scanning fast: `os.scandir`, no per-file `stat` when the listing already gives the size; don't hash files
  whose size is unique.
- Build with PyInstaller (`DupliCompare.spec`); `build/` and `dist/` are not source.

## CI and deploy (public repo, GitHub-hosted runners are fine)
- `release.yml`: every push runs the tests and builds Windows/macOS/Linux; when `VERSION` has no tag yet it
  publishes the GitHub Release `v<VERSION>` (landing page links to `releases/latest/download/...`). Bump
  `VERSION` and `VERSION` in `drive_compare.py` together (a test checks).
- `deploy-vps.yml`: copies the landing page to the VPS `static-sites` container. A push runs the checks; the deploy
  waits for the owner's 🚀 in MBM Control. Never start a deploy yourself.
- Docs and comments in English. Git author: Mohammad Behdad Moudi <mbehdadm@gmail.com> (root rule).
