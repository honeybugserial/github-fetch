# gh_fetch.py

A simple, lightweight Python script to **download the latest GitHub release asset** matching a regex pattern.

Perfect for quickly grabbing the newest version of tools, CLIs, or binaries (e.g. `ipatool`, `winUpdateMiniTool`, etc.) without manually navigating GitHub releases.

---

## Features

- Supports repo name (`owner/repo`) or full GitHub URL
- Regex-based asset selection (very flexible in theroy, unfortunatly no one actually uderstands regex)
- Progress bar during download (not really)
- Optional auto-run after download (Windows)

---
## Usage
```bash
python gh_fetch.py <repo> <asset_regex> [output_dir] [--run]

## Examples

```bash
python gh_fetch.py majd/ipatool "darwin-amd64$"
python gh_fetch.py rustdesk/rustdesk "x86_64\.exe$" --run

