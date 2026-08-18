"""Shared helpers for tool implementations: output capping, subprocess execution,
and binary-availability checks."""

import shutil
import subprocess
from typing import Optional, Union

MAX_OUTPUT_CHARS = 8000
COMMAND_TIMEOUT = 60


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n...[truncated, {len(text)} chars total]"
    return text


def which(binary: str) -> Optional[str]:
    return shutil.which(binary)


def run_command(
    args: Union[str, list],
    shell: bool = False,
    timeout: int = COMMAND_TIMEOUT,
    cwd: Optional[str] = None,
) -> str:
    """Run a subprocess and return a formatted stdout/stderr/exit-code block."""
    try:
        result = subprocess.run(
            args,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        out = result.stdout or ""
        err = result.stderr or ""
        combined = f"[exit code {result.returncode}]\n"
        if out:
            combined += f"--- stdout ---\n{out}\n"
        if err:
            combined += f"--- stderr ---\n{err}\n"
        return truncate(combined)
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s"
    except OSError as exc:
        return f"Failed to execute command: {exc}"
