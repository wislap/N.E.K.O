"""Background publication tasks for authoring workbench projects."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Literal, NamedTuple
from urllib.parse import quote
from uuid import uuid4

from plugin import settings
from plugin.logging_config import get_logger
from plugin.neko_plugin_cli.repo_state import RepoState
from plugin.server.application.plugin_project_commands import (
    claim_project,
    project_worker_command,
    project_worker_directory,
    release_project,
    start_cli,
    terminate_process_tree,
)
from plugin.server.application.plugin_projects import source_repo_root
from plugin.server.application.plugin_tool_environment import (
    ToolEnvironmentError,
    resolve_tool_environment,
    tool_resources,
)
from plugin.server.application.plugin_publish_status import (
    LocalStatus,
    RemoteGithubState,
    read_local_status,
    read_remote_github,
)
from plugin.server.application.plugins._env_budgets import env_seconds
from plugin.server.domain.plugin_project_errors import ProjectErrorCode as Code
from plugin.server.domain.plugin_project_errors import project_error
from plugin.utils.asyncio_utils import await_cancellation_safe
from utils.file_utils import atomic_write_json, read_json_tolerating_replace

logger = get_logger("server.application.plugin_publish_tasks")

PublishMode = Literal["github_and_market", "github", "market_notify"]
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})
_LOG_LINES = 200
_LOG_LINE_CHARS = 400
_MAX_TASKS = 64
_TERMINATE_GRACE_SECONDS = 15.0
_MIN_RELEASE_WAIT_SECONDS = 60.0
_RELEASE_WORKFLOW = ".github/workflows/release.yml"
# Lines the CLI starts itself; anything else after ``[FAIL]`` continues it.
_MARKER_PATTERN = re.compile(r"^\[[^\]]+\]")
_CAUSE_PATTERN = re.compile(r"^\s*(fatal:|error:|! \[)", re.IGNORECASE)
PublishStage = Literal["checks", "github", "market"]


@dataclass
class _Task:
    task_id: str
    project_id: str
    mode: PublishMode
    status: str = "queued"
    message: str = ""
    release_url: str = ""
    market_status: str | None = None
    market_candidate_id: int | None = None
    market_request_id: int | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_LINES))
    started_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_requested: bool = False
    stop_requested: bool = False
    process: subprocess.Popen[str] | None = None
    preparing: bool = False


# Reentrant: snapshots are taken while the registry lock is already held.
_lock = threading.RLock()
_tasks: dict[str, _Task] = {}
_active: dict[str, str] = {}
_latest: dict[str, str] = {}
_workers: dict[str, asyncio.Task[None]] = {}


def _snapshot(task: _Task) -> dict[str, object]:
    with _lock:
        return {
            "task_id": task.task_id,
            "project_id": task.project_id,
            "mode": task.mode,
            "status": task.status,
            "message": task.message,
            "release_url": task.release_url,
            "market_status": task.market_status,
            "market_candidate_id": task.market_candidate_id,
            "market_request_id": task.market_request_id,
            "log": list(task.log),
            "started_at": task.started_at,
            "updated_at": task.updated_at,
            "finished_at": task.finished_at,
        }


class _Gate(NamedTuple):
    """Everything ``start_publish`` needs after the local preconditions pass."""

    root: Path
    source_dir: Path
    plugin_id: str
    repository: str
    head: str | None
    tag: str


def _push_blockers(state: RepoState) -> list[str]:
    branch = state.branch
    if state.head is None:
        return ["the repository has no commits yet"]
    if state.detached:
        return ["HEAD is detached; check out the branch to publish from"]
    if state.upstream is None:
        return [f"branch {branch} has no upstream; run git push -u origin {branch}"]
    if not state.upstream.startswith("origin/"):
        return [
            f"branch {branch} tracks {state.upstream}, which is not on origin; "
            f"run git push -u origin {branch}"
        ]
    if state.head_pushed is not True:
        return [f"HEAD is not pushed to {state.upstream}; run git push"]
    return []


def _release_blockers(local: LocalStatus) -> list[str]:
    """What ``publish github`` would refuse before it pushes a tag."""
    state = local.repository
    reasons: list[str] = []
    if state.worktree_clean is None:
        reasons.append("git status could not be read")
    elif not state.worktree_clean:
        reasons.append(
            "the git working tree has uncommitted changes: " + ", ".join(state.dirty_paths)
        )
    reasons.extend(_push_blockers(state))
    from plugin.neko_plugin_cli.repo_action_migration import ActionFileStatus

    workflow = local.actions.get(_RELEASE_WORKFLOW)
    if workflow != ActionFileStatus.CURRENT:
        reasons.append(
            f"the standard release workflow {_RELEASE_WORKFLOW} is {workflow or 'missing'}; "
            "apply the workflow update in the plugin workbench"
        )
    if state.tag_exists and state.tag_at_head is False:
        reasons.append(
            f"tag {state.tag} already points at another commit; bump the version in plugin.toml"
        )
    return reasons


def _local_gate(project_id: str, mode: PublishMode) -> _Gate:
    """Refuse to start unless the CLI's own local preconditions already hold."""
    local = read_local_status(project_id)
    state = local.repository
    reasons: list[str] = []
    if not state.git_available:
        reasons.append("git is not available")
    elif not state.is_repository:
        reasons.append("the project directory does not have its own git repository")
    if not state.github_repository:
        reasons.append("git remote origin does not point to a GitHub repository")
    # Notifying the Market only names an existing release; local edits do not matter.
    if state.is_repository and mode != "market_notify":
        reasons.extend(_release_blockers(local))
    if reasons:
        raise project_error(Code.PUBLISH_NOT_READY, "; ".join(reasons), 409)
    assert state.github_repository is not None  # the check above raised without it
    return _Gate(
        root=project_worker_directory(),
        source_dir=local.source_dir,
        plugin_id=local.plugin_id,
        repository=state.github_repository,
        head=state.head,
        tag=state.tag,
    )


