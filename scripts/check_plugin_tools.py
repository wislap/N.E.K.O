"""Exercise staged plugin workbench tools outside the source checkout."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile


def check(backend: Path) -> None:
    with TemporaryDirectory(prefix="neko plugin tools ") as directory:
        root = Path(directory)
        state = root / "state"
        state.mkdir()
        os.environ["NEKO_STORAGE_SELECTED_ROOT"] = str(state)
        os.environ["NEKO_STORAGE_ANCHOR_ROOT"] = str(state)
        from plugin.neko_plugin_cli.core.project import create_project
        from plugin.neko_plugin_cli.templates.generator import PluginSpec
        from plugin.server.application import plugin_tool_environment

        probe = subprocess.run(
            [str(backend), "--neko-plugin-project-worker", "probe"], cwd=root,
            timeout=30, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        )
        if "plugin-project-worker-v1" not in probe.stdout.splitlines():
            raise RuntimeError("The packaged backend did not start the project worker")

        project = root / "packaged_probe"
        create_project(PluginSpec("packaged_probe", quick_start=True, create_github_actions=True),
                       project, repo_root=None)
        (project / "__init__.py").write_text(
            "from plugin.sdk.plugin import NekoPluginBase, neko_plugin\n\n\n"
            "@neko_plugin\n"
            "class PackagedProbePlugin(NekoPluginBase):\n"
            "    pass\n", encoding="utf-8"
        )
        (project / "pyproject.toml").write_text(
            '[project]\nname = "packaged-probe"\nversion = "0.1.0"\ndependencies = ["six==1.17.0"]\n',
            encoding="utf-8",
        )
        (project / "tests/test_smoke.py").write_text(
            "from plugin.plugins.packaged_probe import PackagedProbePlugin\n"
            "from plugin.sdk.plugin import NekoPluginBase\n\n\n"
            "def test_imports():\n"
            "    assert issubclass(PackagedProbePlugin, NekoPluginBase)\n"
            "    from six import text_type\n"
            "    assert text_type is str\n", encoding="utf-8",
        )
        subprocess.run(["git", "add", "-A"], cwd=project, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Plugin Tools Smoke", "-c", "user.email=smoke@example.invalid",
             "commit", "-m", "Plugin tools smoke fixture"],
            cwd=project, check=True,
        )
        with patch.object(sys, "executable", str(backend)):
            tools = asyncio.run(plugin_tool_environment.resolve_tool_environment(None))
        target = root / "packages"
        result = subprocess.run(
            tools.command("precheck", str(project), "--target-dir", str(target), "--json"),
            cwd=tools.cwd, env=tools.env, timeout=600, capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode:
            raise RuntimeError(f"Plugin tools precheck failed:\n{result.stdout}\n{result.stderr}")
        report = json.loads(result.stdout.splitlines()[-1])
        if not report["passed"] or "tests=passed" not in report["stages"][-1]["output"]:
            raise RuntimeError(f"Plugin tools did not run the plugin tests: {report}")
        with ZipFile(target / "packaged_probe.neko-plugin") as package:
            if not any(name.endswith("/plugin.meta.json") for name in package.namelist()):
                raise RuntimeError("Plugin tools did not generate packaged metadata")
        print("Packaged plugin worker and isolated plugin precheck passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", type=Path)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    check(parser.parse_args().backend.resolve())
