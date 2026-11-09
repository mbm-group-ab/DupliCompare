"""
Optional AI help for DupliCompare: turns a compare or duplicates result into
a short summary and advice, through the MBM AI service (mbm-ai).

- Off unless configured. The app is public, so no key is shipped in it:
  put the URL and key in the environment (DUPLICOMPARE_MBMAI_URL,
  DUPLICOMPARE_MBMAI_KEY) or in ~/.duplicompare/ai.json
  {"url": "http://127.0.0.1:8031", "key": "..."}.
  mbm-ai has no public port: open an SSH tunnel to the VPS loopback first
  (ssh -N -L 8031:127.0.0.1:8031 deploy@<vps>).
- Only a compact digest is sent (counts, sizes, folder and file names; never
  file contents) and always as privacy "private", so mbm-ai answers with its
  local model and nothing reaches an outside AI provider.
"""

import json
import os
import urllib.error
import urllib.request
from collections import Counter, defaultdict

DEFAULT_URL = "http://127.0.0.1:8031"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".duplicompare", "ai.json")
TOP = 15
MAX_PATH_CHARS = 120
MAX_DIGEST_CHARS = 9000   # the local model has a small context window

LANGUAGES = {"English": "English", "Svenska": "Swedish", "فارسی": "Persian (Farsi)"}


class AiError(Exception):
    pass


def load_config():
    """Return {"url", "key"} or None when AI is not configured."""
    url = os.environ.get("DUPLICOMPARE_MBMAI_URL", "").strip()
    key = os.environ.get("DUPLICOMPARE_MBMAI_KEY", "").strip()
    if not key:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            key = str(data.get("key", "")).strip()
            url = url or str(data.get("url", "")).strip()
        except (OSError, ValueError):
            pass
    if not key:
        return None
    return {"url": (url or DEFAULT_URL).rstrip("/"), "key": key}


SETUP_HELP = (
    "AI help is not set up on this computer.\n\n"
    "It uses the MBM AI service (mbm-ai). Put your key in\n"
    f"{CONFIG_PATH}\n"
    '{"url": "http://127.0.0.1:8031", "key": "<your mbm-ai key>"}\n'
    "(or set DUPLICOMPARE_MBMAI_KEY / DUPLICOMPARE_MBMAI_URL), and keep an SSH "
    "tunnel to the server open: ssh -N -L 8031:127.0.0.1:8031 deploy@<vps>.\n\n"
    "The key must be allowed to send private data: only a summary of names and "
    "sizes is sent, and only the local model sees it."
)


def _short(path):
    path = str(path)
    if len(path) <= MAX_PATH_CHARS:
        return path
    return path[:40] + " ... " + path[-(MAX_PATH_CHARS - 46):]


def _human(n):
    if n is None or n < 0:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _top_prefix(rel, depth=2):
    parts = rel.replace("\\", "/").split("/")[:-1]
    return "/".join(parts[:depth]) or "(root)"


def _ext(name):
    return os.path.splitext(name)[1].lower() or "(none)"


def _cap(text):
    if len(text) <= MAX_DIGEST_CHARS:
        return text
    return text[:MAX_DIGEST_CHARS] + "\n[digest cut]"


def build_compare_digest(path_a, path_b, summary, result):
    lines = [
        "COMPARE of two folders (by relative path and size).",
        f"A = {_short(path_a)}",
        f"B = {_short(path_b)}",
        f"Files: A {summary['a_files']}, B {summary['b_files']}; size A {_human(summary['a_size'])}, B {_human(summary['b_size'])}",
        f"Only in A: {summary['only_a']} files ({_human(summary['only_a_size'])}); "
        f"only in B: {summary['only_b']} files ({_human(summary['only_b_size'])}); "
        f"same path but different size: {summary['mismatch']}",
    ]
    for label, key in (("Whole folders only in A", "folders_only_a"),
                       ("Whole folders only in B", "folders_only_b")):
        rows = result[key]
        lines.append(f"\n{label} ({len(rows)}), biggest first:")
        for path, files, size in rows[:TOP]:
            lines.append(f"- {_short(path)}: {files} files, {_human(size)}")
        if len(rows) > TOP:
            lines.append(f"- ... {len(rows) - TOP} more")
    for label, key in (("Loose files missing in B (folder exists in both)", "files_only_a"),
                       ("Loose files missing in A (folder exists in both)", "files_only_b")):
        rows = result[key]
        lines.append(f"\n{label}: {len(rows)}")
        if rows:
            by_dir = Counter(_top_prefix(r) for r, _s in rows)
            by_ext = Counter(_ext(r) for r, _s in rows)
            lines.append("  by folder: " + "; ".join(f"{d} ({n})" for d, n in by_dir.most_common(10)))
            lines.append("  by type: " + ", ".join(f"{e} {n}" for e, n in by_ext.most_common(8)))
    rows = result["mismatch"]
    if rows:
        lines.append(f"\nSize differs ({len(rows)}), largest gap first:")
        for path, sa, sb in sorted(rows, key=lambda r: -abs(r[1] - r[2]))[:10]:
            lines.append(f"- {_short(path)}: A {_human(sa)} vs B {_human(sb)}")
    return _cap("\n".join(lines))


