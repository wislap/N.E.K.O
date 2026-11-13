"""Subprocess entry point for the workbench's Git and Market operations."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path


def main(argv: list[str] | None = None, *, build_commit: str | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("probe")
    create = commands.add_parser("create-repo")
    create.add_argument("plugin", type=Path)
    create.add_argument("--private", action="store_true")
    github = commands.add_parser("github")
    github.add_argument("directory", type=Path)
    github.add_argument("plugin_id")
    github.add_argument("repository")
    github.add_argument("tag")
    github.add_argument("head")
    github.add_argument("timeout", type=float)
    market = commands.add_parser("market")
    market.add_argument("url")
    market.add_argument("wait", type=float)
    market.add_argument("--asset-name")
    market.add_argument("--endpoint", required=True)
    args = parser.parse_args(argv)
    if args.operation == "probe":
        print(json.dumps({"build_commit": build_commit}), flush=True)
        return 0
    import httpx

    try:
        if args.operation == "create-repo":
            from .commands import init_cmd

            return init_cmd.create_github_repository(
                args.plugin, owner=None, private=args.private,
                on_created=lambda url: print(json.dumps({"repository_url": url}), flush=True),
            )
        from .commands import publish_cmd

        if args.operation == "github":
            from plugin.utils.github_auth import github_api_url

            release = publish_cmd.publish_github_release(
                args.directory, plugin_id=args.plugin_id, repository=args.repository,
                tag=args.tag, head=args.head, timeout=args.timeout, api_url=github_api_url(),
            )
            print(json.dumps({"release_url": release.url, "asset_name": release.asset_name}), flush=True)
            return 0

        def report(publication: publish_cmd.MarketPublication) -> None:
            print(json.dumps({"publication": asdict(publication)}), flush=True)

        return publish_cmd._notify_market(
            args.url, wait=args.wait, asset_name=args.asset_name, on_publication=report,
            endpoint=args.endpoint,
        )
    except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
        print(f"[FAIL] {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
