#!/usr/bin/env python3
"""Download a release asset from a GitHub repo, verify it, and optionally run it.

If a release has no matching attached file, links written in the release notes are
checked too (some projects host their binaries elsewhere and just link to them).

Set GITHUB_TOKEN (or GH_TOKEN) to raise the API rate limit from 60 to 5000 requests/hour.
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
import requests
import rich  # only used for the banner; see bananaer()

__version__ = "1.3.1"

API = "https://api.github.com"

# When bundled with PyInstaller, sys.frozen is set and __file__ points inside the
# temporary extraction folder, so use the .exe's own location instead.
FROZEN = getattr(sys, "frozen", False)
SCRIPT_DIR = Path(sys.executable if FROZEN else __file__).resolve().parent

ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2",
                    ".7z", ".gz", ".xz", ".bz2", ".dmg", ".pkg", ".deb", ".rpm")

URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`]+")
SHA256_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")


class FetchError(Exception):
    """An expected failure with a message suitable for the user."""


def parse_repo(arg):
    """Accept owner/name, github.com/owner/name, or a full URL (with or without .git)."""
    arg = arg.strip()
    m = re.match(r"(?:https?://)?(?:www\.)?github\.com/([^/]+/[^/?#]+)", arg)
    repo = (m.group(1) if m else arg).strip("/")
    repo = re.sub(r"\.git$", "", repo)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise FetchError(f"Not a valid repo: {arg!r} (expected owner/name or a GitHub URL)")
    return repo


def make_session():
    s = requests.Session()
    s.headers["Accept"] = "application/vnd.github+json"
    s.headers["X-GitHub-Api-Version"] = "2022-11-28"
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def hash_from_notes(body, name):
    """Find a SHA-256 on the same line as the file name.

    Covers "name, hash", "name: hash" and sha256sum-style "hash  name" lines.
    """
    name_re = re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])")
    for line in body.splitlines():
        if name_re.search(line):
            m = SHA256_RE.search(line)
            if m:
                return "sha256:" + m.group(0).lower()
    return None


def links_from_notes(body):
    """Yield asset-like dicts for download links written in a release's notes."""
    body = body or ""
    seen = set()
    for url in URL_RE.findall(body):
        url = url.rstrip(".,;:!*")
        parsed = urlparse(url)
        name = unquote(Path(parsed.path).name)
        # Only links that look like files: skip pages like /pull/123 or /releases/tag/v1.2.3.
        if "." not in name or url in seen:
            continue
        if parsed.netloc.endswith("github.com") and "/releases/download/" not in parsed.path:
            continue
        seen.add(url)
        yield {"name": name, "browser_download_url": url, "size": None,
               "digest": hash_from_notes(body, name), "external": True}


def find_asset(s, repo, pattern, include_prerelease=False):
    """Return (release, asset, newest_tag) for the newest release with a matching asset.

    Attached files are preferred; links in the release notes are the fallback.
    """
    r = s.get(f"{API}/repos/{repo}/releases", params={"per_page": 100}, timeout=20)
    if r.status_code == 404:
        raise FetchError(f"Repo not found (or private): {repo}")
    if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
        raise FetchError("GitHub API rate limit reached. Set GITHUB_TOKEN to raise the limit.")
    r.raise_for_status()

    newest_tag = None
    newest_names = []                                          # NEW
    for release in r.json():
        if release.get("draft") or (release.get("prerelease") and not include_prerelease):
            continue
        links = links_from_notes(release.get("body"))         # NEW
        if newest_tag is None:                                 # CHANGED
            newest_tag = release["tag_name"]
            newest_names = ([a["name"] for a in release.get("assets", [])]   # NEW
                            + [l["name"] for l in links])
        for asset in release.get("assets", []):
            if pattern.search(asset["name"]):
                return release, asset, newest_tag
        for link in links:                                     # CHANGED
            if pattern.search(link["name"]):
                return release, link, newest_tag

    kind = "releases" if include_prerelease else "stable releases"
    msg = (f"No asset or release-notes link matching {pattern.pattern!r} "
           f"in the {kind} of {repo}.")
    if newest_tag is None:                                     # NEW
        msg += f" (No {kind} found at all.)"
    elif newest_names:
        msg += f"\nFiles in {newest_tag}:\n  " + "\n  ".join(newest_names)
    else:
        msg += f"\n{newest_tag} has no attached files or download links."
    raise FetchError(msg)

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def existing_matches(path, asset, expected):
    """Return (matches, verified) for a file already on disk."""
    if not path.is_file():
        return False, False
    size = asset.get("size")
    if size is not None and path.stat().st_size != size:
        print(f"  Existing {path.name} is {path.stat().st_size} bytes, release is {size}; re-downloading.")
        return False, False
    if expected:
        print(f"  Checking existing {path.name} against the published SHA-256...")
        if sha256_file(path) == expected:
            return True, True
        print("  Existing file does not match the published checksum; re-downloading.")
        return False, False
    return size is not None, False  # size matched, no checksum available


