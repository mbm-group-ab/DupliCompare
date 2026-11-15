# DupliCompare (folder drive-compare-tool, repo mbm-group-ab/DupliCompare): rules for agents

Free, public, cross-platform Tkinter app: compares two folders (HTML/CSV report) and finds duplicate files
across folders, with delete / rename / move per copy. Read `README.md` first (usage, speed notes, AI part).

## Files
- `drive_compare.py`: the GUI, scanning and duplicate pipeline (size, then head+tail hash, then full BLAKE2b hash).
- `report_writer.py`: the report folder (`index.html` + `data/` + `.csv`).
- `ai_assist.py`: optional summary/advice through mbm-ai.
- `index.html`, `robots.txt`, `sitemap.xml`, `assets/`: landing page `duplicompare.mbm-group.se`, English only
  (root rule for non-app sites).

## Rules
- **Never delete, move or rename a user's file without an explicit click** in the duplicate group view. The AI only
  advises; it never acts on files.
- **AI**: only through mbm-ai `/v1/generate`, always privacy `private` (local model). It sends counts, sizes and
  folder/file names, never file contents, and only after the user presses "Send to AI". The repo is public:
  never commit a key or a default key. The key comes from `~/.duplicompare/ai.json` or
  `DUPLICOMPARE_MBMAI_URL` / `DUPLICOMPARE_MBMAI_KEY`.
- Keep scanning fast: `os.scandir`, no per-file `stat` when the listing already gives the size; don't hash files
  whose size is unique.
- Build with PyInstaller (`DupliCompare.spec`); `build/` and `dist/` are not source.

## CI and deploy (public repo, GitHub-hosted runners are fine)
- `auto-tag.yml`: a push to `main` bumps a `vX.Y.Z` tag.
- `build.yml`: on a `v*.*.*` tag, builds Windows/macOS/Linux and publishes a GitHub Release (the landing page
  links to `releases/latest/download/...`).
- `deploy-vps.yml`: copies the landing page to the VPS `static-sites` container. A push runs the checks; the deploy
  waits for the owner's 🚀 in MBM Control. Never start a deploy yourself.
- Docs and comments in English. Git author: Mohammad Behdad Moudi <mbehdadm@gmail.com> (root rule).