def _remote_release_blockers(gate: _Gate, remote: RemoteGithubState) -> list[str]:
    """GitHub facts that make a new release pointless; unknown facts are left to the CLI."""
    reasons: list[str] = []
    if remote["visibility"] == "private":
        reasons.append(
            f"repository {gate.repository} is private; the Market needs a public repository"
        )
    elif remote["visibility"] == "not_found":
        reasons.append(
            f"repository {gate.repository} is not visible on GitHub (missing or private); "
            "the Market needs a public repository"
        )
    if remote["tag"] == "elsewhere":
        reasons.append(
            f"remote tag {gate.tag} already points at another commit; "
            "bump the version in plugin.toml"
        )
    return reasons


def _verified_release_url(gate: _Gate, remote: RemoteGithubState) -> str:
    # The Market rejects releases it cannot verify; check before asking.
    if remote["visibility"] in {"private", "not_found"}:
        raise project_error(
            Code.PUBLISH_NOT_READY, "The Market requires a public GitHub repository", 409
        )
    if remote["rate_limited"]:
        raise project_error(
            Code.PUBLISH_RATE_LIMITED,
            "GitHub rate limit reached; the release cannot be verified right now.",
            429,
        )
    if not remote["reachable"]:
        raise project_error(
            Code.PUBLISH_NOT_READY,
            "GitHub could not be reached; the release cannot be verified right now",
            409,
        )
    release = remote["release"]
    if release["state"] == "unknown":
        raise project_error(
            Code.PUBLISH_NOT_READY, f"release {gate.tag} could not be read from GitHub", 409
        )
    if release["state"] != "published":
        raise project_error(
            Code.PUBLISH_NOT_READY, f"release {gate.tag} is not published yet", 409
        )
    if release["missing_assets"]:
        raise project_error(
            Code.PUBLISH_NOT_READY,
            f"release assets are incomplete: {', '.join(release['missing_assets'])}",
            409,
        )
    if not release["ready"]:
        raise project_error(
            Code.PUBLISH_NOT_READY,
            f"release workflow for {gate.tag} is {release['workflow']}; it must complete successfully",
            409,
        )
    return f"https://github.com/{gate.repository}/releases/tag/{quote(gate.tag, safe='')}"


