"""Local GitHub credential selection shared by publishing and status reads."""

from __future__ import annotations

import os
import subprocess

from plugin.neko_plugin_cli.tool_resources import workbench_tool_path


GITHUB_API_URL = "https://api.github.com"


def github_api_url() -> str:
    """Use a local GitHub fixture when running desktop release acceptance."""
    port = os.environ.get("NEKO_PLUGIN_GITHUB_LOOPBACK_PORT", "")
    if port.isascii() and port.isdecimal() and len(port) <= 5 and 1 <= int(port) <= 65535:
        return f"http://127.0.0.1:{int(port)}"
    return GITHUB_API_URL


def gh_auth_token() -> str | None:
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            env={**os.environ, "PATH": workbench_tool_path()},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "neko-plugin",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = (
        os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or gh_auth_token()
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
