"""Locate the plugin tools used by the desktop workbench."""

import os
import sys
from pathlib import Path


def workbench_tool_path() -> str:
    path = os.get_exec_path()
    if (getattr(sys, "frozen", False) or "__compiled__" in globals()) and sys.platform in {"darwin", "linux"}:
        directories = [str(Path.home() / ".local" / "bin")]
        if sys.platform == "darwin":
            directories.extend(["/opt/homebrew/bin", "/usr/local/bin"])
        path.extend(directory for directory in directories if directory not in path and Path(directory).is_dir())
    return os.pathsep.join(path)


def plugin_tool_resources_dir(backend: Path) -> Path:
    directory = backend.resolve().parent
    contents = directory.parent
    if directory.name == "MacOS" and contents.name == "Contents" and contents.parent.suffix == ".app":
        # Keep data in the resource directory covered by the bundle signature.
        directory = contents / "Resources"
    return directory / "plugin-tools"
