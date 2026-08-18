"""Docker-based sandboxing for arbitrary shell/Python execution.

Only run_shell_command and run_python_script route through this (see
security_utils.py / dev_tools.py) — purpose-built tools like nmap_scan need
real network reachability and specific installed binaries, so containerizing
them would need those binaries baked into the image and defeats the point
for anything that must reach a real target.

Note: docker_available() and the graceful-degradation path (sandbox enabled
but Docker missing) are exercised in this project's test suite against a
machine with no Docker installed. The actual containerized-execution path
itself has not been run end-to-end here — verify it yourself once Docker
Desktop is installed before relying on it for anything sensitive.
"""

import os
import shutil
import subprocess
import tempfile
from typing import Literal

from .common import truncate

DOCKER_IMAGE = "python:3-slim"
CONTAINER_TIMEOUT = 60
MEMORY_LIMIT = "512m"
CPU_LIMIT = "1"


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def run_in_sandbox(payload: str, mode: Literal["shell", "python"], timeout: int = CONTAINER_TIMEOUT) -> str:
    """Runs payload inside a disposable python:3-slim container. Returns a
    formatted stdout/stderr/exit-code block, same shape as
    tools.common.run_command, prefixed with '[sandboxed]' so results are
    distinguishable from direct-host execution."""
    inner_timeout = max(timeout - 5, 1)

    if mode == "shell":
        args = [
            "docker", "run", "--rm",
            "--memory", MEMORY_LIMIT, "--cpus", CPU_LIMIT,
            DOCKER_IMAGE,
            "timeout", str(inner_timeout), "sh", "-c", payload,
        ]
        return _run_docker(args, timeout)

    # python mode: mount a temp script read-only rather than passing code as
    # an argv string, so quoting/escaping of arbitrary source is never a concern.
    tmp_dir = tempfile.mkdtemp(prefix="sandbox_")
    try:
        script_path = os.path.join(tmp_dir, "script.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(payload)
        args = [
            "docker", "run", "--rm",
            "--memory", MEMORY_LIMIT, "--cpus", CPU_LIMIT,
            "-v", f"{tmp_dir}:/code:ro",
            DOCKER_IMAGE,
            "timeout", str(inner_timeout), "python", "/code/script.py",
        ]
        return _run_docker(args, timeout)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_docker(args: list, timeout: int) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        out = result.stdout or ""
        err = result.stderr or ""
        combined = f"[sandboxed, exit code {result.returncode}]\n"
        if out:
            combined += f"--- stdout ---\n{out}\n"
        if err:
            combined += f"--- stderr ---\n{err}\n"
        return truncate(combined)
    except subprocess.TimeoutExpired:
        return f"Sandboxed command timed out after {timeout}s"
    except OSError as exc:
        return f"Failed to run sandboxed command: {exc}"
