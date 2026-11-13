"""Resolve the interpreter used to check plugins from the workbench."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import portalocker

from plugin import settings
from plugin.neko_plugin_cli.tool_resources import plugin_tool_resources_dir
from plugin.server.application.plugin_project_commands import check_child_env, run_cli
from utils.file_utils import atomic_write_text


class ToolEnvironmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolEnvironment:
    python: Path
    cwd: Path
    env: dict[str, str]

    def command(self, *args: str) -> list[str]:
        return [str(self.python), "-m", "plugin.neko_plugin_cli.cli", *args]


@dataclass(frozen=True)
class ToolResources:
    root: Path
    key: str
    commit: str
    python: str
    wheel: str
    hashes: dict[str, str]

    @property
    def environment_root(self) -> Path:
        return settings.get_plugin_state_root().parent / ".neko-plugin-tools" / self.key

    @property
    def executable(self) -> Path:
        return self.environment_root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

    @property
    def ready_file(self) -> Path:
        return self.environment_root / ".ready"

    def is_ready(self) -> bool:
        try:
            return self.executable.is_file() and self.ready_file.read_text(encoding="utf-8") == self.key
        except OSError:
            return False


def tool_resources(backend: Path | None = None) -> ToolResources:
    root = plugin_tool_resources_dir(backend or Path(sys.executable))
    try:
        contents = (root / "manifest.json").read_bytes()
        manifest = json.loads(contents)
        wheel = manifest["wheel"]
        commit = manifest["commit"]
        python = manifest["python"]
        hashes = manifest["sha256"]
        if (
            manifest["schema"] != 1
            or manifest["platform"] != sys.platform
            or manifest["architecture"] != platform.machine().lower()
            or not re.fullmatch(r"[a-f0-9]{40}", commit)
            or not re.fullmatch(r"3\.11\.\d+", python)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+\.whl", wheel)
            or set(hashes) != {wheel, "requirements.txt", "pytest.ini"}
            or any(not re.fullmatch(r"[a-f0-9]{64}", digest) for digest in hashes.values())
        ):
            raise ValueError("invalid tooling manifest")
        if any(not (root / name).is_file() for name in hashes):
            raise ValueError("incomplete tooling resources")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ToolEnvironmentError("This N.E.K.O. package has no compatible plugin tooling resources") from exc
    return ToolResources(root, hashlib.sha256(contents).hexdigest(), commit, python, wheel, hashes)


def packaged_workflow_ref() -> str:
    from plugin.neko_plugin_cli.templates.generator import MARKET_ACTIONS_NEKO_REF

    if not (getattr(sys, "frozen", False) or "__compiled__" in globals()):
        return MARKET_ACTIONS_NEKO_REF
    try:
        return tool_resources().commit
    except ToolEnvironmentError:
        return MARKET_ACTIONS_NEKO_REF


def verify_tool_resources(resources: ToolResources) -> None:
    for name, expected in resources.hashes.items():
        try:
            with (resources.root / name).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == expected:
                    continue
        except OSError as exc:
            raise ToolEnvironmentError(f"Cannot read plugin tooling resource {name}") from exc
        raise ToolEnvironmentError(f"Plugin tooling resource {name} failed its integrity check")


async def _prepare(resources: ToolResources, env: dict[str, str]) -> None:
    uv = shutil.which("uv")
    if uv is None or shutil.which("uvx") is None:
        raise ToolEnvironmentError("Install uv and uvx to prepare the plugin tool environment")
    await asyncio.to_thread(verify_tool_resources, resources)
    root = resources.environment_root
    # uv must use a version-matched interpreter even on machines with another
    # Python installed. Keep its download and install state outside the bundle.
    bootstrap_env = {key: value for key, value in env.items() if not key.startswith("UV_")}
    bootstrap_env["UV_PYTHON_INSTALL_DIR"] = str(root.parent / "python")
    commands = [
        [uv, "venv", "--no-config", "--no-project", "--managed-python", "--clear",
         "--python", resources.python, str(root)],
        [uv, "pip", "sync", "--no-config", "--require-hashes", "--only-binary", ":all:",
         "--python", str(resources.executable), str(resources.root / "requirements.txt")],
        [uv, "pip", "install", "--no-config", "--no-deps",
         "--python", str(resources.executable), str(resources.root / resources.wheel)],
    ]
    for command in commands:
        result = await run_cli(command, cwd=root.parent, timeout=300, merge_stderr=True, env=bootstrap_env)
        if result.returncode:
            detail = (result.stdout or "").strip().splitlines()
            raise ToolEnvironmentError(f"Could not prepare plugin tools: {' '.join(detail[-3:])[-500:]}")
    atomic_write_text(resources.ready_file, resources.key)


async def resolve_tool_environment(source_root: Path | None) -> ToolEnvironment:
    env = check_child_env()
    if source_root is not None:
        return _check_environment(
            Path(sys.executable), source_root, env,
            settings.get_plugin_state_root().parent / ".neko-plugin-tools" / "source-state",
        )
    resources = tool_resources()
    try:
        root = resources.environment_root
        root.parent.mkdir(parents=True, exist_ok=True)
        lock = portalocker.Lock(root.with_suffix(".lock"), timeout=0)
        # Acquiring without a blocking wait keeps cancellation responsive.
        async with asyncio.timeout(600):
            while not resources.is_ready():
                try:
                    lock.acquire()
                except portalocker.exceptions.LockException:
                    await asyncio.sleep(0.25)
                    continue
                try:
                    if not resources.is_ready():
                        await _prepare(resources, env)
                finally:
                    lock.release()
    except (OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
        raise ToolEnvironmentError(f"Could not prepare plugin tools: {exc}") from exc
    env["NEKO_PLUGIN_TEST_CONFIG"] = str(resources.root / "pytest.ini")
    return _check_environment(
        resources.executable, resources.environment_root, env, resources.environment_root / "state",
    )


def _check_environment(python: Path, cwd: Path, env: dict[str, str], state: Path) -> ToolEnvironment:
    # Logging/config initialization in the test runner should use its own data
    # directory, even when the host is using the default user data root.
    env["NEKO_STORAGE_SELECTED_ROOT"] = str(state)
    env["NEKO_STORAGE_ANCHOR_ROOT"] = str(state)
    return ToolEnvironment(python, cwd, env)