def download(s, asset, out_dir, require_checksum=False, skip_existing=False):
    """Stream the asset to a .part file, verify it, then move it into place."""
    name = Path(asset["name"]).name  # never let an asset name escape out_dir
    out_path = out_dir / name
    part_path = out_dir / (name + ".part")
    url = asset["browser_download_url"]
    external = asset.get("external", False)

    digest = (asset.get("digest") or "").lower()
    expected = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
    if require_checksum and not expected:
        raise FetchError(f"{name} has no published SHA-256 digest (--require-checksum is set).")

    if skip_existing:
        matches, verified = existing_matches(out_path, asset, expected)
        if matches:
            how = "SHA-256 verified" if verified else "size matches, no checksum published"
            print(f"Already up to date: {out_path} ({how}); skipping download.")
            return out_path, verified

    if not out_dir.exists():
        print(f"Creating {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    if external:
        print(f"  Link from release notes: {url}")
    print(f"Downloading to {out_path}...")

    # Links from release notes can point at any host, so they're fetched without
    # the session (and its GitHub token).
    get = requests.get if external else s.get

    sha = hashlib.sha256()
    try:
        with get(url, stream=True, timeout=60,
                 headers={"Accept": "application/octet-stream"}) as dl:
            dl.raise_for_status()
            total = int(dl.headers.get("content-length") or 0)
            encoded = bool(dl.headers.get("content-encoding"))
            done = 0
            last_pct = -1
            with open(part_path, "wb") as f:
                for chunk in dl.iter_content(65536):
                    f.write(chunk)
                    sha.update(chunk)
                    done += len(chunk)
                    if total and not encoded:
                        pct = done * 100 // total
                        if pct != last_pct:  # redraw only when the number changes
                            print(f"\r  {pct:3d}%", end="", flush=True)
                            last_pct = pct
            print()

        if total and not encoded and done != total:
            raise FetchError(f"Download incomplete: got {done} of {total} bytes.")

        source = " (hash taken from the release notes)" if external else ""
        if expected:
            if sha.hexdigest() != expected:
                raise FetchError(f"Checksum mismatch for {name}; the file was discarded.")
            print(f"  SHA-256 verified{source}")
        else:
            print("  No checksum published for this asset; not verified")

        os.replace(part_path, out_path)
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise

    return out_path, bool(expected)


def launch(path):
    path = path.resolve()
    if path.name.lower().endswith(ARCHIVE_SUFFIXES):
        raise FetchError(f"{path.name} looks like an archive or installer package, "
                         "not something to run directly. It's been downloaded; open it manually.")
    print(f"Launching {path.name}...")
    if sys.platform == "win32":
        if path.suffix.lower() in (".exe", ".com", ".bat", ".cmd"):
            subprocess.Popen([str(path)], cwd=path.parent,
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            os.startfile(path)  # e.g. .msi: let Windows pick the handler
    else:
        path.chmod(path.stat().st_mode | 0o111)
        subprocess.Popen([str(path)], cwd=path.parent)

def bananaer():
    rich.print("[white] ▐█▀▀▀█   ▐█▀▀▀  ▐█▌  ▀▀█▀▀      ▐█▀▀▀█   ▐█▌  ▀▀█▀▀      ▐█▀▀▀█   ▐█▀▀▀  ▐█▀▀▀█   █▀▀▀█  [/]")
    rich.print("[green]█[/][white] █ [/][green]█▄[/][bright_green]▄▄█[/][green]█[/][white] █ [/][bright_green on green]▄▄[/][bright_green]█[/][green]█[/][white] █ [/][bright_green]█[/][bright_green on green]▄[/][green]█[/][white] █ [/][bright_green on green]▄[/][bright_green]█[/][white]    [/][green]█[/][white] █ [/][green]█▄[/][bright_green]▄▄█[/][green]█[/][white] █ [/][bright_green]█[/][bright_green on green]▄[/][green]█[/][white] █ [/][bright_green on green]▄[/][bright_green]█[/][white]    [/][green]█[/][white] █ [/][green]█[/][white] █ [/][bright_green]█[/][green]█[/][white] █ [/][bright_green on green]▄▄[/][bright_green]█[/][green]█[/][white] █ [/][green]█[/][white] █[/][bright_green]▐[/][green]█[/][white] █ [/][green]█[/][white] █ [/][bright_green]█[/]")
    rich.print("[green]█[/][white] █ ▄▄▄ [/][bright_green]█[/][green]█[/][white] █ [/][bright_green]▀▀█[/][green]█[/][white] █ [/][bright_green]██[/][green]█[/][white] █ [/][bright_green]██[/][white]    [/][green]█[/][white] █ ▄▄▄ [/][bright_green]█[/][green]█[/][white] █ [/][bright_green]██[/][green]█[/][white] █ [/][bright_green]██[/][white]    [/][green]█[/][white] █ [/][green]▀[/][white] █ [/][bright_green]█[/][green]█[/][white] █ [/][bright_green]▀▀█[/][green]█[/][white] █ [/][green]▀[/][white] █[/][bright_green]▐[/][green]█[/][white] █ [/][green]█[/][white] █ [/][bright_green]█[/]")
    rich.print("[green]█[/][white] █ [/][green]▄[/][white] █ [/][bright_green]█[/][green]█[/][white] █▀▀ [/][bright_green]█[/][green]█[/][white] █ [/][bright_green]██[/][green]█[/][white] █ [/][bright_green]██[/][white]    [/][green]█[/][white] █ [/][green]▄[/][white] █ [/][bright_green]█[/][green]█[/][white] █ [/][bright_green]██[/][green]█[/][white] █ [/][bright_green]██[/][white]    [/][green]█[/][white] █▀▀█▀ [/][bright_green]█[/][green]█[/][white] █▀▀ [/][bright_green]█[/][green]█[/][white] █▀▀▀▀[/][bright_green]▐[/][green]█[/][white] █ [/][green]█[/][white] █ [/][bright_green]█[/]")
    rich.print("[green]█[/][white] [/][bright_black on white]░[/][white] [/][green]█[/][white] [/][bright_black on white]░[/][white] [/][bright_green]█[/][green]█[/][white] [/][bright_black on white]░[/][white] [/][bright_green on green]▄▄[/][bright_green]█[/][green]█[/][white] [/][bright_black on white]░[/][white] [/][bright_green]██[/][green]█[/][white] [/][bright_black on white]░[/][white] [/][bright_green]██[/][white]    [/][green]█[/][white] [/][bright_black on white]░[/][white] [/][green]█[/][white] [/][bright_black on white]░[/][white] [/][bright_green]█[/][green]█[/][white] [/][bright_black on white]░[/][white] [/][bright_green]██[/][green]█[/][white] [/][bright_black on white]░[/][white] [/][bright_green]██[/][white]    [/][green]█[/][white] [/][bright_black on white]░[/][white] [/][green]▄[/][white] [/][bright_black on white]░[/][white] [/][bright_green]█[/][green]█[/][white] [/][bright_black on white]░[/][white] [/][bright_green on green]▄▄[/][bright_green]█[/][green]█[/][white] [/][bright_black on white]░[/][white] [/][bright_green on green]▄▄▄[/][bright_green]█[/][green]█[/][white] [/][bright_black on white]░[/][white] [/][green]█[/][white] [/][bright_black on white]░[/][white] [/][bright_green]█[/]")
    rich.print("[green]█[/][white] [/][bright_black on white]▒[/][white] [/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][bright_green]█[/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][bright_green]███[/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][bright_green]██[/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][bright_green]██[/][white]    [/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][bright_green]█[/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][bright_green]██[/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][bright_green]██[/][white]    [/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][bright_green]█[/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][bright_green]███[/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][bright_green]████[/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][green]█[/][white] [/][bright_black on white]▒[/][white] [/][bright_green]█[/]")
    rich.print("[white] [/][bright_black]▐[/][bright_black on white]▓[/][bright_black]▄▄▄[/][bright_black on white]▓[/][white]   [/][bright_black]▐[/][bright_black on white]▓[/][bright_black]▄▄▄[/][white]  [/][bright_black]▐[/][bright_black on white]▓[/][bright_black]▌[/][white]   [/][bright_black]▐[/][bright_black on white]▓[/][bright_black]▌[/][white]       [/][bright_black]▐[/][bright_black on white]▓[/][bright_black]▄▄▄[/][bright_black on white]▓[/][white]   [/][bright_black]▐[/][bright_black on white]▓[/][bright_black]▌[/][white]   [/][bright_black]▐[/][bright_black on white]▓[/][bright_black]▌[/][white]       [/][bright_black]▐[/][bright_black on white]▓[/][bright_black]▌[/][white] [/][bright_black]▐[/][bright_black on white]▓[/][bright_black]▌[/][white]  [/][bright_black]▐[/][bright_black on white]▓[/][bright_black]▄▄▄[/][white]  [/][bright_black]▐[/][bright_black on white]▓[/][bright_black]▌[/][white]      [/][bright_black on white]▓[/][bright_black]▄▄▄[/][bright_black on white]▓[/][white]  [/]")

EXAMPLES = r"""
examples:
  Real repos:
    gh_fetch majd/ipatool "macos-amd64"
    gh_fetch https://github.com/majd/ipatool "\.exe$" C:\Tools
    gh_fetch sergiye/winUpdateMiniTool "\.exe$" . --run
    gh_fetch DrDonk/unlocker "\.zip$" --skip-existing
    gh_fetch monero-project/monero-gui "win-x64.*\.zip$"   (link in release notes)

  Grab the only .exe in the latest release:
    gh_fetch owner/repo "\.exe$"

  Partial match: a .zip with "x64" anywhere in the name:
    gh_fetch owner/repo "x64.*\.zip$"
      matches: tool-1.4.2-win-x64.zip
      skips:   tool-1.4.2-win-arm64.zip, tool-1.4.2-win-x64.zip.sha256

  Windows + 64-bit + zip, in any order of the other bits:
    gh_fetch owner/repo "(?i)win.*(x64|amd64|x86_64).*\.zip$"

  Either an installer .exe or an .msi:
    gh_fetch owner/repo "\.(exe|msi)$"

  Setup exe only, not the "portable" one:
    gh_fetch owner/repo "^(?!.*portable).*setup.*\.exe$"

  Save to a folder and run it right away:
    gh_fetch owner/repo "setup.*\.exe$" C:\Downloads --run

  Use a full GitHub URL, include pre-releases:
    gh_fetch https://github.com/owner/repo "\.zip$" --prerelease

regex cheat sheet (Python 're', matched anywhere in the file name):
  .          any single character        \.     a literal dot
  .*         anything (or nothing)       $      end of the name
  ^          start of the name           (?i)   ignore upper/lower case
  (a|b)      a or b                      [0-9]+ one or more digits
  (?!.*foo)  at the start: name must NOT contain "foo"

  Tips:
  - End with $ (e.g. "\.zip$") so ".zip.sha256" / ".zip.sig" files don't match.
  - Always escape the dot: "\.exe", not ".exe" (plain . matches any char).
  - Put the regex in double quotes so the shell leaves | ( ) $ alone.
  - Not sure what the files are called? Use a pattern that matches
    nothing ("zzz") and look at the error, or check the repo's Releases page.
"""

def main():
    bananaer()
    p = argparse.ArgumentParser(
        description=f"gh_fetch {__version__}: Download a release asset from GitHub.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("repo", help="owner/name or a GitHub URL")
    p.add_argument("asset_regex",
                   help="regex matched against asset file names (or file names in release-notes links)")
    p.add_argument("output_dir", nargs="?", default=".", type=Path,
                   help="where to save the file (default: current directory)")
    p.add_argument("--run", action="store_true", help="run the file after downloading")
    p.add_argument("--prerelease", action="store_true", help="also consider pre-releases")
    p.add_argument("--skip-existing", action="store_true",
                   help="don't re-download if the file is already there and matches the release")
    p.add_argument("--require-checksum", action="store_true",
                   help="refuse assets without a published SHA-256 digest")
    args = p.parse_args()

    try:
        pattern = re.compile(args.asset_regex)
    except re.error as e:
        p.error(f"invalid asset_regex: {e}")

    try:
        repo = parse_repo(args.repo)
        s = make_session()
        print(f"Looking up releases for {repo}...")
        release, asset, newest_tag = find_asset(s, repo, pattern, args.prerelease)

        note = ""
        if asset.get("external"):
            note += f"  [linked from release notes, {urlparse(asset['browser_download_url']).netloc}]"
        if release["tag_name"] != newest_tag:
            note += f"  [note: newest release {newest_tag} has no matching asset]"
        print(f"Found: {asset['name']} ({release['tag_name']}){note}")

        out_path, verified = download(s, asset, args.output_dir, args.require_checksum,
                                      args.skip_existing)
        print(f"Done: {out_path}")

        if args.run:
            if not verified:
                print("Warning: running a file whose checksum could not be verified.")
            launch(out_path)
    except FetchError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except requests.RequestException as e:
        print(f"\nNetwork error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())