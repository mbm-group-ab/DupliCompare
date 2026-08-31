# DupliCompare

A free, cross-platform GUI app that:
- compares two folders recursively by file name and size and generates an
  HTML diff report, and
- scans any folder for duplicate files (by size + MD5 content hash) and
  lets you review each duplicate group side by side — delete, rename, or
  move any copy right from the app.

Landing page with download links: https://duplicompare.com (see
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

## Deploying the landing page to a server

The static landing page (`index.html` + `assets/`) ships with a
`Dockerfile`, `docker-compose.yml`, and a GitHub Actions workflow
(`.github/workflows/deploy.yml`) that deploys it over SSH on every push to
`main`.

**One-time setup:**
1. Point the domain's DNS A record at the server's IP.
2. In the GitHub repo, add these secrets (Settings → Secrets and variables
   → Actions):

   | Secret | Value |
   |---|---|
   | `VPS_HOST` | server IP/hostname |
   | `VPS_USER` | SSH deploy user |
   | `VPS_SSH_KEY` | that user's SSH private key |
   | `GH_PAT` | GitHub PAT with read access to this repo (used to clone on first deploy) |

3. Push to `main` (or run the workflow manually) — it clones/pulls the repo
   on the server and runs `docker compose up -d --build`.
4. On the server: `sudo cp deploy/nginx.conf /etc/nginx/conf.d/duplicompare.conf && sudo nginx -t && sudo systemctl reload nginx`
5. `sudo certbot --nginx -d duplicompare.com -d www.duplicompare.com` for
   HTTPS (only works once DNS has propagated). Certbot rewrites the conf
   file to add the SSL block — pull that live version back into
   `deploy/nginx.conf` afterwards so the repo matches reality.
