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

## Installation

1. Download the script:

```bash
git clone repo
python gh_fetch.py
