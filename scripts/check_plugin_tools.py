"""Exercise staged plugin workbench tools outside the source checkout."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import tarfile
from contextlib import ExitStack, contextmanager, suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from shlex import quote
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from unittest.mock import patch
from zipfile import ZipFile

_RELEASE_ASSET = "packaged-probe-v0.1.0.neko-plugin"
_REPOSITORY = "example/n.e.k.o_plugin_packaged_probe"
_RELEASE_URL = f"https://github.com/{_REPOSITORY}/releases/tag/v0.1.0"


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
import subprocess
import sys

def main():
    args = sys.argv[1:]
    if args == ["auth", "status"]:
        return 0
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


@contextmanager
def _market_api():
    class MarketHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/api/v1/release-publications" or body != {
                "release_url": _RELEASE_URL, "asset_name": _RELEASE_ASSET,
            }:
                _reply(self, 400, {"code": "unexpected_smoke_request"})
                return
            _reply(self, 202, {"status": "verifying", "publication_request": {"id": 17}})

    with _loopback_api(MarketHandler) as port:
        print(f"Checking the Market worker against localhost:{port}", flush=True)
        yield f"http://127.0.0.1:{port}"


@contextmanager
def _github_api(root: Path, env: dict[str, str]):
    remote = root / "local-remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        cwd=root, env=env, check=True, capture_output=True, timeout=30,
    )
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
    github_env = {
        "GIT_ALLOW_PROTOCOL": "file:ssh", "GIT_SSH_VARIANT": "simple",
        "GIT_SSH_COMMAND": f"sh {quote(transport.as_posix())}",
    }
    tag = "v0.1.0"
    repo_path = f"/repos/{_REPOSITORY}"

    class GithubHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
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
                    "html_url": _RELEASE_URL,
                    "assets": [
                        {"name": name, "state": "uploaded", "size": 1, "browser_download_url": f"{_RELEASE_URL}/{name}"}
                        for name in (_RELEASE_ASSET, "packaged_probe.market-release-check.txt", "market-evidence.json")
                    ],
                })
            elif self.path.startswith(f"{repo_path}/actions/workflows/release.yml/runs?head_sha="):
                _reply(self, 200, {"workflow_runs": [{
                    "head_branch": tag, "event": "push", "status": "completed", "conclusion": "success",
                }]})
            else:
                _reply(self, 404, {})

    with _loopback_api(GithubHandler) as port:
        print(f"Checking the GitHub worker against localhost:{port}", flush=True)
        yield port, github_env, remote


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


@contextmanager
def _desktop_runtime(backend: Path, root: Path, env: dict[str, str]):
    """Launch the actual desktop backend and exercise the embedded plugin server."""
    import httpx

    from scripts.runtime_memory_baseline import (
        _capture_process_tree,
        _port_released,
        _terminate_process_trees,
    )
    from utils.port_utils import set_port_probe_reuse

    port_keys = ("MAIN_SERVER_PORT", "MEMORY_SERVER_PORT", "TOOL_SERVER_PORT")
    with ExitStack() as sockets:
        ports = {}
        for key in port_keys:
            bound = sockets.enter_context(socket.socket())
            set_port_probe_reuse(bound)
            bound.bind(("127.0.0.1", 0))
            ports[key] = bound.getsockname()[1]
    runtime = root / "runtime"
    storage = root / "storage"
    profile = root / "profile"
    for directory in (runtime, storage, profile):
        directory.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    child_env = {
        **env, **{f"NEKO_{key}": str(port) for key, port in ports.items()},
        "GH_CONFIG_DIR": str(profile / "github-cli"),
        "LOCALAPPDATA": str(profile), "APPDATA": str(profile), "USERPROFILE": str(profile),
        "HOME": str(profile), "XDG_CONFIG_HOME": str(profile / ".config"),
        "XDG_DATA_HOME": str(profile / ".local/share"), "XDG_CACHE_HOME": str(profile / ".cache"),
        "NEKO_USER_DATA_DIR": str(profile), "NEKO_RUNTIME_STATE_DIR": str(runtime),
        "NEKO_STORAGE_SELECTED_ROOT": str(storage), "NEKO_STORAGE_ANCHOR_ROOT": str(storage),
        "NEKO_RUNTIME_SHUTDOWN_TOKEN": token, "NEKO_DISABLE_AGENT_AUTO_RESTORE": "1",
    }
    log_path = root / "desktop.log"
    main_url = f"http://127.0.0.1:{ports['MAIN_SERVER_PORT']}"
    launched_ports = set(ports.values())
    print(
        f"Starting {backend} at {main_url} (shutdown through /api/runtime/shutdown)",
        flush=True,
    )
    with log_path.open("wb") as output, httpx.Client(trust_env=False, timeout=5) as client:
        process = subprocess.Popen(
            [str(backend)], cwd=backend.parent, env=child_env, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            deadline = monotonic() + 180
            record_path = runtime / "launcher.json"
            while True:
                with suppress(FileNotFoundError, PermissionError):
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    if record["pid"] != process.pid:
                        raise RuntimeError("The isolated launcher record belongs to another process")
                    launched_ports.update(record.get("ports", {}).values())
                    launched_ports.update(record.get("internal_ports", {}).values())
                    if record["state"] == "ready":
                        if record["ports"] != ports:
                            raise RuntimeError("The launcher changed the requested server ports")
                        plugin_port = record["internal_ports"]["USER_PLUGIN_SERVER_PORT"]
                        break
                if process.poll() is not None or monotonic() >= deadline:
                    raise RuntimeError(f"The packaged desktop backend did not become ready (exit {process.poll()})")
                sleep(0.25)

            plugin_url = f"http://127.0.0.1:{plugin_port}"
            print(f"Packaged plugin server ready at {plugin_url}", flush=True)
            yield client, plugin_url
        except Exception:
            print("\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]), file=sys.stderr)
            raise
        finally:
            tree = _capture_process_tree(process.pid)
            if process.poll() is None:
                with suppress(httpx.HTTPError, subprocess.TimeoutExpired):
                    response = client.post(f"{main_url}/api/runtime/shutdown", headers={
                        "X-Neko-Runtime-Shutdown-Token": token,
                    })
                    response.raise_for_status()
                    process.wait(timeout=30)
            _, remaining = _terminate_process_trees([process], tree)
            process.wait(timeout=5)
            if remaining:
                raise RuntimeError(f"Packaged desktop left test processes running: {remaining}")
            busy_ports = sorted(port for port in launched_ports if not _port_released(port))
            if busy_ports:
                raise RuntimeError(f"The packaged backend did not release ports: {busy_ports}")


def _check_desktop_runtime(backend: Path, root: Path, env: dict[str, str]) -> None:
    project_root = root / "projects"
    project_root.mkdir(parents=True)
    gh_bin = _install_github_cli_fixture(root)
    with _github_api(root, env) as (github_port, github_env, remote), _market_api() as market_url:
        runtime_env = {
            **env, **github_env, "PATH": str(gh_bin) + os.pathsep + env["PATH"],
            "NEKO_PLUGIN_GITHUB_LOOPBACK_PORT": str(github_port),
            "NEKO_MARKET_API_URL": market_url, "NO_PROXY": "localhost,127.0.0.1",
            "GH_TOKEN": "", "GITHUB_TOKEN": "",
        }
        _check_publication(backend, root, runtime_env, remote)


def _check_publication(
    backend: Path, root: Path, env: dict[str, str], remote: Path,
) -> None:
    def payload(response, status):
        if response.status_code != status:
            raise RuntimeError(f"{response.request.url.path}: {response.status_code} {response.text}")
        return response.json()

    with _desktop_runtime(backend, root / "desktop", env) as (client, plugin_url):
        page = client.get(f"{plugin_url}/ui/")
        page.raise_for_status()
        asset = re.search(r'<script\b[^>]*\bsrc="(/ui/assets/[^"?#]+\.js)"', page.text)
        if asset is None:
            raise RuntimeError("The packaged plugin manager page has no JavaScript bundle")
        client.get(f"{plugin_url}{asset[1]}").raise_for_status()

        origin = {"Origin": plugin_url}
        csrf = payload(client.get(f"{plugin_url}/security/csrf-token", headers=origin), 200)
        headers = {**origin, "X-CSRF-Token": csrf["csrf_token"]}
        project = payload(client.post(f"{plugin_url}/plugin-projects/create", headers=headers, json={
            "parent_dir": str(root / "projects"), "plugin_id": "packaged_probe", "name": "Packaged Probe",
        }), 201)
        project_dir = Path(project["project_dir"])
        path = f"{plugin_url}/plugin-projects/{project['project_id']}"
        state = payload(client.get(f"{path}/toolchain"), 200)
        if state.get("source_checkout") is not False or state.get("check_environment") != "setup":
            raise RuntimeError(f"Unexpected tool environment in packaged runtime: {state}")

        _commit_fixture(project_dir, env)
        repository = payload(client.post(f"{path}/create-repo", headers=headers, json={}, timeout=190), 201)
        if repository.get("repository_url") != f"https://github.com/{_REPOSITORY}":
            raise RuntimeError(f"Unexpected packaged repository result: {repository}")
        report = payload(client.post(f"{path}/precheck", headers=headers, timeout=1200), 200)
        if not report["passed"] or "tests=passed" not in report["stages"][-1]["output"]:
            raise RuntimeError(f"Packaged runtime precheck did not pass: {report}")

        def git(*args, cwd=project_dir):
            return subprocess.check_output(
                ["git", *args], cwd=cwd, env=env, text=True, encoding="utf-8", timeout=30,
            ).strip()

        head = git("rev-parse", "HEAD")
        git("push", remote.as_posix(), "HEAD:refs/heads/main")
        git("update-ref", "refs/remotes/origin/main", head)
        git("branch", "--set-upstream-to=origin/main")
        git("remote", "set-url", "origin", f"git@github.com:{_REPOSITORY}.git")
        identity = {"repository": _REPOSITORY, "head": head, "tag": "v0.1.0"}

        started = payload(client.post(
            f"{path}/publish", headers=headers, json={**identity, "mode": "github_and_market"}, timeout=60,
        ), 202)
        deadline = monotonic() + 180
        while True:
            task = payload(client.get(f"{path}/publish-task"), 200)
            if task["status"] not in {"queued", "running"}:
                break
            if monotonic() >= deadline:
                raise RuntimeError(f"Packaged publication did not finish: {task}")
            sleep(0.5)
        if (
            task["task_id"] != started["task_id"] or task["status"] != "succeeded"
            or task["release_url"] != _RELEASE_URL or task["market_request_id"] != 17
            or task["market_status"] != "verifying"
        ):
            raise RuntimeError(f"Unexpected packaged publication task: {task}")
        if git("rev-parse", "refs/tags/v0.1.0", cwd=remote) != head:
            raise RuntimeError("Packaged GitHub worker did not push the expected tag")

    print("Packaged backend passed repository creation, local precheck and GitHub + Market publication")


def check(
    backend: Path, expected_commit: str | None = None, *,
    resources_only: bool = False,
) -> None:
    with TemporaryDirectory(prefix="neko plugin tools ") as directory:
        root = Path(directory)
        state = root / "state"
        state.mkdir()
        with patch.dict(os.environ, {
            "NEKO_STORAGE_SELECTED_ROOT": str(state),
            "NEKO_STORAGE_ANCHOR_ROOT": str(state),
        }):
            from plugin.server.application import plugin_tool_environment
            from plugin.server.application.plugin_project_commands import check_child_env

            try:
                resources = plugin_tool_environment.tool_resources(backend)
                if expected_commit is not None and resources.commit != expected_commit:
                    raise RuntimeError(f"Plugin tools {resources.commit} do not match checkout {expected_commit}")
                plugin_tool_environment.verify_tool_resources(resources)
                if resources_only:
                    print(f"Verified packaged plugin tool resources for {resources.commit}")
                    return
                worker_env = {
                    **check_child_env(),
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_ALLOW_PROTOCOL": "file",
                }
                _check_desktop_runtime(backend, root / "workbench", worker_env)
            finally:
                # Windows needs the plugin log closed before deleting the test root.
                logging.shutdown()


def _extract_windows_backend(archive: Path, destination: Path) -> Path:
    prefix = "resources/bin/"
    executable = f"{prefix}projectneko_server.exe"
    with ZipFile(archive) as bundle:
        names = bundle.namelist()
        missing = {"N.E.K.O.exe", executable}.difference(names)
        if missing:
            raise RuntimeError(f"Desktop ZIP is missing: {', '.join(sorted(missing))}")
        # Keep the backend layout and test independently of the build tree.
        bundle.extractall(destination, members=(name for name in names if name.startswith(prefix)))
    return destination / executable


@contextmanager
def _packaged_backend(artifact: Path):
    if not artifact.is_file():
        raise RuntimeError(f"Packaged artifact not found: {artifact}")
    suffix = artifact.suffix.casefold()
    is_tar = artifact.name.endswith(".tar.gz")
    if suffix not in {".zip", ".appimage"} and not is_tar:
        yield artifact
        return

    with TemporaryDirectory(prefix="neko extracted desktop ") as directory:
        destination = Path(directory)
        if suffix == ".zip":
            backend = _extract_windows_backend(artifact, destination)
        elif is_tar:
            if sys.platform == "darwin":
                destination /= "N.E.K.O.app"
                destination.mkdir()
            with tarfile.open(artifact, "r:gz") as bundle:
                bundle.extractall(destination, filter="data")
            if sys.platform == "darwin":
                desktop = destination / "Contents/MacOS/N.E.K.O"
                backend_app = destination / "Contents/Resources/bin/projectneko_server.app"
                backend = backend_app / "Contents/MacOS/projectneko_server"
                # The outer app may be unsigned in nightly builds. The inner
                # Nuitka app is signed even when Electron signing is disabled.
                signed_apps = [backend_app]
                if (destination / "Contents/_CodeSignature/CodeResources").is_file():
                    signed_apps.append(destination)
                for app in signed_apps:
                    subprocess.run(
                        ["codesign", "--verify", "--deep", "--strict", str(app)],
                        check=True, timeout=120,
                    )
            else:
                desktop = destination / "n.e.k.o"
                backend = destination / "resources/bin/projectneko_server"
            if not desktop.is_file():
                raise RuntimeError(f"Portable archive is missing the desktop executable: {desktop}")
        else:
            subprocess.run(
                [str(artifact), "--appimage-extract"], cwd=destination,
                check=True, timeout=300, stdout=subprocess.DEVNULL,
            )
            backend = destination / "squashfs-root/resources/bin/projectneko_server"
        if not backend.is_file():
            raise RuntimeError(f"Desktop artifact is missing the backend: {backend}")
        yield backend


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, help="Frozen backend executable, Portable ZIP/tar.gz, or Linux AppImage")
    parser.add_argument("--expected-commit")
    parser.add_argument("--resources-only", action="store_true", help="Validate bundled tool resources without starting the backend")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    with _packaged_backend(artifact) as backend:
        check(
            backend, args.expected_commit,
            resources_only=args.resources_only,
        )
