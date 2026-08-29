"""Developer and workspace tools: reading, writing, and patching local files,
and running local Python code."""

import os

from . import sandbox, state, workspace
from .common import run_command, truncate, COMMAND_TIMEOUT

MAX_READ_CHARS = 20000
SEARCH_MAX_MATCHES = 200
SEARCH_IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", "venv", ".venv", "env",
    ".idea", ".vscode", "dist", "build", ".mypy_cache", ".pytest_cache", "target",
}


def read_file(filepath: str) -> str:
    """Read the text contents of a local file."""
    try:
        resolved = workspace.resolve_path(filepath)
    except workspace.WorkspaceError as exc:
        return str(exc)
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            return truncate(f.read(), MAX_READ_CHARS)
    except OSError as exc:
        return f"Failed to read file '{filepath}': {exc}"


def write_file(filepath: str, content: str) -> str:
    """Create or overwrite a local file with the given text content."""
    try:
        resolved = workspace.resolve_path(filepath)
    except workspace.WorkspaceError as exc:
        return str(exc)
    try:
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} chars to '{filepath}'"
    except OSError as exc:
        return f"Failed to write file '{filepath}': {exc}"


def patch_file(filepath: str, search_str: str, replace_str: str) -> str:
    """Replace one exact, unique occurrence of search_str with replace_str."""
    try:
        resolved = workspace.resolve_path(filepath)
    except workspace.WorkspaceError as exc:
        return str(exc)
    try:
        with open(resolved, "r", encoding="utf-8") as f:
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
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as exc:
        return f"Failed to write patched file '{filepath}': {exc}"
    return f"Patched '{filepath}': replaced 1 occurrence ({len(search_str)} chars -> {len(replace_str)} chars)."


def search_in_files(query: str, path: str = ".", extensions: str = "") -> str:
    """Recursively search text files under a directory for a substring, like grep.

    `extensions` is an optional comma-separated filter, e.g. 'py,txt,md'.
    Returns file:line: matching-line entries."""
    try:
        root = workspace.resolve_path(path)
    except workspace.WorkspaceError as exc:
        return str(exc)
    if not os.path.isdir(root):
        return f"'{path}' is not a directory."

    ext_filter = {e.strip().lstrip(".").lower() for e in extensions.split(",") if e.strip()}
    matches = []
    query_lower = query.lower()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SEARCH_IGNORE_DIRS and not d.startswith(".")]
        for filename in filenames:
            if ext_filter and filename.rsplit(".", 1)[-1].lower() not in ext_filter:
                continue
            full = os.path.join(dirpath, filename)
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    for lineno, line in enumerate(f, 1):
                        if query_lower in line.lower():
                            rel = os.path.relpath(full, root)
                            matches.append(f"{rel}:{lineno}: {line.strip()}")
                            if len(matches) >= SEARCH_MAX_MATCHES:
                                matches.append(f"... (stopped at {SEARCH_MAX_MATCHES} matches)")
                                return truncate("\n".join(matches))
            except OSError:
                continue

    if not matches:
        return f"No matches for '{query}'."
    return truncate("\n".join(matches))


def run_python_script(script_path_or_code: str) -> str:
    """Run a local .py file by path, or inline Python source if no matching file exists."""
    candidate = script_path_or_code
    if script_path_or_code.endswith(".py"):
        try:
            candidate = workspace.resolve_path(script_path_or_code)
        except workspace.WorkspaceError as exc:
            return str(exc)
    is_file = os.path.isfile(candidate) and candidate.endswith(".py")
    script_path_or_code = candidate if is_file else script_path_or_code

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
        resolved = workspace.resolve_path(path)
    except workspace.WorkspaceError as exc:
        return str(exc)
    try:
        entries = os.listdir(resolved)
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
    registry.register(
        "search_in_files",
        "Recursively search text/code files under a directory for a substring (like grep), returning "
        "file:line matches. Ideal for finding where something is defined or used across a project.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search for (case-insensitive)."},
                "path": {"type": "string", "description": "Directory to search under. Defaults to current/workspace."},
                "extensions": {
                    "type": "string",
                    "description": "Optional comma-separated extension filter, e.g. 'py,js,md'.",
                },
            },
            "required": ["query"],
        },
        search_in_files,
        category="dev",
    )
