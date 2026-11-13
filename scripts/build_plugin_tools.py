"""Stage the versioned Python tooling resources in the desktop backend."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def _workflow_ref(commit: str) -> str:
    from plugin.neko_plugin_cli.templates.generator import (
        MARKET_ACTIONS_NEKO_REF,
        MARKET_ACTIONS_NEKO_REPOSITORY,
    )

    url = (
        f"https://api.github.com/repos/{MARKET_ACTIONS_NEKO_REPOSITORY}"
        f"/compare/{commit}...{MARKET_ACTIONS_NEKO_REF}?per_page=1"
    )
    try:
        with urlopen(url, timeout=10) as response:
            comparison = json.load(response)
        if (
            comparison["status"] in {"ahead", "identical"}
            and comparison["behind_by"] == 0
            and comparison["base_commit"]["sha"] == commit
            and comparison["merge_base_commit"]["sha"] == commit
        ):
            return commit
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return MARKET_ACTIONS_NEKO_REF


def stage(destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"Plugin tools already exist: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".plugin-tools-", dir=destination.parent) as staging:
        resources = Path(staging) / destination.name
        resources.mkdir()
        _build_resources(resources)
        resources.rename(destination)


def _build_resources(destination: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--python", sys.executable, "--out-dir", str(destination)],
        cwd=ROOT,
        check=True,
    )
    (wheel,) = destination.glob("*.whl")
    requirements = destination / "requirements.txt"
    subprocess.run(
        ["uv", "export", "--locked", "--only-group", "plugin-tools", "--no-emit-project",
         "--no-header", "--output-file", str(requirements)],
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
        "workflow_ref": _workflow_ref(commit),
        "platform": sys.platform,
        "architecture": platform.machine().lower(),
        "python": platform.python_version(),
        "wheel": wheel.name,
        "sha256": hashes,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("directory", nargs="?", type=Path, help="Tool resource directory")
    destination.add_argument("--backend", type=Path, help="Path to the packaged backend executable")
    args = parser.parse_args()
    if args.backend:
        if not args.backend.is_file():
            parser.error(f"Backend executable not found: {args.backend}")
        from plugin.neko_plugin_cli.tool_resources import plugin_tool_resources_dir

        stage(plugin_tool_resources_dir(args.backend))
    else:
        stage(args.directory.resolve())
