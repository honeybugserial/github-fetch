import re
import sys
import subprocess
import requests
from pathlib import Path

# Usage: python gh_fetch.py <repo> <asset_regex> [output_dir]
# Example: python gh_fetch.py majd/ipatool "darwin-amd64$"
# Example: python gh_fetch.py https://github.com/majd/ipatool "\.exe$" C:\Tools

def parse_repo(arg):
    m = re.match(r"https://github\.com/([^/]+/[^/]+)", arg)
    return m.group(1) if m else arg.strip("/")

def fetch(repo, asset_regex, out_dir, run_after=False):
    api = f"https://api.github.com/repos/{repo}/releases"
    print(f"Fetching latest release from {repo}...")
    r = requests.get(api, timeout=20)
    r.raise_for_status()

    asset = None
    for release in r.json():
        for a in release.get("assets", []):
            if re.search(asset_regex, a["name"]):
                asset = a
                print(f"Found: {a['name']} ({release['tag_name']})")
                break
        if asset:
            break

    if not asset:
        print("No matching asset found.")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / asset["name"]
    print(f"Downloading to {out_path}...")

    dl = requests.get(asset["browser_download_url"], stream=True, timeout=60)
    dl.raise_for_status()
    total = int(dl.headers.get("content-length", 0))
    done = 0

    with open(out_path, "wb") as f:
        for chunk in dl.iter_content(65536):
            if chunk:
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = int(done / total * 100)
                    print(f"\r  {pct}%", end="", flush=True)

    print(f"\nDone: {out_path}")

    if run_after:
        print(f"Launching {out_path.name}...")
        subprocess.Popen([str(out_path)], creationflags=subprocess.CREATE_NEW_CONSOLE)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python gh_fetch.py <repo_or_url> <asset_regex> [output_dir] [--run]")
        print('Example: python gh_fetch.py majd/ipatool "darwin-amd64$"')
        print('Example: python gh_fetch.py sergiye/winUpdateMiniTool "\.exe$" . --run')
        sys.exit(1)

    repo      = parse_repo(sys.argv[1])
    pattern   = sys.argv[2]
    args      = sys.argv[3:]
    run_after = "--run" in args
    out_dir   = Path(next((a for a in args if not a.startswith("--")), "."))

    fetch(repo, pattern, out_dir, run_after)