async def start_publish(
    project_id: str, mode: PublishMode, *, confirmed_head: str | None = None,
    confirmed_tag: str | None = None, confirmed_repository: str | None = None,
) -> dict[str, object]:
    source_root = source_repo_root()
    gate = await asyncio.to_thread(_local_gate, project_id, mode)
    if (
        (confirmed_head is not None and mode != "market_notify" and confirmed_head != gate.head)
        or (confirmed_tag is not None and confirmed_tag != gate.tag)
        or (confirmed_repository is not None and confirmed_repository.casefold() != gate.repository.casefold())
    ):
        raise project_error(Code.PUBLISH_NOT_READY, "The project changed. Refresh and confirm the release again.", 409)
    if mode != "market_notify" and source_root is None:
        try:
            tool_resources()
        except ToolEnvironmentError as exc:
            raise project_error(Code.PUBLISH_UNAVAILABLE, str(exc), 503) from exc
    budget = env_seconds("NEKO_PLUGIN_PUBLISH_BUDGET", 1200.0)
    market_wait = env_seconds("NEKO_PLUGIN_MARKET_WAIT", 1800.0)
    asset_name = None
    release_url = ""
    if mode == "market_notify":
        remote = await read_remote_github(
            gate.repository, gate.head, gate.tag, gate.plugin_id, fresh=True
        )
        release_url = _verified_release_url(gate, remote)
        asset_name = remote["release"].get("asset_name")
    else:
        remote = await read_remote_github(gate.repository, gate.head, gate.tag, gate.plugin_id)
        reasons = _remote_release_blockers(gate, remote)
        if reasons:
            raise project_error(Code.PUBLISH_NOT_READY, "; ".join(reasons), 409)

    task = _Task(
        task_id=uuid4().hex, project_id=project_id, mode=mode, release_url=release_url
    )
    with _lock:
        active_id = _active.get(project_id)
        active = _tasks.get(active_id) if active_id else None
        if active is not None and active.status not in _TERMINAL_STATUSES:
            raise project_error(
                Code.PUBLISH_TASK_ACTIVE,
                f"a publish task is already running for project {project_id}",
                409,
            )
        # Shared with precheck and create-repo: they touch the same tree.
        claim_project(project_id, "publish")
        _prune()
        _tasks[task.task_id] = task
        _active[project_id] = task.task_id
        _latest[project_id] = task.task_id
        snapshot = _snapshot(task)
    worker = asyncio.create_task(
        _publish(task, gate, source_root, budget, market_wait, asset_name),
        name=f"plugin-publish-{task.task_id}",
    )
    _workers[task.task_id] = worker
    worker.add_done_callback(lambda _done, task_id=task.task_id: _workers.pop(task_id, None))
    return snapshot


def latest_publish_task(project_id: str) -> dict[str, object]:
    with _lock:
        task = _tasks.get(_latest.get(project_id, ""))
        if task is not None:
            return _snapshot(task)
    from plugin.server.infrastructure.authoring_project_store import list_records

    if any(record.project_id == project_id for record in list_records()):
        try:
            saved = read_json_tolerating_replace(_publication_path(project_id))
            if (
                isinstance(saved, dict)
                and saved.get("project_id") == project_id
                and isinstance(status := saved.get("status"), str)
                and status in _TERMINAL_STATUSES
            ):
                return saved
        except (OSError, ValueError):
            pass
    raise project_error(Code.PUBLISH_TASK_NOT_FOUND, f"no publish task for project {project_id}", 404)


def _publication_path(project_id: str) -> Path:
    filename = sha256(project_id.encode("utf-8")).hexdigest() + ".json"
    return settings.get_plugin_state_root().parent / ".neko-plugin-publications" / filename


def _save_task(task: _Task, *, interrupted: bool = False) -> None:
    if not task.release_url:
        return
    try:
        snapshot = _snapshot(task)
        # Retain the outcome across restarts without storing Git/CLI output.
        snapshot["log"] = []
        if snapshot["status"] == "failed":
            snapshot["message"] = ""
        if interrupted:
            snapshot.update(
                status="canceled",
                message="GitHub Release is ready; refresh the Market status before retrying the notification.",
                finished_at=time.time(),
            )
        atomic_write_json(_publication_path(task.project_id), snapshot)
    except OSError as exc:
        logger.warning("Could not retain plugin publication status: {}", exc)


async def cancel_publish(project_id: str, task_id: str) -> dict[str, object]:
    with _lock:
        task = _tasks.get(task_id)
        if task is None or task.project_id != project_id:
            raise project_error(Code.PUBLISH_TASK_NOT_FOUND, f"unknown publish task: {task_id}", 404)
        if task.status in _TERMINAL_STATUSES:
            return _snapshot(task)
        task.cancel_requested = True
        worker = _workers.get(task_id)
        preparing = task.preparing
    if preparing and worker is not None:
        worker.cancel()
    await asyncio.to_thread(_terminate, task)
    with _lock:
        return _snapshot(task)


async def shutdown_publications() -> None:
    workers = tuple(_workers.values())
    for worker in workers:
        worker.cancel()
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)


