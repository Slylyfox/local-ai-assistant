"""Developer and workspace tools: reading, writing, and patching local files,
and running local Python code."""

import os

from . import sandbox, state
from .common import run_command, truncate, COMMAND_TIMEOUT

MAX_READ_CHARS = 20000


def read_file(filepath: str) -> str:
    """Read the text contents of a local file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return truncate(f.read(), MAX_READ_CHARS)
    except OSError as exc:
        return f"Failed to read file '{filepath}': {exc}"


def write_file(filepath: str, content: str) -> str:
    """Create or overwrite a local file with the given text content."""
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} chars to '{filepath}'"
    except OSError as exc:
        return f"Failed to write file '{filepath}': {exc}"


def patch_file(filepath: str, search_str: str, replace_str: str) -> str:
    """Replace one exact, unique occurrence of search_str with replace_str."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        return f"Failed to read file '{filepath}': {exc}"

    count = content.count(search_str)
    if count == 0:
        return f"search_str not found in '{filepath}'. No changes made."
    if count > 1:
        return (
            f"search_str appears {count} times in '{filepath}'; refusing to patch an "
            "ambiguous match. Provide more surrounding context to make search_str unique."
        )

    new_content = content.replace(search_str, replace_str, 1)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as exc:
        return f"Failed to write patched file '{filepath}': {exc}"
    return f"Patched '{filepath}': replaced 1 occurrence ({len(search_str)} chars -> {len(replace_str)} chars)."


def run_python_script(script_path_or_code: str) -> str:
    """Run a local .py file by path, or inline Python source if no matching file exists."""
    is_file = os.path.isfile(script_path_or_code) and script_path_or_code.endswith(".py")

    if state.sandbox_enabled:
        if sandbox.docker_available():
            if is_file:
                try:
                    with open(script_path_or_code, "r", encoding="utf-8") as f:
                        source = f.read()
                except OSError as exc:
                    return f"Failed to read '{script_path_or_code}': {exc}"
            else:
                source = script_path_or_code
            return sandbox.run_in_sandbox(source, mode="python")
        prefix = "⚠ Docker unavailable, ran directly on host instead:\n"
        cmd = ["python", script_path_or_code] if is_file else ["python", "-c", script_path_or_code]
        return prefix + run_command(cmd, timeout=COMMAND_TIMEOUT)

    cmd = ["python", script_path_or_code] if is_file else ["python", "-c", script_path_or_code]
    return run_command(cmd, timeout=COMMAND_TIMEOUT)


def list_directory(path: str = ".") -> str:
    """List the files and folders inside a local directory."""
    try:
        entries = os.listdir(path)
        return truncate("\n".join(sorted(entries)) or "(empty directory)")
    except OSError as exc:
        return f"Failed to list directory '{path}': {exc}"


def register(registry):
    registry.register(
        "read_file",
        "Read the text contents of a local text, code, or configuration file.",
        {
            "type": "object",
            "properties": {"filepath": {"type": "string", "description": "Path to the file to read."}},
            "required": ["filepath"],
        },
        read_file,
        category="dev",
    )
    registry.register(
        "write_file",
        "Create or overwrite a local file with generated code, a script, or a report.",
        {
            "type": "object",
            "properties": {
                "filepath": {"type": "string", "description": "Path to the file to write."},
                "content": {"type": "string", "description": "Text content to write to the file."},
            },
            "required": ["filepath", "content"],
        },
        write_file,
        category="dev",
    )
    registry.register(
        "patch_file",
        "Perform a precise, targeted update on an existing code/text file by replacing one "
        "unique occurrence of search_str with replace_str. Fails safely if the match isn't unique.",
        {
            "type": "object",
            "properties": {
                "filepath": {"type": "string", "description": "Path to the file to patch."},
                "search_str": {"type": "string", "description": "Exact text to find (must be unique in the file)."},
                "replace_str": {"type": "string", "description": "Text to replace it with."},
            },
            "required": ["filepath", "search_str", "replace_str"],
        },
        patch_file,
        category="dev",
    )
    registry.register(
        "run_python_script",
        "Execute a local Python script by file path, or inline Python source code, and capture stdout/stderr.",
        {
            "type": "object",
            "properties": {
                "script_path_or_code": {
                    "type": "string",
                    "description": "Path to a local .py file, or a snippet of Python source code to run inline.",
                }
            },
            "required": ["script_path_or_code"],
        },
        run_python_script,
        category="dev",
    )
    registry.register(
        "list_directory",
        "List files and folders inside a local directory.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to list. Defaults to current directory."}
            },
            "required": [],
        },
        list_directory,
        category="dev",
    )
