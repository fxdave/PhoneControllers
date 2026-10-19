#!/usr/bin/env python3
"""Writes python-deps.json, the Flatpak module with this app's Python dependencies.

Takes the versions installed in the environment it runs in (use the project's venv), so the
Flatpak gets what was tested. Wheels are pinned by hash from PyPI; compiled ones are fetched
for each architecture the Flatpak builds, and evdev is built from source against the SDK's
kernel headers.

    .venv/bin/python flatpak/generate-python-deps.py
"""

import json
import urllib.request
from importlib import metadata
from pathlib import Path

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name

TOP_LEVEL = ["aiohttp", "evdev", "qrcode"]
# The GNOME 49 runtime's Python.
PYTHON = "313"
ARCHES = {"x86_64": "x86_64", "aarch64": "aarch64"}
MARKER_ENVIRONMENT = {
    "python_version": "3.13", "python_full_version": "3.13.0", "sys_platform": "linux",
    "platform_system": "Linux", "platform_machine": "x86_64", "os_name": "posix",
    "implementation_name": "cpython", "platform_python_implementation": "CPython", "extra": "",
}


def closure(names):
    seen = {}
    pending = list(names)
    while pending:
        name = canonicalize_name(pending.pop())
        if name in seen:
            continue
        distribution = metadata.distribution(name)
        seen[name] = distribution.version
        for line in distribution.requires or []:
            requirement = Requirement(line)
            if requirement.marker and not requirement.marker.evaluate(MARKER_ENVIRONMENT):
                continue
            pending.append(requirement.name)
    return seen


def pypi_files(name, version):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json") as response:
        return json.load(response)["urls"]


def pick(name, version):
    """Sources for one package: a pure wheel, a wheel per architecture, or the sdist."""
    files = pypi_files(name, version)
    wheels = [f for f in files if f["packagetype"] == "bdist_wheel"]
    pure = [f for f in wheels if f["filename"].endswith("-py3-none-any.whl")]
    if pure:
        return [source(pure[0])]
    sources = []
    for flatpak_arch, wheel_arch in ARCHES.items():
        matches = [f for f in wheels
                   if f"-cp{PYTHON}-cp{PYTHON}-" in f["filename"]
                   and "manylinux" in f["filename"] and f["filename"].endswith(f"_{wheel_arch}.whl")]
        if not matches:
            break
        sources.append(source(sorted(matches, key=lambda f: f["filename"])[-1], flatpak_arch))
    else:
        return sources
    sdists = [f for f in files if f["packagetype"] == "sdist"]
    return [source(sdists[0])]


def source(file, arch=None):
    entry = {"type": "file", "url": file["url"], "sha256": file["digests"]["sha256"]}
    if arch:
        entry["only-arches"] = [arch]
    return entry


def main():
    packages = closure(TOP_LEVEL)
    sources = []
    for name, version in sorted(packages.items()):
        picked = pick(name, version)
        print(f"{name} {version}: {', '.join(Path(s['url']).name for s in picked)}")
        sources.extend(picked)
    module = {
        "name": "python-deps",
        "buildsystem": "simple",
        "build-commands": [
            "pip3 install --verbose --exists-action=i --no-index --find-links=\"file://${PWD}\" "
            "--prefix=${FLATPAK_DEST} --no-build-isolation " + " ".join(TOP_LEVEL),
        ],
        "sources": sources,
    }
    target = Path(__file__).with_name("python-deps.json")
    target.write_text(json.dumps(module, indent=2) + "\n")
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
