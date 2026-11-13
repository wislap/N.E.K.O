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
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from zipfile import ZipFile


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


def _check_market_worker(backend: Path, root: Path, repository: str, environment: dict[str, str]) -> None:
    release_url = f"{repository}/releases/tag/v0.1.0"
    asset = "packaged_probe.neko-plugin"
    requests = []
    response = json.dumps({
        "status": "verifying", "publication_request": {"id": 17},
    }).encode("utf-8")

    class MarketHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, json.loads(body)))
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    with HTTPServer(("127.0.0.1", 0), MarketHandler) as server:
        print(f"Checking the Market worker against localhost:{server.server_port}", flush=True)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = _run_worker(
                backend, root, "market", release_url, "0", "--asset-name", asset,
                "--endpoint", f"http://127.0.0.1:{server.server_port}/api/v1/release-publications",
                environment=environment,
            )
        finally:
            server.shutdown()
            thread.join()
    expected = {"release_url": release_url, "asset_name": asset}
    if result.returncode or requests != [("/api/v1/release-publications", expected)]:
        raise RuntimeError(f"Packaged Market worker failed:\n{result.stdout}\n{result.stderr}")
    receipt = {"status": "verifying", "candidate_id": None, "request_id": 17, "version": None}
    if _records(result, "publication") != [receipt]:
        raise RuntimeError(f"Packaged Market worker did not report the verification receipt:\n{result.stdout}")


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
        # An existing origin also checks the structured success response.
        repository = "https://github.com/example/n.e.k.o_plugin_packaged_probe"
        subprocess.run(["git", "remote", "add", "origin", repository], cwd=project, env=worker_env, check=True)
        creation = _run_worker(backend, root, "create-repo", str(project), environment=worker_env)
        if creation.returncode or _records(creation, "repository_url") != [repository]:
            raise RuntimeError(f"Packaged repository worker failed:\n{creation.stdout}\n{creation.stderr}")
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
        print("Packaged repository and Market workers, isolated precheck, and GitHub release checks passed")


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
