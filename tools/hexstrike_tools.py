"""Integration with HexStrike AI (https://github.com/0x4m4/hexstrike-ai) — an
open-source security tool orchestration server exposing 80+ pentesting tools
(nmap, gobuster, sqlmap, hydra, metasploit, etc.) over a local REST API.

This talks directly to HexStrike's REST backend (hexstrike_server.py, default
port 8888) rather than going through its MCP wrapper layer — that wrapper is
just a thin bridge making the same HTTP calls, so this is a more direct path
to the same functionality without needing an MCP client implementation.

Intended for a locally/lab-hosted HexStrike instance (e.g. a Parrot/Kali VM on
your own network) against targets you're explicitly authorized to test.
HexStrike itself ships with no built-in authentication — treat network access
to it as equivalent to shell access, same as this app's own run_shell_command,
and don't expose its port beyond your lab network."""

from typing import Optional

import requests

from config import load_config
from .common import truncate

REQUEST_TIMEOUT = 1800  # some scans (full nmap, hydra brute force) run long
HEALTH_TIMEOUT = 10

# Broad (not exhaustive) list of tool names HexStrike exposes as
# POST /api/tools/<name> endpoints, for discovery via hexstrike_list_tools().
KNOWN_TOOLS = [
    "amass", "angr", "anew", "api_fuzzer", "api_schema_analyzer", "arjun",
    "arp-scan", "autorecon", "binwalk", "browser-agent",
    "burpsuite-alternative", "checkov", "checksec", "clair",
    "cloudmapper", "dalfox", "dirb", "dirsearch", "docker-bench-security",
    "dnsenum", "dotdotpwn", "enum4linux", "enum4linux-ng", "exiftool",
    "falco", "feroxbuster", "ffuf", "fierce", "foremost", "gau",
    "gdb", "gdb-peda", "ghidra", "gobuster", "graphql_scanner", "hakrawler",
    "hashcat", "hashpump", "http-framework", "httpx", "hydra", "jaeles",
    "john", "jwt_analyzer", "katana", "kube-bench", "kube-hunter",
    "libc-database", "masscan", "metasploit", "msfvenom", "nbtscan",
    "netexec", "nikto", "nmap", "nmap-advanced", "nuclei", "objdump",
    "one-gadget", "pacu", "paramspider", "prowler", "pwninit", "pwntools",
    "qsreplace", "radare2", "responder", "ropgadget", "ropper", "rpcclient",
    "rustscan", "scout-suite", "smbmap", "sqlmap", "steghide", "strings",
    "subfinder", "terrascan", "trivy", "uro", "volatility", "volatility3",
    "wafw00f", "waybackurls", "wfuzz", "wpscan", "x8", "xsser", "xxd", "zap",
]


def _base_url() -> str:
    return load_config().hexstrike_base_url.strip().rstrip("/")


def _enabled() -> bool:
    return load_config().hexstrike_enabled


def hexstrike_health() -> str:
    """Check connectivity to the configured HexStrike server and how many tools it reports."""
    if not _enabled():
        return "HexStrike integration is disabled in Settings."
    base = _base_url()
    if not base:
        return "No HexStrike server URL configured in Settings."
    try:
        resp = requests.get(f"{base}/health", timeout=HEALTH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return f"Could not reach HexStrike server at {base}: {exc}"
    return (
        f"HexStrike server: {data.get('status', 'unknown')} "
        f"(v{data.get('version', '?')}), "
        f"{data.get('total_tools_count', '?')} tools available."
    )


def hexstrike_list_tools() -> str:
    """List known HexStrike tool names callable via hexstrike_run_tool."""
    return ", ".join(KNOWN_TOOLS)


def hexstrike_run_tool(tool_name: str, params: Optional[dict] = None) -> str:
    """Run a HexStrike-hosted security tool against a target."""
    if not _enabled():
        return "HexStrike integration is disabled in Settings. Enable it and set the server URL first."
    base = _base_url()
    if not base:
        return "No HexStrike server URL configured in Settings."

    try:
        resp = requests.post(
            f"{base}/api/tools/{tool_name}", json=params or {}, timeout=REQUEST_TIMEOUT
        )
    except requests.Timeout:
        return (
            f"HexStrike tool '{tool_name}' timed out after {REQUEST_TIMEOUT}s. "
            "Long-running scans may still be executing on the server."
        )
    except requests.RequestException as exc:
        return f"Could not reach HexStrike server at {base}: {exc}"

    try:
        data = resp.json()
    except ValueError:
        return f"HexStrike returned a non-JSON response (HTTP {resp.status_code}): {resp.text[:500]}"

    if not resp.ok:
        return f"HexStrike tool '{tool_name}' failed (HTTP {resp.status_code}): {data.get('error', data)}"
    if "error" in data:
        return f"HexStrike tool '{tool_name}' error: {data['error']}"

    lines = [f"[{tool_name}] exit code {data.get('return_code', '?')}"]
    stdout = data.get("stdout", "")
    stderr = data.get("stderr", "")
    if stdout:
        lines.append(f"--- stdout ---\n{stdout}")
    if stderr:
        lines.append(f"--- stderr ---\n{stderr}")
    return truncate("\n".join(lines))


def register(registry):
    registry.register(
        "hexstrike_health",
        "Check connectivity to the configured HexStrike security-tool server and see how many tools it has available.",
        {"type": "object", "properties": {}, "required": []},
        hexstrike_health,
        category="hexstrike",
    )
    registry.register(
        "hexstrike_list_tools",
        "List the security tool names available via hexstrike_run_tool (nmap, gobuster, sqlmap, hydra, "
        "metasploit, and 80+ others).",
        {"type": "object", "properties": {}, "required": []},
        hexstrike_list_tools,
        category="hexstrike",
    )
    registry.register(
        "hexstrike_run_tool",
        "Run a security tool hosted on the configured HexStrike server against a target. Example: "
        "tool_name='nmap' with params={'target': '10.0.0.5', 'scan_type': '-sCV'}; or tool_name='gobuster' "
        "with params={'url': 'http://10.0.0.5', 'mode': 'dir', 'wordlist': '/usr/share/wordlists/dirb/common.txt'}. "
        "Call hexstrike_list_tools first if unsure which tools are available. Only use against systems you "
        "are explicitly authorized to test.",
        {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "HexStrike tool name, e.g. 'nmap', 'gobuster', 'sqlmap'.",
                },
                "params": {
                    "type": "object",
                    "description": "Tool-specific parameters as a JSON object, e.g. {'target': '10.0.0.5'}.",
                },
            },
            "required": ["tool_name"],
        },
        hexstrike_run_tool,
        category="hexstrike",
    )
