"""Stage the versioned Python tooling resources next to the desktop backend."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def stage(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    subprocess.run(
        ["uv", "build", "--wheel", "--python", sys.executable, "--out-dir", str(destination)],
        cwd=ROOT,
        check=True,
    )
    (wheel,) = destination.glob("*.whl")
    requirements = destination / "requirements.txt"
    subprocess.run(
        ["uv", "export", "--locked", "--only-group", "plugin-tools", "--no-emit-project",
         "--output-file", str(requirements)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    test_config = destination / "pytest.ini"
    shutil.copyfile(ROOT / "pytest.ini", test_config)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    hashes = {}
    for path in (wheel, requirements, test_config):
        with path.open("rb") as stream:
            hashes[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {
        "schema": 1,
        "commit": commit,
        "platform": sys.platform,
        "architecture": platform.machine().lower(),
        "python": platform.python_version(),
        "wheel": wheel.name,
        "sha256": hashes,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    stage(parser.parse_args().destination.resolve())
