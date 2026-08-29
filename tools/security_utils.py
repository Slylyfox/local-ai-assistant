"""General-purpose shell access and security-adjacent data utilities
(encoding, hashing, JWT inspection)."""

import base64
import hashlib
import json
import urllib.parse
from typing import Optional

from . import sandbox, state, workspace
from .common import run_command

SUPPORTED_OPS = (
    "base64_encode",
    "base64_decode",
    "hex_encode",
    "hex_decode",
    "url_encode",
    "url_decode",
    "md5",
    "sha1",
    "sha256",
    "jwt_decode",
)


def run_shell_command(command: str) -> str:
    """Execute a shell/terminal command on the local machine and return stdout/stderr."""
    # A workspace, when set, is the command's working directory. Under hard
    # sandbox with no workspace there's no safe cwd to confine to, so refuse.
    if state.workspace_sandboxed and not workspace.has_workspace():
        return "Workspace sandboxing is on but no workspace folder is set; refusing to run a shell command."
    cwd = workspace.workspace_cwd()

    if state.sandbox_enabled:
        if sandbox.docker_available():
            return sandbox.run_in_sandbox(command, mode="shell")
        prefix = "⚠ Docker unavailable, ran directly on host instead:\n"
        return prefix + run_command(command, shell=True, cwd=cwd)
    return run_command(command, shell=True, cwd=cwd)


def _pad_b64(s: str) -> str:
    return s + "=" * (-len(s) % 4)


def crypto_utility(operation: str, data: str, key: Optional[str] = None) -> str:
    """Base64/hex/URL encode-decode, MD5/SHA1/SHA256 hashing, or JWT header/payload decoding."""
    op = (operation or "").lower().strip()
    try:
        if op == "base64_encode":
            return base64.b64encode(data.encode()).decode()
        if op == "base64_decode":
            return base64.b64decode(data.encode()).decode(errors="replace")
        if op == "hex_encode":
            return data.encode().hex()
        if op == "hex_decode":
            return bytes.fromhex(data).decode(errors="replace")
        if op == "url_encode":
            return urllib.parse.quote(data)
        if op == "url_decode":
            return urllib.parse.unquote(data)
        if op == "md5":
            return hashlib.md5(data.encode()).hexdigest()
        if op == "sha1":
            return hashlib.sha1(data.encode()).hexdigest()
        if op == "sha256":
            return hashlib.sha256(data.encode()).hexdigest()
        if op == "jwt_decode":
            parts = data.split(".")
            if len(parts) < 2:
                return "Not a valid JWT (expected at least header.payload)."
            header = base64.urlsafe_b64decode(_pad_b64(parts[0])).decode(errors="replace")
            payload = base64.urlsafe_b64decode(_pad_b64(parts[1])).decode(errors="replace")
            try:
                header = json.dumps(json.loads(header), indent=2)
            except json.JSONDecodeError:
                pass
            try:
                payload = json.dumps(json.loads(payload), indent=2)
            except json.JSONDecodeError:
                pass
            return f"Header:\n{header}\n\nPayload:\n{payload}\n\n(signature not verified)"
        return f"Unknown operation '{operation}'. Supported: {', '.join(SUPPORTED_OPS)}."
    except Exception as exc:  # noqa: BLE001 - report malformed input back to the model
        return f"crypto_utility failed: {exc}"


def register(registry):
    registry.register(
        "run_shell_command",
        "Execute a shell/terminal command on the user's local machine and return stdout/stderr.",
        {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The full shell command to execute."}},
            "required": ["command"],
        },
        run_shell_command,
        category="security",
    )
    registry.register(
        "crypto_utility",
        "Encode/decode/hash text. Operations: " + ", ".join(SUPPORTED_OPS) + ". jwt_decode reads the "
        "header/payload only and does not verify the signature.",
        {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "description": "One of: " + ", ".join(SUPPORTED_OPS) + "."},
                "data": {"type": "string", "description": "The input text/data to operate on."},
                "key": {"type": "string", "description": "Reserved for future HMAC support; currently unused."},
            },
            "required": ["operation", "data"],
        },
        crypto_utility,
        category="security",
    )
