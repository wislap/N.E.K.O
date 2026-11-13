"""Exercise staged plugin workbench tools outside the source checkout."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from shlex import quote
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from zipfile import ZipFile


_RELEASE_ASSET = "packaged-probe-v0.1.0.neko-plugin"


def _run_worker(
    backend: Path, directory: Path, *args: str, environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    env = dict(environment)
    env.update(
        GH_CONFIG_DIR=str(directory / "gh-config"),
        GH_PROMPT_DISABLED="1",
        NO_PROXY="localhost,127.0.0.1",
        PYTHONIOENCODING="utf-8",
    )
    for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[proxy] = "http://127.0.0.1:1"
    return subprocess.run(
        [str(backend), "--neko-plugin-project-worker", *args], cwd=directory,
        timeout=30, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )


def _records(result: subprocess.CompletedProcess[str], key: str) -> list[object]:
    prefix = "{" + json.dumps(key) + ":"
    return [
        json.loads(line)[key]
        for line in result.stdout.splitlines()
        if line.startswith(prefix)
    ]


def _check_repo_worker(backend: Path, root: Path, project: Path, repository: str, env: dict[str, str]) -> None:
    # Validate the idempotent success response without creating a remote repo.
    subprocess.run(["git", "remote", "add", "origin", repository], cwd=project, env=env, check=True)
    creation = _run_worker(backend, root, "create-repo", str(project), environment=env)
    if creation.returncode or _records(creation, "repository_url") != [repository]:
        raise RuntimeError(f"Packaged repository worker failed:\n{creation.stdout}\n{creation.stderr}")


@contextmanager
def _loopback_api(handler: type[BaseHTTPRequestHandler]):
    with HTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join()


def _reply(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    response = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(response)))
    handler.end_headers()
    handler.wfile.write(response)


def _check_market_worker(backend: Path, root: Path, repository: str, environment: dict[str, str]) -> None:
    release_url = f"{repository}/releases/tag/v0.1.0"
    asset = _RELEASE_ASSET
    requests = []

    class MarketHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, json.loads(body)))
            _reply(self, 202, {"status": "verifying", "publication_request": {"id": 17}})

    with _loopback_api(MarketHandler) as port:
        print(f"Checking the Market worker against localhost:{port}", flush=True)
        result = _run_worker(
            backend, root, "market", release_url, "0", "--asset-name", asset,
            "--endpoint", f"http://127.0.0.1:{port}/api/v1/release-publications",
            environment=environment,
        )
    expected = {"release_url": release_url, "asset_name": asset}
    if result.returncode or requests != [("/api/v1/release-publications", expected)]:
        raise RuntimeError(f"Packaged Market worker failed:\n{result.stdout}\n{result.stderr}")
    receipt = {"status": "verifying", "candidate_id": None, "request_id": 17, "version": None}
    if _records(result, "publication") != [receipt]:
        raise RuntimeError(f"Packaged Market worker did not report the verification receipt:\n{result.stdout}")


def _check_github_worker(backend: Path, root: Path, project: Path, repository: str, env: dict[str, str]) -> None:
    def git(directory: Path, *args: str, environment: dict[str, str] = env) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=directory, env=environment, text=True, encoding="utf-8", errors="replace",
        ).strip()

    remote = root / "local-remote.git"
    git(root, "init", "--bare", "--initial-branch=main", str(remote))
    head = git(project, "rev-parse", "HEAD")
    git(project, "push", remote.as_posix(), "HEAD:refs/heads/main")
    git(project, "update-ref", "refs/remotes/origin/main", head)
    git(project, "branch", "--set-upstream-to=origin/main")
    # Git still validates the configured GitHub URL. A test-only SSH command
    # runs Git's real transport helpers against a fixed local bare repository.
    transport = root / "local-git-transport.sh"
    transport.write_text(
        'case "$2" in\n'
        f'  "git-upload-pack "*) exec git upload-pack {quote(remote.as_posix())} ;;\n'
        f'  "git-receive-pack "*) exec git receive-pack {quote(remote.as_posix())} ;;\n'
        '  *) exit 1 ;;\nesac\n',
        encoding="utf-8",
    )
    github_repo = repository.removeprefix("https://github.com/")
    git(project, "remote", "set-url", "origin", f"git@github.com:{github_repo}.git")
    github_env = {
        **env, "GIT_ALLOW_PROTOCOL": "ssh", "GIT_SSH_VARIANT": "simple",
        "GIT_SSH_COMMAND": f"sh {quote(transport.as_posix())}",
    }
    tag = "v0.1.0"
    release_url = f"{repository}/releases/tag/{tag}"
    asset_name = _RELEASE_ASSET
    repo_path = f"/repos/{github_repo}"
    requests: list[str] = []

    class GithubHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            if self.headers.get("Authorization") is not None:
                _reply(self, 403, {"message": "Unexpected GitHub credentials"})
            elif self.path == repo_path:
                _reply(self, 200, {"private": False})
            elif self.path == f"{repo_path}/releases/tags/{tag}":
                _reply(self, 200, {
                    "html_url": release_url,
                    "assets": [
                        {"name": name, "state": "uploaded", "size": 1, "browser_download_url": f"{release_url}/{name}"}
                        for name in (asset_name, "packaged_probe.market-release-check.txt", "market-evidence.json")
                    ],
                })
            elif self.path == f"{repo_path}/actions/workflows/release.yml/runs?head_sha={head}":
                _reply(self, 200, {"workflow_runs": [{
                    "head_branch": tag, "event": "push", "status": "completed", "conclusion": "success",
                }]})
            else:
                _reply(self, 404, {})

    expected = {"release_url": release_url, "asset_name": asset_name}
    expected_requests = [
        repo_path, f"{repo_path}/releases/tags/{tag}",
        f"{repo_path}/actions/workflows/release.yml/runs?head_sha={head}",
    ]
    with _loopback_api(GithubHandler) as port:
        print(f"Checking the GitHub worker against localhost:{port}", flush=True)
        # The second attempt resumes an already-pushed tag after a restart
        # without modifying the local repository or its remote tag.
        for _ in range(2):
            requests.clear()
            result = _run_worker(
                backend, root, "github", str(project), "packaged_probe", github_repo, tag, head, "0",
                "--loopback-api", str(port), environment=github_env,
            )
            if result.returncode or _records(result, "release_url") != [release_url]:
                raise RuntimeError(f"Packaged GitHub release worker failed:\n{result.stdout}\n{result.stderr}")
            release = next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"release_url":'))
            if release != expected or requests != expected_requests or git(remote, "rev-parse", f"refs/tags/{tag}") != head:
                raise RuntimeError("Packaged GitHub worker did not publish the expected tag and Release")


def check(backend: Path, expected_commit: str | None = None) -> None:
    with TemporaryDirectory(prefix="neko plugin tools ") as directory:
        root = Path(directory)
        state = root / "state"
        state.mkdir()
        os.environ["NEKO_STORAGE_SELECTED_ROOT"] = str(state)
        os.environ["NEKO_STORAGE_ANCHOR_ROOT"] = str(state)
        from plugin.neko_plugin_cli.core.project import create_project
        from plugin.neko_plugin_cli.templates.generator import PluginSpec
        from plugin.server.application import plugin_tool_environment
        from plugin.server.application.plugin_project_commands import check_child_env

        git_env = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
        }
        worker_env = {**check_child_env(), **git_env}

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
        subprocess.run(["git", "add", "-A"], cwd=project, env=worker_env, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Plugin Tools Smoke", "-c", "user.email=smoke@example.invalid",
             "commit", "-m", "Plugin tools smoke fixture"],
            cwd=project, env=worker_env, check=True,
        )
        # An isolated gh config exercises the manual path without a remote write.
        manual = _run_worker(backend, root, "create-repo", str(project), environment=worker_env)
        if manual.returncode != 3 or "[MANUAL]" not in manual.stdout:
            raise RuntimeError(f"Packaged repository fallback failed:\n{manual.stdout}\n{manual.stderr}")
        repository = "https://github.com/example/n.e.k.o_plugin_packaged_probe"
        _check_repo_worker(backend, root, project, repository, worker_env)
        _check_market_worker(backend, root, repository, worker_env)

        with patch.object(sys, "executable", str(backend)):
            resources = plugin_tool_environment.tool_resources()
            if expected_commit is not None and resources.commit != expected_commit:
                raise RuntimeError("Plugin tool version differs from the backend build")
            tools = asyncio.run(plugin_tool_environment.resolve_tool_environment(None))
        target = root / "packages"
        check_env = {**tools.env, **git_env}
        result = subprocess.run(
            tools.command("precheck", str(project), "--target-dir", str(target), "--json"),
            cwd=tools.cwd, env=check_env, timeout=600, capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode:
            raise RuntimeError(f"Plugin tools precheck failed:\n{result.stdout}\n{result.stderr}")
        report = json.loads(result.stdout.splitlines()[-1])
        if not report["passed"] or "tests=passed" not in report["stages"][-1]["output"]:
            raise RuntimeError(f"Plugin tools did not run the plugin tests: {report}")
        with ZipFile(target / "packaged_probe.neko-plugin") as package:
            if not any(name.endswith("/plugin.meta.json") for name in package.namelist()):
                raise RuntimeError("Plugin tools did not generate packaged metadata")
        release_check = subprocess.run(
            tools.command("publish", "github", str(project), "--check-only"),
            cwd=tools.cwd, env=check_env, timeout=600, capture_output=True, text=True, encoding="utf-8",
        )
        if release_check.returncode:
            raise RuntimeError(f"Packaged GitHub release checks failed:\n{release_check.stdout}\n{release_check.stderr}")
        stale = _run_worker(
            backend, root, "github", str(project), "packaged_probe", repository.removeprefix("https://github.com/"),
            "v0.1.0", "0" * 40, "0", environment=worker_env,
        )
        if stale.returncode != 1 or "changed during release checks" not in stale.stdout:
            raise RuntimeError(f"Packaged GitHub worker failed to reject stale HEAD:\n{stale.stdout}\n{stale.stderr}")
        if subprocess.check_output(["git", "tag", "--list"], cwd=project, env=worker_env, text=True).strip():
            raise RuntimeError("Release checks created a tag")
        _check_github_worker(backend, root, project, repository, worker_env)
        print("Packaged repository worker, isolated precheck, GitHub release, and Market notification passed")


@contextmanager
def _backend_from_zip(archive: Path):
    with TemporaryDirectory(prefix="neko extracted desktop ") as directory:
        destination = Path(directory)
        prefix = "resources/bin/"
        with ZipFile(archive) as bundle:
            names = bundle.namelist()
            backend_files = [name for name in names if name.startswith(prefix)]
            required = {"N.E.K.O.exe", f"{prefix}projectneko_server.exe", f"{prefix}plugin-tools/manifest.json"}
            missing = required.difference(names)
            if missing:
                raise RuntimeError(f"Desktop ZIP is missing: {', '.join(sorted(missing))}")
            # Extract the backend, keeping its relative layout and testing a
            # destination containing spaces independently of the build tree.
            bundle.extractall(destination, members=backend_files)
        yield destination / prefix / "projectneko_server.exe"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, help="Frozen backend executable or Windows desktop ZIP")
    parser.add_argument("--expected-commit")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    if artifact.suffix.casefold() == ".zip":
        with _backend_from_zip(artifact) as backend:
            check(backend, args.expected_commit)
    else:
        check(artifact, args.expected_commit)
