#!/usr/bin/env python3
"""Writes the manifest for the Flathub repository: the same as the local one, but the app's
source is this repository at a release tag instead of the working copy.

    python3 flatpak/make-flathub-manifest.py v0.1.0 out-dir

Run it after pushing the tag; it reads the tag's commit from git.
"""

import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "io.github.fxdave.PhoneControllers"
REPO_URL = "https://github.com/fxdave/PhoneControllers.git"
HERE = Path(__file__).resolve().parent


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__.strip())
        return 2
    tag, out = sys.argv[1], Path(sys.argv[2])
    commit = subprocess.run(["git", "rev-list", "-n", "1", tag], cwd=HERE, check=True,
                            capture_output=True, text=True).stdout.strip()
    manifest = (HERE / f"{APP_ID}.yml").read_text()
    local = manifest[manifest.index("    sources:\n"):]
    remote = (f"    sources:\n"
              f"      - type: git\n"
              f"        url: {REPO_URL}\n"
              f"        tag: {tag}\n"
              f"        commit: {commit}\n")
    # Drop the local build comments at the top; the Flathub repo builds from here.
    body = manifest[manifest.index("id: "):].replace(local, remote)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{APP_ID}.yml").write_text(body)
    shutil.copy(HERE / "python-deps.json", out / "python-deps.json")
    print(f"wrote {out / f'{APP_ID}.yml'} for {tag} ({commit[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
