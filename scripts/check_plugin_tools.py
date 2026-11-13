"""Exercise staged plugin workbench tools outside the source checkout."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from contextlib import ExitStack, contextmanager, nullcontext
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from shlex import quote
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch
from zipfile import ZipFile


_RELEASE_ASSET = "packaged-probe-v0.1.0.neko-plugin"
_REPOSITORY = "example/n.e.k.o_plugin_packaged_probe"


def _install_github_cli_fixture(root: Path) -> Path:
    # uv installs a native Windows console launcher, which subprocess can
    # resolve as `gh` without a shell. The fixture has no dependencies.
    wheel = root / "workbench_gh_fixture-0.0.0-py3-none-any.whl"
    metadata = "workbench_gh_fixture-0.0.0.dist-info"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(f"{metadata}/METADATA", "Metadata-Version: 2.1\nName: workbench-gh-fixture\nVersion: 0.0.0\n")
        archive.writestr(f"{metadata}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{metadata}/entry_points.txt", "[console_scripts]\ngh = workbench_gh_fixture:main\n")
        archive.writestr("workbench_gh_fixture.py", f'''\
import os
import subprocess
import sys
from pathlib import Path

def main():
    args = sys.argv[1:]
    with Path(os.environ["NEKO_TEST_GH_LOG"]).open("a", encoding="utf-8") as log:
        print(" ".join(args), file=log)
    if args == ["auth", "status"]:
        return 0 if os.environ["NEKO_TEST_GH_AUTH"] == "1" else 1
    if args == ["api", "user", "--jq", ".login"]:
        print("example")
        return 0
    if args == ["repo", "create", "{_REPOSITORY}", "--public", "--source", ".", "--remote", "origin"]:
        return subprocess.call(["git", "remote", "add", "origin", "https://github.com/{_REPOSITORY}"])
    print(f"Unexpected GitHub CLI call: {{args}}", file=sys.stderr)
    return 1
''')
        archive.writestr(f"{metadata}/RECORD", "".join(f"{name},,\n" for name in archive.namelist()))
    fixture = root / "github-cli"
    subprocess.run(["uv", "venv", "--no-project", "--python", sys.executable, str(fixture)], check=True, timeout=60)
    bin_dir = fixture / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    subprocess.run(
        ["uv", "pip", "install", "--no-config", "--no-index", "--no-deps", "--python", str(python), str(wheel)],
        check=True, timeout=60,
    )
    return bin_dir


def _worker_environment(directory: Path, environment: dict[str, str]) -> dict[str, str]:
    env = dict(environment)
    env.update(
        GH_CONFIG_DIR=str(directory / "gh-config"),
        GH_PROMPT_DISABLED="1",
        NO_PROXY="localhost,127.0.0.1",
        PYTHONIOENCODING="utf-8",
    )
    for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[proxy] = "http://127.0.0.1:1"
    return env


def _run_worker(
    backend: Path, directory: Path, *args: str, environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(backend), "--neko-plugin-project-worker", *args], cwd=directory,
        timeout=30, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_worker_environment(directory, environment),
    )


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


def _reply(handler: BaseHTTPRequestHandler, status: int, payload: object, *, retry_after: int | None = None) -> None:
    response = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(response)))
    if retry_after is not None:
        handler.send_header("Retry-After", str(retry_after))
    handler.end_headers()
    handler.wfile.write(response)


@contextmanager
def _market_api():
    requests = []
    retrying = Event()

    class MarketHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, json.loads(body)))
            if len(requests) == 1:
                _reply(self, 503, {"code": "verification_running"}, retry_after=60)
                retrying.set()
                return
            _reply(self, 202, {"status": "verifying", "publication_request": {"id": 17}})

    with _loopback_api(MarketHandler) as port:
        print(f"Checking the Market worker against localhost:{port}", flush=True)
        yield f"http://127.0.0.1:{port}", requests, retrying


@contextmanager
def _github_api(root: Path, project: Path, repository: str, env: dict[str, str]):
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
            elif self.path in {f"{repo_path}/releases/tags/{tag}", f"{repo_path}/git/ref/tags/{tag}"}:
                tagged = subprocess.run(
                    ["git", "rev-parse", "--verify", f"refs/tags/{tag}"],
                    cwd=remote, env=env, capture_output=True, text=True,
                )
                if tagged.returncode:
                    _reply(self, 404, {})
                    return
                if self.path.startswith(f"{repo_path}/git/"):
                    _reply(self, 200, {"object": {"type": "commit", "sha": tagged.stdout.strip()}})
                    return
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

    with _loopback_api(GithubHandler) as port:
        print(f"Checking the GitHub worker against localhost:{port}", flush=True)
        yield port, github_env, head, requests
    if git(remote, "rev-parse", f"refs/tags/{tag}") != head:
        raise RuntimeError("Packaged GitHub worker did not push the expected tag")


async def _check_workbench_routes(
    backend: Path, root: Path, gh_bin: Path, commit: str, env: dict[str, str],
) -> None:
    import httpx
    from fastapi import FastAPI

    from plugin import settings
    from plugin.server.application import plugin_project_commands, plugin_projects
    from plugin.server.application import plugin_publish_status, plugin_publish_tasks, plugin_repo_tasks
    from plugin.server.routes.plugin_projects import router

    logging.getLogger("httpx").setLevel(logging.WARNING)

    def payload(response: httpx.Response, status: int) -> dict:
        if response.status_code != status:
            raise RuntimeError(f"{response.request.url.path}: {response.status_code} {response.text}")
        return response.json()

    application = FastAPI()
    application.include_router(router)
    repository = f"https://github.com/{_REPOSITORY}"
    gh_log = root / "github-cli.log"
    network_env = _worker_environment(root, env)
    network_env.update(
        PATH=str(gh_bin) + os.pathsep + env["PATH"], NEKO_TEST_GH_LOG=str(gh_log), NEKO_TEST_GH_AUTH="0",
    )
    with ExitStack() as patches:
        patches.enter_context(patch.object(sys, "executable", str(backend)))
        patches.enter_context(patch.object(sys, "frozen", True, create=True))
        patches.enter_context(patch.object(
            plugin_repo_tasks, "run_cli", partial(plugin_project_commands.run_cli, env=network_env),
        ))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://127.0.0.1",
        ) as client:
            scaffold = payload(await client.post("/plugin-projects/create", json={
                "parent_dir": str(root), "plugin_id": "packaged_probe", "name": "Packaged Probe",
            }), 201)
            project = Path(scaffold["project_dir"])
            path = f"/plugin-projects/{scaffold['project_id']}"
            for workflow in ("verify", "release"):
                content = (project / f".github/workflows/{workflow}.yml").read_text(encoding="utf-8")
                if f"plugin-market-{workflow}.yml@{commit}\n" not in content or f"neko-ref: {commit}\n" not in content:
                    raise RuntimeError(f"Generated {workflow} workflow does not pin the packaged build")
            await asyncio.to_thread(_commit_fixture, project, env)

            manual = payload(await client.post(f"{path}/create-repo", json={}), 200)
            if manual.get("status") != "manual":
                raise RuntimeError(f"Unexpected packaged repository fallback: {manual}")
            network_env["NEKO_TEST_GH_AUTH"] = "1"
            created = payload(await client.post(f"{path}/create-repo", json={}), 201)
            repeated = payload(await client.post(f"{path}/create-repo", json={}), 201)
            if any(result.get("repository_url") != repository for result in (created, repeated)):
                raise RuntimeError(f"Unexpected packaged repository result: {created}")
            expected_calls = [
                "auth status", "auth status", "api user --jq .login",
                f"repo create {_REPOSITORY} --public --source . --remote origin",
            ]
            if gh_log.read_text(encoding="utf-8").splitlines() != expected_calls:
                raise RuntimeError("Repository setup did not use the isolated GitHub CLI as expected")
            target = root / "packages"
            with patch.object(plugin_projects, "TemporaryDirectory", return_value=nullcontext(str(target))):
                report = payload(await client.post(f"{path}/precheck"), 200)
            if not report["passed"] or "tests=passed" not in report["stages"][-1]["output"]:
                raise RuntimeError(f"Packaged HTTP precheck did not pass: {report}")
            with ZipFile(target / "packaged_probe.neko-plugin") as package:
                if not any(name.endswith("/plugin.meta.json") for name in package.namelist()):
                    raise RuntimeError("Plugin tools did not generate packaged metadata")
            stale = await asyncio.to_thread(
                _run_worker, backend, root, "github", str(project), "packaged_probe",
                repository.removeprefix("https://github.com/"), "v0.1.0", "0" * 40, "0", environment=env,
            )
            if stale.returncode != 1 or "changed during release checks" not in stale.stdout:
                raise RuntimeError(f"Packaged GitHub worker failed to reject stale HEAD:\n{stale.stdout}\n{stale.stderr}")

            with _github_api(root, project, repository, env) as (port, github_env, head, github_requests):
                with _market_api() as (market_url, market_requests, retrying), ExitStack() as publish_patches:
                    github_url = f"http://127.0.0.1:{port}"
                    command = plugin_project_commands.project_worker_command

                    def worker_command(*args: str) -> list[str]:
                        if args[0] == "github":
                            args = (*args, "--loopback-api", str(port))
                        return command(*args)

                    publish_patches.enter_context(patch.object(plugin_publish_tasks, "project_worker_command", worker_command))
                    publish_patches.enter_context(patch.object(
                        plugin_publish_tasks, "start_cli",
                        partial(plugin_project_commands.start_cli, env=_worker_environment(root, github_env)),
                    ))
                    publish_patches.enter_context(patch.object(plugin_publish_status, "_GITHUB_API", github_url))
                    publish_patches.enter_context(patch.object(plugin_publish_status, "github_headers", return_value={}))
                    publish_patches.enter_context(patch.object(settings, "MARKET_API_URL", market_url))
                    tag = "v0.1.0"
                    release_url = f"{repository}/releases/tag/{tag}"
                    identity = {"repository": repository.removeprefix("https://github.com/"), "head": head, "tag": tag}

                    async def wait_for_task(started: dict, mode: str) -> dict:
                        async with asyncio.timeout(120):
                            while True:
                                task = payload(await client.get(f"{path}/publish-task"), 200)
                                if task["status"] not in {"queued", "running"}:
                                    break
                                await asyncio.sleep(0.5)
                            # Wait for persistence and the release of the project lock.
                            worker = plugin_publish_tasks._workers.get(started["task_id"])
                            if worker is not None:
                                await asyncio.shield(worker)
                        if task["task_id"] != started["task_id"] or task["release_url"] != release_url:
                            raise RuntimeError(f"Unexpected packaged publication task for {mode}: {task}")
                        return task

                    try:
                        for mode in ("github", "market_notify", "github_and_market"):
                            if mode == "market_notify":
                                started = payload(await client.post(f"{path}/publish", json={**identity, "mode": mode}), 202)
                                if not await asyncio.to_thread(retrying.wait, 30):
                                    raise RuntimeError("Market worker did not reach the retry response")
                                payload(await client.post(f"{path}/publish-task/{started['task_id']}/cancel"), 200)
                                canceled = await wait_for_task(started, mode)
                                if canceled["status"] != "canceled":
                                    raise RuntimeError(f"Packaged Market worker did not cancel: {canceled}")
                            started = payload(await client.post(f"{path}/publish", json={**identity, "mode": mode}), 202)
                            task = await wait_for_task(started, mode)
                            if task["status"] != "succeeded":
                                raise RuntimeError(f"Packaged {mode} did not publish the expected Release: {task}")
                            expected_receipt = None if mode == "github" else 17
                            expected_status = None if mode == "github" else "verifying"
                            if task["market_request_id"] != expected_receipt or task["market_status"] != expected_status:
                                raise RuntimeError(f"Unexpected Market receipt for {mode}: {task}")
                            print(f"Packaged workbench publication passed: {mode}", flush=True)
                        with patch.dict(plugin_publish_tasks._latest, clear=True):
                            saved = payload(await client.get(f"{path}/publish-task"), 200)
                        if saved != {**task, "log": []}:
                            raise RuntimeError(f"Unexpected persisted publication receipt: {saved}")
                    finally:
                        await plugin_publish_tasks.shutdown_publications()
                    expected_request = ("/api/v1/release-publications", {
                        "release_url": release_url, "asset_name": _RELEASE_ASSET,
                    })
                    if market_requests != [expected_request] * 3:
                        raise RuntimeError(f"Unexpected Market requests: {market_requests}")
                    if not any("/actions/workflows/release.yml/runs?" in path for path in github_requests):
                        raise RuntimeError("The workbench did not verify the GitHub Actions release run")
    print("Packaged workbench HTTP routes passed precheck and all three publication modes")


def _commit_fixture(project: Path, env: dict[str, str]) -> None:
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
    subprocess.run(["git", "add", "-A"], cwd=project, env=env, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Plugin Tools Smoke", "-c", "user.email=smoke@example.invalid",
         "commit", "-m", "Plugin tools smoke fixture"],
        cwd=project, env=env, check=True,
    )


def check(backend: Path, expected_commit: str | None = None) -> None:
    with TemporaryDirectory(prefix="neko plugin tools ") as directory:
        root = Path(directory)
        state = root / "state"
        state.mkdir()
        os.environ["NEKO_STORAGE_SELECTED_ROOT"] = str(state)
        os.environ["NEKO_STORAGE_ANCHOR_ROOT"] = str(state)
        from plugin.server.application import plugin_tool_environment
        from plugin.server.application.plugin_project_commands import check_child_env

        git_env = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
        }
        worker_env = {**check_child_env(), **git_env}

        gh_bin = _install_github_cli_fixture(root)

        with patch.object(sys, "executable", str(backend)):
            resources = plugin_tool_environment.tool_resources()
            if expected_commit is not None and resources.commit != expected_commit:
                raise RuntimeError("Plugin tool version differs from the backend build")
        try:
            asyncio.run(_check_workbench_routes(backend, root, gh_bin, resources.commit, worker_env))
        finally:
            # The in-process routes open the plugin log under the test root.
            # Windows needs its handle closed before TemporaryDirectory cleans up.
            logging.shutdown()


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