def _prune() -> None:
    """Drop finished tasks once too many accumulated; each project keeps its latest."""
    if len(_tasks) <= _MAX_TASKS:
        return
    keep = set(_latest.values())
    finished = sorted(
        (task for task in _tasks.values() if task.status in _TERMINAL_STATUSES and task.task_id not in keep),
        key=lambda task: task.finished_at or 0.0,
    )
    for task in finished[: len(_tasks) - _MAX_TASKS]:
        _tasks.pop(task.task_id, None)
        if _active.get(task.project_id) == task.task_id:
            _active.pop(task.project_id, None)


def _stream(
    task: _Task, argv: list[str], cwd: Path, env: dict[str, str] | None = None
) -> tuple[int | None, str]:
    """Run the CLI to completion, collecting its output. Called in a thread."""
    with _lock:
        if task.cancel_requested or task.stop_requested:
            return None, ""
    try:
        options = {} if env is None else {"env": env}
        process = start_cli(argv, cwd=cwd, merge_stderr=True, **options)
    except OSError as exc:
        return None, f"publish command could not start: {exc}"
    with _lock:
        stopped_before_registration = task.cancel_requested or task.stop_requested
        if not stopped_before_registration:
            task.process = process
            task.status = "running"
            task.started_at = task.started_at or time.time()
            task.updated_at = time.time()
    if stopped_before_registration:
        terminate_process_tree(process)
        return None, ""
    assert process.stdout is not None
    worker_output = ""
    while line := process.stdout.readline(_LOG_LINE_CHARS + 1):
        with _lock:
            task.log.append(line.rstrip("\n")[:_LOG_LINE_CHARS])
            task.updated_at = time.time()
        if env is None and line.startswith(('{"release_url":', '{"publication":')):
            worker_output = line
            if not line.endswith("\n"):
                worker_output += process.stdout.readline(4096 - len(line))
                line = worker_output
        # Retain one bounded entry per line, discarding oversized tails in chunks.
        while line and not line.endswith("\n"):
            line = process.stdout.readline(_LOG_LINE_CHARS + 1)
    returncode = process.wait()
    with _lock:
        task.process = None
    return returncode, worker_output


def _terminate(task: _Task) -> None:
    with _lock:
        # A timeout can request termination while process creation is in flight.
        task.stop_requested = True
        process = task.process
    if process is not None:
        terminate_process_tree(process)


def _failure_message(task: _Task, returncode: int | None) -> str:
    """The last ``[FAIL]`` line, completed by the cause a multi-line error carries.

    A git error prints its first stderr line on the ``[FAIL]`` line (often
    ``To https://...``); the actual ``fatal:`` / ``! [rejected]`` line follows.
    """
    with _lock:
        lines = list(task.log)
    starts = [index for index, line in enumerate(lines) if "[FAIL]" in line]
    if not starts:
        if returncode is None:
            return "the publish task was interrupted before completion"
        return f"publish exited with code {returncode}"
    start = starts[-1]
    continuation: list[str] = []
    for line in lines[start + 1 :]:
        if _MARKER_PATTERN.match(line):
            break
        if line.strip():
            continuation.append(line.strip())
    causes = [line for line in continuation if _CAUSE_PATTERN.match(line)]
    cause = causes[-1] if causes else (continuation[-1] if continuation else "")
    message = f"{lines[start]} | {cause}" if cause else lines[start]
    return message[:_LOG_LINE_CHARS]


async def _timeout_message(gate: _Gate, budget: float, stage: PublishStage) -> str:
    message = f"publish timed out after {budget:.0f} seconds"
    if stage == "checks":
        return f"{message} during local release checks"
    if stage == "market":
        return f"{message} while notifying the Market; refresh the Market status before retrying"
    remote = await read_remote_github(
        gate.repository, gate.head, gate.tag, gate.plugin_id, fresh=True
    )
    if remote["tag"] != "at_head":
        return message
    return (
        f"{message} after tag {gate.tag} was pushed; the GitHub Release may still be in "
        "progress. Notify the Market once it appears."
    )


