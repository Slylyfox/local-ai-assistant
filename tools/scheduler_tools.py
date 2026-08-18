"""Background task automation: schedule any other registered tool to run on
an interval/cron/one-shot timer, or watch a directory for new files.

Safety model: creating a schedule or watch is itself a normal tool call, so
it goes through the GUI's usual per-call confirmation dialog exactly once
(you see precisely what will run and how often before approving it). After
that it runs unattended — that's the point of automation — but every firing
checks tools.state.tool_execution_enabled first, so switching the master
toggle off pauses all background jobs immediately, and every firing's output
goes to Task History rather than the live chat, per the "don't clutter the
active chat stream" requirement."""

import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import state, task_log

NON_SCHEDULABLE = {"schedule_task", "cancel_task", "list_scheduled_tasks", "watch_directory"}
RESULT_PREVIEW_CHARS = 400

_scheduler = BackgroundScheduler()
_scheduler.start()
_jobs_meta: dict[str, dict] = {}
_registry_ref = None


def _run_scheduled_tool(job_id: str, tool_name: str, arguments: dict) -> None:
    if not state.tool_execution_enabled:
        task_log.log_event(f"[task {job_id}] skipped '{tool_name}' — tool execution is disabled in Settings")
        return
    if _registry_ref is None:
        return
    try:
        result = _registry_ref.call(tool_name, arguments)
    except Exception as exc:  # noqa: BLE001
        result = f"error: {exc}"
    preview = result if len(result) <= RESULT_PREVIEW_CHARS else result[:RESULT_PREVIEW_CHARS] + "...[truncated]"
    task_log.log_event(f"[task {job_id}] {tool_name}({arguments}) -> {preview}")


def schedule_task(
    tool_name: str,
    arguments: Optional[dict] = None,
    interval_seconds: int = 0,
    cron_expression: str = "",
    run_once_in_seconds: int = 0,
) -> str:
    """Schedule another registered tool to run automatically in the background."""
    if _registry_ref is None or tool_name not in _registry_ref.names():
        return f"Unknown tool: {tool_name}"
    if tool_name in NON_SCHEDULABLE:
        return f"'{tool_name}' cannot itself be scheduled."

    arguments = arguments or {}

    if interval_seconds:
        trigger = IntervalTrigger(seconds=interval_seconds)
        desc = f"every {interval_seconds}s"
    elif cron_expression:
        try:
            trigger = CronTrigger.from_crontab(cron_expression)
        except ValueError as exc:
            return f"Invalid cron_expression: {exc}"
        desc = f"cron '{cron_expression}'"
    elif run_once_in_seconds:
        trigger = DateTrigger(run_date=datetime.now() + timedelta(seconds=run_once_in_seconds))
        desc = f"once in {run_once_in_seconds}s"
    else:
        return "Provide one of interval_seconds, cron_expression, or run_once_in_seconds."

    job_id = uuid.uuid4().hex[:8]
    _scheduler.add_job(_run_scheduled_tool, trigger, args=[job_id, tool_name, arguments], id=job_id)
    _jobs_meta[job_id] = {"tool": tool_name, "arguments": arguments, "schedule": desc}
    task_log.log_event(f"[task {job_id}] scheduled: {tool_name}({arguments}) [{desc}]")
    return f"Scheduled task '{job_id}': {tool_name} ({desc}). Runs unattended — check Task History for output."


def list_scheduled_tasks() -> str:
    if not _jobs_meta:
        return "No scheduled tasks."
    lines = []
    for job_id, meta in _jobs_meta.items():
        job = _scheduler.get_job(job_id)
        next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if job and job.next_run_time else "N/A"
        lines.append(f"{job_id}: {meta['tool']}({meta['arguments']}) [{meta['schedule']}] next_run={next_run}")
    return "\n".join(lines)


def cancel_task(task_id: str) -> str:
    if task_id not in _jobs_meta:
        return f"No scheduled task with id '{task_id}'."
    try:
        _scheduler.remove_job(task_id)
    except Exception:  # noqa: BLE001
        pass
    del _jobs_meta[task_id]
    task_log.log_event(f"[task {task_id}] cancelled")
    return f"Cancelled task '{task_id}'."


