"""Run ``neko-plugin`` commands for an authoring project.

Precheck, create-repo and publish all run the CLI as a child process against
the same source tree (``vendor/`` sync, git, tags). This module holds what
they share: one operation per project at a time, a UTF-8 child environment,
and termination of the whole process tree (git, uvx, pytest grandchildren),
not just the direct child.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path

import psutil

from plugin.logging_config import get_logger
from plugin.server.domain.plugin_project_errors import ProjectErrorCode as Code
from plugin.server.domain.plugin_project_errors import project_error

logger = get_logger("server.application.plugin_project_commands")

_TERMINATE_GRACE_SECONDS = 15.0

_busy_lock = threading.Lock()
# project_id -> name of the operation holding it.
_busy: dict[str, str] = {}


def claim_project(project_id: str, operation: str) -> None:
    """Mark ``project_id`` busy with ``operation``; refuse when another holds it."""
    with _busy_lock:
        holder = _busy.get(project_id)
        if holder is not None:
            raise project_error(
                Code.PLUGIN_OPERATION_BUSY,
                f"{holder} is already running for project {project_id}",
                409,
            )
        _busy[project_id] = operation


def release_project(project_id: str) -> None:
    with _busy_lock:
        _busy.pop(project_id, None)


class project_operation:
    """Hold ``project_id`` for one operation.

    A class, not ``@contextmanager``: the generator form rewrites the
    traceback of an exception raised inside the block, which
    ``ServerDomainError`` does not allow (see ``bounded_operation_wait``).
    """

    def __init__(self, project_id: str, operation: str) -> None:
        self._project_id = project_id
        self._operation = operation

    def __enter__(self) -> None:
        claim_project(self._project_id, self._operation)

    def __exit__(self, *_exc: object) -> None:
        release_project(self._project_id)


def cli_child_env() -> dict[str, str]:
    # UTF-8 mode also makes the CLI decode its own git/ruff/pytest pipes as
    # UTF-8, whatever the Windows code page is.
    return {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }


def check_child_env() -> dict[str, str]:
    """Keep tool/network settings while keeping host and publishing credentials out of tests."""
    system = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "HOME", "USERPROFILE",
        "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
        "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "NEKO_PLUGIN_MARKET_SECURITY_CHECKS",
        "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432",
        "UV", "UV_INDEX", "UV_DEFAULT_INDEX", "UV_CACHE_DIR",
        "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONUNBUFFERED",
    }
    return {
        key: value for key, value in cli_child_env().items()
        if key.upper() in system
    }


def project_worker_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return [sys.executable, "--neko-plugin-project-worker", *args]
    return [sys.executable, "-m", "plugin.neko_plugin_cli.project_worker", *args]


def project_worker_directory() -> Path:
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def start_cli(
    argv: list[str], *, cwd: Path, merge_stderr: bool, env: Mapping[str, str] | None = None
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=cli_child_env() if env is None else env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
    )


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill the CLI and everything it spawned (git, uvx, build children)."""
    if process.poll() is not None:
        return
    parent: psutil.Process | None = None
    try:
        parent = psutil.Process(process.pid)
    except psutil.Error:
        pass
    children: list[psutil.Process] = []
    if parent is not None:
        try:
            children = parent.children(recursive=True)
        except psutil.Error:
            children = []

    for child in children:
        try:
            child.terminate()
        except psutil.Error:
            pass
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - escalation best-effort
            logger.warning("CLI subprocess did not exit after kill: pid={}", process.pid)
    # Children may outlive a parent that exits promptly on terminate.
    if not children:
        return
    _, alive = psutil.wait_procs(children, timeout=5)
    for child in alive:
        try:
            child.kill()
        except psutil.Error:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=5)


async def run_cli(
    argv: list[str], *, cwd: Path, timeout: float, merge_stderr: bool = False,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the CLI to completion.

    Raises ``OSError`` when it cannot start and ``subprocess.TimeoutExpired``
    once the tree was killed after ``timeout``. Cancelling the caller kills
    the tree too, so a server shutdown leaves nothing running.
    """
    options = {} if env is None else {"env": env}
    process = start_cli(argv, cwd=cwd, merge_stderr=merge_stderr, **options)
    communicate = asyncio.ensure_future(asyncio.to_thread(process.communicate))
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communicate), timeout)
    except TimeoutError:
        await asyncio.to_thread(terminate_process_tree, process)
        try:
            await asyncio.wait_for(communicate, _TERMINATE_GRACE_SECONDS)
        except TimeoutError:  # pragma: no cover - a killed tree closes its pipes
            logger.warning("CLI pipes stayed open after terminate: pid={}", process.pid)
        raise subprocess.TimeoutExpired(argv, timeout) from None
    except asyncio.CancelledError:
        await asyncio.to_thread(terminate_process_tree, process)
        try:
            await asyncio.wait_for(communicate, _TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            logger.warning("Canceled CLI pipes stayed open: pid={}", process.pid)
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr or "")