async def _run_stage(
    task: _Task, argv: list[str], cwd: Path, budget: float, env: dict[str, str] | None = None
) -> tuple[int | None, str]:
    stream = asyncio.create_task(asyncio.to_thread(_stream, task, argv, cwd, env))
    try:
        return await asyncio.wait_for(asyncio.shield(stream), budget)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if isinstance(exc, asyncio.CancelledError):
            with _lock:
                task.cancel_requested = True
        await asyncio.to_thread(_terminate, task)
        try:
            await asyncio.wait_for(asyncio.shield(stream), _TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            logger.warning("publish stream did not end after terminate: task={}", task.task_id)
        raise


async def _publish(
    task: _Task, gate: _Gate, source_root: Path | None, budget: float,
    market_wait: float, asset_name: str | None,
) -> None:
    returncode = None
    spawn_error = ""
    timeout_message = ""
    stage_budget = budget
    stage: PublishStage = "checks"
    try:
        if task.mode != "market_notify":
            with _lock:
                if task.cancel_requested:
                    return
                task.status = "running"
                task.started_at = task.updated_at = time.time()
                task.log.append("Preparing the plugin check environment")
                task.preparing = True
            try:
                environment = await resolve_tool_environment(source_root)
            finally:
                with _lock:
                    task.preparing = False
            release_deadline = time.monotonic() + budget
            stage_budget = min(
                env_seconds("NEKO_PLUGIN_PRECHECK_BUDGET", 600.0),
                budget,
            )
            returncode, spawn_error = await _run_stage(
                task, environment.command("publish", "github", str(gate.source_dir), "--check-only"),
                environment.cwd, stage_budget, environment.env,
            )
            if returncode != 0 or task.cancel_requested:
                return
            spawn_error = ""
            stage_budget = max(release_deadline - time.monotonic(), _MIN_RELEASE_WAIT_SECONDS)
            release_wait = max(stage_budget - min(30, stage_budget / 2), 1)
            assert gate.head is not None
            stage = "github"
            returncode, output = await _run_stage(
                task,
                project_worker_command("github", str(gate.source_dir), gate.plugin_id,
                                       gate.repository, gate.tag, gate.head, str(release_wait)),
                gate.root, stage_budget,
            )
            if returncode != 0:
                spawn_error = output
                return
            try:
                release = json.loads(output)
                with _lock:
                    task.release_url = release["release_url"]
                    asset_name = release["asset_name"]
            except (ValueError, KeyError, TypeError):
                spawn_error = "GitHub release worker returned no release result"
                return
        if task.mode != "github" and not task.cancel_requested:
            await await_cancellation_safe(asyncio.create_task(
                asyncio.to_thread(_save_task, task, interrupted=True)
            ))
            stage_budget = market_wait + 30
            stage = "market"
            args = [
                "market", task.release_url, str(market_wait), "--endpoint",
                f"{settings.MARKET_API_URL.rstrip('/')}/api/v1/release-publications",
            ]
            if asset_name:
                args.extend(("--asset-name", asset_name))
            returncode, output = await _run_stage(task, project_worker_command(*args), gate.root, stage_budget)
            if output.startswith('{"publication":'):
                publication = json.loads(output)["publication"]
                with _lock:
                    task.market_status = publication["status"]
                    task.market_candidate_id = publication["candidate_id"]
                    task.market_request_id = publication["request_id"]
            else:
                spawn_error = output or ("Market worker returned no publication result" if returncode == 0 else "")
    except TimeoutError:
        timeout_message = await _timeout_message(gate, stage_budget, stage)
    except (ToolEnvironmentError, OSError, ValueError, KeyError, TypeError) as exc:
        spawn_error = str(exc)
    except asyncio.CancelledError:
        task.cancel_requested = True
        raise
    finally:
        try:
            await await_cancellation_safe(asyncio.create_task(asyncio.to_thread(
                _finalize, task, returncode, spawn_error, timeout_message
            )))
        finally:
            release_project(task.project_id)


def _finalize(
    task: _Task, returncode: int | None, spawn_error: str, timeout_message: str
) -> None:
    with _lock:
        if task.status in _TERMINAL_STATUSES:
            return
        if task.cancel_requested:
            task.status = "canceled"
            task.message = "the publish task was canceled"
        elif spawn_error:
            task.status = "failed"
            task.message = spawn_error
        elif timeout_message:
            task.status = "failed"
            task.message = timeout_message
        elif returncode == 0:
            task.status = "succeeded"
        else:
            task.status = "failed"
            task.message = _failure_message(task, returncode)
        task.finished_at = time.time()
        task.updated_at = task.finished_at
    _save_task(task)
    with _lock:
        if _active.get(task.project_id) == task.task_id:
            _active.pop(task.project_id, None)
    logger.info(
        "publish task finished: task={}, mode={}, status={}", task.task_id, task.mode, task.status
    )
