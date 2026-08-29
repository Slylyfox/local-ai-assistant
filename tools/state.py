"""Small shared mutable flags the GUI pushes into.

Background jobs (scheduled tasks, directory watches) run on their own
timers outside any single request/response cycle, so they can't read the
GUI's toggles directly. main.py updates these flags whenever the user flips
a switch; other modules read them before running anything, so e.g.
disabling tool execution pauses background automation too, not just
interactive tool calls.
"""

tool_execution_enabled = False
sandbox_enabled = False

# Active workspace folder (see tools/workspace.py). Empty = no workspace: tools
# behave exactly as before. When set, relative paths resolve against it.
workspace_folder = ""
# When True, file/shell tools refuse any path resolving outside the workspace
# (hard sandbox). When False, the workspace is just a convenience root and
# absolute paths anywhere still work.
workspace_sandboxed = False