def build_duplicates_digest(rootmap, groups):
    lines = ["DUPLICATE FILES (identical size and content) across the scanned folders."]
    lines += [f"{rootmap.tag(i)} = {_short(p)}" for i, p in enumerate(rootmap.roots)]

    rows = []
    for paths in groups:
        try:
            size = os.path.getsize(paths[0])
        except OSError:
            size = 0
        rows.append((size * (len(paths) - 1), size, paths, rootmap.where(paths), rootmap.is_cross(paths)))
    rows.sort(key=lambda r: -r[0])
    wasted = sum(r[0] for r in rows)
    cross = [r for r in rows if r[4]]
    lines.append(f"Groups: {len(rows)}; wasted space by extra copies: {_human(wasted)}; "
                 f"groups across different folders: {len(cross)}; inside one folder: {len(rows) - len(cross)}")

    by_where = defaultdict(lambda: [0, 0])
    for w, _s, _p, where, _c in rows:
        by_where[where][0] += 1
        by_where[where][1] += w
    lines.append("\nWasted space by which folders hold the copies:")
    for where, (n, w) in sorted(by_where.items(), key=lambda kv: -kv[1][1])[:10]:
        lines.append(f"- {where}: {n} groups, {_human(w)}")

    by_dir = defaultdict(lambda: [0, 0])
    for w, _s, paths, _wh, _c in rows:
        for p in paths[1:]:
            d = rootmap.describe(os.path.dirname(p))
            by_dir[d][0] += 1
            by_dir[d][1] += w // max(len(paths) - 1, 1)
    lines.append("\nDirectories holding the most extra copies:")
    for d, (n, w) in sorted(by_dir.items(), key=lambda kv: -kv[1][1])[:10]:
        lines.append(f"- {_short(d)}: {n} extra copies, about {_human(w)}")

    exts = Counter()
    for _w, _s, paths, _wh, _c in rows:
        exts[_ext(paths[0])] += 1
    lines.append("\nDuplicate groups by type: " + ", ".join(f"{e} {n}" for e, n in exts.most_common(8)))

    lines.append(f"\nBiggest groups by wasted space (top {TOP}):")
    for w, size, paths, where, _c in rows[:TOP]:
        shown = "; ".join(_short(rootmap.describe(p)) for p in paths[:3])
        more = f" (+{len(paths) - 3} more)" if len(paths) > 3 else ""
        lines.append(f"- {_human(size)} x {len(paths)} in {where}: {shown}{more}")
    return _cap("\n".join(lines))


def build_prompt(kind, digest, language):
    lang = LANGUAGES.get(language, "English")
    system = (
        "You help a person tidy their files. You get a compact digest of a folder "
        "comparison or of duplicate files (names, sizes, counts; never contents). "
        "Be practical and honest: only use what is in the digest, say when something "
        "cannot be known from it, and never claim that anything is safe to delete without "
        "the person checking. The person does the deleting; you only advise. "
        f"Answer in {lang}, in plain text without markdown tables."
    )
    if kind == "compare":
        ask = (
            "1) Summarise in a few lines how the two folders differ. "
            "2) Say what looks like a missing backup or a copy that is out of date. "
            "3) Give a short, ordered list of what to do next (what to copy where, what to check first). "
            "4) List any risks or things to verify before changing anything."
        )
    else:
        ask = (
            "1) Summarise in a few lines where the duplicates are and how much space they waste. "
            "2) Suggest which folder looks like the keeper and which looks like the extra copy, "
            "and why, when the digest allows a guess. "
            "3) Give a short, ordered cleanup plan starting with the biggest wins. "
            "4) List what to verify before deleting (e.g. open the group, check dates)."
        )
    return system, f"{ask}\n\n--- DIGEST ---\n{digest}"


def ask_ai(system, prompt, cfg, timeout=420):
    """Call mbm-ai /v1/generate (private) and return the answer text."""
    body = json.dumps({
        "prompt": prompt, "system": system, "privacy": "private",
        "quality": "auto", "max_tokens": 1500,
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg["url"] + "/v1/generate", data=body, method="POST",
        headers={"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
        except (ValueError, OSError):
            pass
        hints = {
            401: "the key was not accepted",
            403: "this key may not send private data or use /v1/generate",
            429: "daily quota reached",
            503: "the local AI model is not available right now",
        }
        raise AiError(f"mbm-ai said {exc.code}: {hints.get(exc.code, detail) or detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AiError(
            f"Cannot reach mbm-ai at {cfg['url']} ({exc}). Is the SSH tunnel open?"
        ) from exc
    text = (data.get("text") or "").strip()
    if not text:
        raise AiError("mbm-ai returned an empty answer.")
    return text
