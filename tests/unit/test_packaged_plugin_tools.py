import json
import os
import tarfile
from io import BytesIO
from zipfile import ZipFile

import pytest

from plugin.neko_plugin_cli import tool_resources
from plugin.neko_plugin_cli.tool_resources import plugin_tool_resources_dir
from scripts import build_plugin_tools, check_plugin_tools


@pytest.mark.parametrize(("status", "behind", "trusted"), [("ahead", 0, True), ("diverged", 9, False)])
def test_workflow_ref_uses_only_commits_on_official_main(monkeypatch, status, behind, trusted):
    commit = "a" * 40
    comparison = {
        "status": status, "behind_by": behind,
        "base_commit": {"sha": commit}, "merge_base_commit": {"sha": commit if trusted else "b" * 40},
    }
    monkeypatch.setattr(build_plugin_tools, "urlopen", lambda url, timeout: BytesIO(json.dumps(comparison).encode()))
    assert build_plugin_tools._workflow_ref(commit) == (commit if trusted else "main")


def test_checks_keep_uv_configuration_and_isolate_host_tokens(monkeypatch):
    from plugin.server.application.plugin_project_commands import check_child_env

    config = {"UV_SYSTEM_CERTS": "1", "UV_OFFLINE": "1", "UV_CONFIG_FILE": "custom.toml"}
    for key, value in config.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GH_TOKEN", "smoke-secret")
    env = check_child_env()
    assert {key: env.get(key) for key in config} == config
    assert "GH_TOKEN" not in env


def test_tool_build_failure_leaves_destination_available_for_retry(tmp_path, monkeypatch):
    destination = tmp_path / "plugin-tools"

    def fail(directory):
        (directory / "partial.whl").touch()
        raise RuntimeError("wheel build failed")

    monkeypatch.setattr(build_plugin_tools, "_build_resources", fail)
    with pytest.raises(RuntimeError, match="wheel build failed"):
        build_plugin_tools.stage(destination)
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setattr(build_plugin_tools, "_build_resources", lambda directory: (directory / "manifest.json").touch())
    build_plugin_tools.stage(destination)
    assert (destination / "manifest.json").is_file()


def test_windows_zip_preserves_backend_layout(tmp_path):
    archive = tmp_path / "desktop.zip"
    with ZipFile(archive, "w") as bundle:
        bundle.writestr("N.E.K.O.exe", "")
        bundle.writestr("resources/bin/projectneko_server.exe", "")
        bundle.writestr("resources/bin/plugin-tools/manifest.json", "{}")

    with check_plugin_tools._packaged_backend(archive) as backend:
        assert backend.name == "projectneko_server.exe"
        assert (plugin_tool_resources_dir(backend) / "manifest.json").is_file()
    assert not backend.exists()


@pytest.mark.parametrize(
    ("platform", "desktop", "backend_path", "resources", "outer_signed"),
    [
        ("linux", "n.e.k.o", "resources/bin/projectneko_server", "resources/bin/plugin-tools", False),
        (
            "darwin", "Contents/MacOS/N.E.K.O",
            "Contents/Resources/bin/projectneko_server.app/Contents/MacOS/projectneko_server",
            "Contents/Resources/bin/projectneko_server.app/Contents/Resources/plugin-tools",
            True,
        ),
    ],
)
def test_portable_tar_preserves_backend_layout(
    tmp_path, monkeypatch, platform, desktop, backend_path, resources, outer_signed,
):
    root = tmp_path / "app"
    for name in (desktop, backend_path, f"{resources}/manifest.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    if outer_signed:
        seal = root / "Contents/_CodeSignature/CodeResources"
        seal.parent.mkdir()
        seal.touch()

    library = root / backend_path
    library.chmod(0o755)
    if os.name == "posix":
        library.with_name("backend-link").symlink_to(library.name)

    archive = tmp_path / "desktop.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(root, arcname=".")

    calls = []
    monkeypatch.setattr(check_plugin_tools.sys, "platform", platform)
    monkeypatch.setattr(check_plugin_tools.subprocess, "run", lambda command, **kwargs: calls.append(command))
    with check_plugin_tools._packaged_backend(archive) as backend:
        assert backend.as_posix().endswith(f"/{backend_path}")
        assert plugin_tool_resources_dir(backend).as_posix().endswith(f"/{resources}")
        assert (plugin_tool_resources_dir(backend) / "manifest.json").read_text() == "fixture"
        if os.name == "posix":
            assert backend.stat().st_mode & 0o100
            assert backend.with_name("backend-link").resolve() == backend
        if platform == "darwin":
            verify = ["codesign", "--verify", "--deep", "--strict"]
            assert calls == [[*verify, str(backend.parents[2])], [*verify, str(backend.parents[6])]]
        else:
            assert calls == []
    assert not backend.exists()


def test_appimage_resolves_the_extracted_backend(tmp_path, monkeypatch):
    artifact = tmp_path / "desktop.AppImage"
    artifact.touch()

    def extract(command, *, cwd, **kwargs):
        assert command == [str(artifact), "--appimage-extract"]
        backend = cwd / "squashfs-root/resources/bin/projectneko_server"
        backend.parent.mkdir(parents=True)
        backend.touch()

    monkeypatch.setattr(check_plugin_tools.subprocess, "run", extract)
    with check_plugin_tools._packaged_backend(artifact) as backend:
        assert backend.as_posix().endswith("/squashfs-root/resources/bin/projectneko_server")
    assert not backend.exists()


def test_portable_tar_rejects_paths_outside_the_extraction_root(tmp_path):
    archive = tmp_path / "desktop.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        entry = tarfile.TarInfo("../escaped")
        entry.size = 1
        bundle.addfile(entry, BytesIO(b"x"))

    with pytest.raises(tarfile.OutsideDestinationError):
        with check_plugin_tools._packaged_backend(archive):
            pytest.fail("The invalid archive should never expose a backend")


def test_packaged_desktop_path_preserves_existing_tool_precedence(tmp_path, monkeypatch):
    home = tmp_path / "home"
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    existing = str(tmp_path / "system" / "bin")
    monkeypatch.setattr(tool_resources.sys, "frozen", True, raising=False)
    monkeypatch.setattr(tool_resources.sys, "platform", "linux")
    monkeypatch.setattr(tool_resources.Path, "home", lambda: home)
    monkeypatch.setenv("PATH", existing)

    assert tool_resources.workbench_tool_path().split(os.pathsep) == [existing, str(user_bin)]
    assert os.get_exec_path() == [existing]
