"""The repo-create server task maps the CLI's exit codes to UI outcomes."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from plugin.server.application import plugin_project_commands as commands
from plugin.server.application import plugin_repo_tasks as repo_tasks
from plugin.server.domain.errors import ServerDomainError

pytestmark = pytest.mark.plugin_unit


def _completed(returncode: int, stdout: str):
    async def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    return run


def _patch(monkeypatch, *, returncode: int, stdout: str):
    monkeypatch.setattr(repo_tasks, "run_cli", _completed(returncode, stdout))


def _run(monkeypatch, *, private: bool = False):
    return asyncio.run(repo_tasks._run(Path("/plugins/demo"), private=private))


def test_exit_zero_is_created_and_extracts_the_repository_url(monkeypatch):
    _patch(
        monkeypatch,
        returncode=0,
        stdout='[OK] repository ready\n{"repository_url": "https://github.com/octocat/n.e.k.o_plugin_demo"}',
    )
    result = _run(monkeypatch)
    assert result["status"] == "created"
    assert result["repository_url"] == "https://github.com/octocat/n.e.k.o_plugin_demo"


def test_exit_three_is_manual_and_keeps_the_steps_in_the_log(monkeypatch):
    _patch(
        monkeypatch,
        returncode=3,
        stdout="[MANUAL] GitHub CLI (gh) is not available\n  1. Create a GitHub repository named n.e.k.o_plugin_demo",
    )
    result = _run(monkeypatch)
    assert result["status"] == "manual"
    assert result["repository_url"] == ""
    assert any("[MANUAL]" in line for line in result["log"])


def test_other_exit_codes_are_failed(monkeypatch):
    _patch(monkeypatch, returncode=1, stdout="[FAIL] a GitHub repository named octocat/x already exists")
    result = _run(monkeypatch)
    assert result["status"] == "failed"


def test_packaged_backend_uses_the_bundled_repository_worker(monkeypatch, tmp_path):
    executable = tmp_path / "projectneko_server.exe"
    monkeypatch.setattr(commands.sys, "executable", str(executable))
    monkeypatch.setattr(commands.sys, "frozen", True, raising=False)

    async def run(argv, **kwargs):
        assert argv[:3] == [str(executable), "--neko-plugin-project-worker", "create-repo"]
        assert kwargs["cwd"] == tmp_path
        return subprocess.CompletedProcess(argv, 3, "[MANUAL] Sign in first", "")

    monkeypatch.setattr(repo_tasks, "run_cli", run)
    assert _run(monkeypatch)["status"] == "manual"


async def test_a_timeout_is_a_failed_domain_error(monkeypatch):

    async def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="create-repo", timeout=180)

    monkeypatch.setattr(repo_tasks, "run_cli", _timeout)
    with pytest.raises(ServerDomainError) as excinfo:
        await repo_tasks._run(Path("/plugins/demo"), private=False)
    assert excinfo.value.code == "REPO_CREATE_FAILED"


def test_private_flag_is_forwarded_to_the_cli(monkeypatch):
    captured = {}

    async def _capture(argv, **kwargs):
        captured["argv"] = argv
        captured["merge_stderr"] = kwargs.get("merge_stderr")
        return subprocess.CompletedProcess(
            argv, 0, "[OK] repository ready: https://github.com/o/n.e.k.o_plugin_demo", ""
        )

    monkeypatch.setattr(repo_tasks, "run_cli", _capture)
    _run(monkeypatch, private=True)
    assert "--private" in captured["argv"]
    assert "create-repo" in captured["argv"]


async def test_create_repo_is_refused_while_the_project_is_busy(monkeypatch):
    _patch(monkeypatch, returncode=0, stdout="")
    monkeypatch.setattr(repo_tasks, "_record_source_dir", lambda _id: Path("/plugins/demo"))
    commands.claim_project("p1", "publish")
    try:
        with pytest.raises(ServerDomainError) as excinfo:
            await repo_tasks.create_repository("p1")
    finally:
        commands.release_project("p1")
    assert excinfo.value.code == "PLUGIN_OPERATION_BUSY"


def test_create_repo_releases_the_project_when_done(monkeypatch):
    _patch(monkeypatch, returncode=0, stdout="")
    monkeypatch.setattr(repo_tasks, "_record_source_dir", lambda _id: Path("/plugins/demo"))
    assert asyncio.run(repo_tasks.create_repository("p1"))["status"] == "created"
    assert "p1" not in commands._busy
