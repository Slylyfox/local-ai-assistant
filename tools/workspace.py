"""Active-workspace support: lets the user 'Add Folder' so the assistant can
work within a project directory like a CLI coding agent.

Two modes, controlled by tools/state.py:
  - Convenience root (default): relative paths in file/shell tools resolve
    against the workspace folder; absolute paths anywhere still work.
  - Hard sandbox (opt-in): any path that resolves outside the workspace is
    refused, for more sensitive work.

resolve_path() is the single chokepoint every file tool routes through, so
both behaviors are enforced in one place rather than reimplemented per tool.
"""

import os

from . import state

# Directories/files never worth showing in an auto-generated project tree.
_TREE_IGNORE = {
    ".git", "__pycache__", "node_modules", "venv", ".venv", "env",
    ".idea", ".vscode", "dist", "build", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "target", ".next", ".DS_Store",
}
_TREE_MAX_ENTRIES = 400
_TREE_MAX_DEPTH = 4


class WorkspaceError(Exception):
    """Raised when a path violates the active hard-sandbox restriction."""


def has_workspace() -> bool:
    return bool(state.workspace_folder)


def resolve_path(path: str) -> str:
    """Resolve a tool-supplied path against workspace rules.

    - No workspace set: returned unchanged (legacy behavior).
    - Workspace set: relative paths resolve against it; absolute paths are
      kept as-is UNLESS hard sandbox is on, in which case any path landing
      outside the workspace raises WorkspaceError.
    """
    if not state.workspace_folder:
        return path

    root = os.path.abspath(state.workspace_folder)
    combined = path if os.path.isabs(path) else os.path.join(root, path)
    resolved = os.path.abspath(combined)

    if state.workspace_sandboxed:
        # Contain within root: resolved must be root or a descendant.
        if resolved != root and not resolved.startswith(root + os.sep):
            raise WorkspaceError(
                f"Path '{path}' is outside the sandboxed workspace ({root}). "
                "Disable workspace sandboxing in Settings to access paths outside it."
            )
    return resolved


def workspace_cwd() -> str | None:
    """Working directory for shell commands, or None if no workspace."""
    return os.path.abspath(state.workspace_folder) if state.workspace_folder else None


def build_file_tree(root: str | None = None, max_depth: int = _TREE_MAX_DEPTH) -> str:
    """A compact, ignore-aware, depth-limited tree of the workspace, for
    folding into the system prompt so the model sees structure immediately."""
    root = os.path.abspath(root or state.workspace_folder)
    if not root or not os.path.isdir(root):
        return ""

    lines = [f"{os.path.basename(root) or root}/"]
    count = [0]
    truncated = [False]

    def walk(directory: str, prefix: str, depth: int):
        if depth > max_depth or truncated[0]:
            return
        try:
            entries = sorted(
                (e for e in os.scandir(directory) if e.name not in _TREE_IGNORE and not e.name.startswith(".")),
                key=lambda e: e.name.lower(),
            )
        except OSError:
            return
        dirs = [e for e in entries if e.is_dir()]
        files = [e for e in entries if e.is_file()]
        for i, entry in enumerate(dirs + files):
            if count[0] >= _TREE_MAX_ENTRIES:
                truncated[0] = True
                lines.append(f"{prefix}... (tree truncated)")
                return
            is_last = i == len(dirs) + len(files) - 1
            connector = "└── " if is_last else "├── "
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{prefix}{connector}{entry.name}{suffix}")
            count[0] += 1
            if entry.is_dir():
                extension = "    " if is_last else "│   "
                walk(entry.path, prefix + extension, depth + 1)

    walk(root, "", 1)
    return "\n".join(lines)


def workspace_prompt_context() -> str:
    """The system-prompt snippet describing the active workspace, or ''."""
    if not state.workspace_folder:
        return ""
    root = os.path.abspath(state.workspace_folder)
    tree = build_file_tree(root)
    mode = "sandboxed (paths confined to this folder)" if state.workspace_sandboxed else "convenience root"
    return (
        f"\n\n--- Active workspace ---\n"
        f"You are working inside this project folder ({mode}):\n{root}\n\n"
        f"Relative paths in file tools resolve against it. Current structure:\n{tree}\n"
        f"--- end workspace ---"
    )