def watch_directory(directory_path: str, file_extension: str, poll_seconds: int = 10) -> str:
    """Poll a directory for new files matching file_extension; log a parsed
    summary of each new file to Task History as it appears."""
    from . import file_ingest

    if not os.path.isdir(directory_path):
        return f"'{directory_path}' is not a directory."

    ext = file_extension if file_extension.startswith(".") else f".{file_extension}"
    seen = set(os.listdir(directory_path))
    job_id = f"watch-{uuid.uuid4().hex[:8]}"

    def poll():
        nonlocal seen
        if not state.tool_execution_enabled:
            return
        try:
            current = set(os.listdir(directory_path))
        except OSError:
            return
        new_files = sorted(f for f in (current - seen) if f.lower().endswith(ext.lower()))
        seen = current
        for name in new_files:
            path = os.path.join(directory_path, name)
            try:
                summary = file_ingest.extract_file_for_prompt(path)
            except Exception as exc:  # noqa: BLE001
                summary = f"failed to parse: {exc}"
            preview = summary if len(summary) <= 600 else summary[:600] + "...[truncated]"
            task_log.log_event(f"[watch {job_id}] new file '{name}':\n{preview}")

    _scheduler.add_job(poll, IntervalTrigger(seconds=poll_seconds), id=job_id)
    _jobs_meta[job_id] = {
        "tool": "watch_directory",
        "arguments": {"directory_path": directory_path, "file_extension": ext},
        "schedule": f"poll every {poll_seconds}s",
    }
    task_log.log_event(f"[{job_id}] watching '{directory_path}' for new '{ext}' files (poll every {poll_seconds}s)")
    return f"Watching '{directory_path}' for new '{ext}' files (job '{job_id}'). New files are summarized in Task History."


def shutdown() -> None:
    try:
        _scheduler.shutdown(wait=False)
    except Exception:  # noqa: BLE001
        pass


def register(registry):
    global _registry_ref
    _registry_ref = registry

    registry.register(
        "schedule_task",
        "Schedule another registered tool to run automatically in the background, on an interval, cron "
        "schedule, or a one-time delay. Runs unattended after this call is approved — output goes to Task "
        "History, not the chat. Use list_scheduled_tasks/cancel_task to manage it.",
        {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "description": "Name of a registered tool to run."},
                "arguments": {"type": "object", "description": "Arguments to pass to that tool."},
                "interval_seconds": {"type": "integer", "description": "Run every N seconds."},
                "cron_expression": {
                    "type": "string",
                    "description": "Standard 5-field cron expression, e.g. '0 * * * *'.",
                },
                "run_once_in_seconds": {"type": "integer", "description": "Run once, N seconds from now."},
            },
            "required": ["tool_name"],
        },
        schedule_task,
        category="automation",
    )
    registry.register(
        "list_scheduled_tasks",
        "List all currently scheduled background tasks and directory watches.",
        {"type": "object", "properties": {}, "required": []},
        list_scheduled_tasks,
        category="automation",
    )
    registry.register(
        "cancel_task",
        "Cancel a scheduled background task or directory watch by its id.",
        {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task id returned by schedule_task or watch_directory.",
                }
            },
            "required": ["task_id"],
        },
        cancel_task,
        category="automation",
    )
    registry.register(
        "watch_directory",
        "Watch a local directory and automatically parse/summarize any new file with the given extension "
        "(e.g. .pcap or .log) as it appears, logging the summary to Task History. Runs unattended after "
        "this call is approved. Use cancel_task to stop it.",
        {
            "type": "object",
            "properties": {
                "directory_path": {"type": "string", "description": "Directory to watch."},
                "file_extension": {
                    "type": "string",
                    "description": "File extension to watch for, e.g. '.pcap' or '.log'.",
                },
                "poll_seconds": {
                    "type": "integer",
                    "description": "How often to check the directory, in seconds. Defaults to 10.",
                },
            },
            "required": ["directory_path", "file_extension"],
        },
        watch_directory,
        category="automation",
    )
