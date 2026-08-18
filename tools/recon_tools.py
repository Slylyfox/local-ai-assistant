"""Reconnaissance and network tools for authorized local security research.

These wrap standard, widely-used assessment tools (nmap, subfinder/dnsrecon)
and the Python standard library. They act only against whatever target the
user/model supplies, and every call still goes through the app's tool
execution toggle and per-call confirmation dialog — nothing here scans or
sends anything on its own."""

import socket
import ssl
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests

from .common import run_command, truncate, which

NMAP_TIMEOUT = 300
RECON_TIMEOUT = 120
HTTP_TIMEOUT = 15
TLS_TIMEOUT = 10


def nmap_scan(target: str, flags: str = "-sV -T4") -> str:
    """Run a local Nmap scan against a target and return formatted output."""
    if not which("nmap"):
        return "nmap is not installed or not on PATH. Install it from nmap.org and try again."
    args = ["nmap"] + flags.split() + [target]
    return run_command(args, timeout=NMAP_TIMEOUT)


def parse_nmap_file(filepath: str) -> str:
    """Parse a local .nmap or .xml Nmap output file into open ports/services/OS guesses."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError as exc:
        return f"Failed to read file '{filepath}': {exc}"

    stripped = raw.strip()
    if not stripped.startswith("<"):
        return truncate(raw)

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return f"Failed to parse '{filepath}' as nmap XML: {exc}"

    lines = []
    for host in root.findall("host"):
        addr_el = host.find("address")
        addr = addr_el.get("addr") if addr_el is not None else "unknown"
        status_el = host.find("status")
        status = status_el.get("state") if status_el is not None else "unknown"
        lines.append(f"Host: {addr} ({status})")

        osmatch = host.find("os/osmatch")
        if osmatch is not None:
            lines.append(f"  OS guess: {osmatch.get('name')} ({osmatch.get('accuracy')}%)")

        ports_el = host.find("ports")
        if ports_el is not None:
            for port in ports_el.findall("port"):
                portid = port.get("portid")
                proto = port.get("protocol")
                state_el = port.find("state")
                state = state_el.get("state") if state_el is not None else "?"
                service_el = port.find("service")
                service = ""
                if service_el is not None:
                    name = service_el.get("name", "")
                    product = service_el.get("product", "")
                    version = service_el.get("version", "")
                    service = " ".join(p for p in (name, product, version) if p)
                lines.append(f"  {portid}/{proto} {state} {service}".rstrip())
        lines.append("")

    if not lines:
        return "No hosts found in the nmap XML output."
    return truncate("\n".join(lines))


def inspect_web_target(url: str) -> str:
    """Fetch HTTP status/headers and TLS certificate details for a URL."""
    if not urlparse(url).scheme:
        url = "https://" + url

    lines = [f"Target: {url}"]
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        lines.append(f"Status: {resp.status_code} {resp.reason}")
        lines.append(f"Final URL: {resp.url}")
        lines.append("Headers:")
        for k, v in resp.headers.items():
            lines.append(f"  {k}: {v}")
    except requests.RequestException as exc:
        lines.append(f"HTTP request failed: {exc}")

    parsed = urlparse(url)
    if parsed.scheme == "https":
        host = parsed.hostname
        port = parsed.port or 443
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=TLS_TIMEOUT) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert()
            lines.append("")
            lines.append("TLS certificate:")
            subject = dict(x[0] for x in cert.get("subject", []))
            issuer = dict(x[0] for x in cert.get("issuer", []))
            lines.append(f"  Subject: {subject}")
            lines.append(f"  Issuer: {issuer}")
            lines.append(f"  Not before: {cert.get('notBefore')}")
            lines.append(f"  Not after: {cert.get('notAfter')}")
            san = cert.get("subjectAltName", [])
            if san:
                lines.append(f"  SAN: {[v for _, v in san]}")
        except (ssl.SSLError, OSError) as exc:
            lines.append(f"TLS inspection failed: {exc}")

    return truncate("\n".join(lines))


def subdomain_recon(domain: str) -> str:
    """Passive subdomain discovery using a locally installed subfinder or dnsrecon."""
    for tool, args in (
        ("subfinder", ["-d", domain, "-silent"]),
        ("dnsrecon", ["-d", domain, "-t", "brt"]),
    ):
        if which(tool):
            return run_command([tool] + args, timeout=RECON_TIMEOUT)
    return (
        "Neither 'subfinder' nor 'dnsrecon' was found on PATH. "
        "Install one of them to enable subdomain reconnaissance."
    )


def register(registry):
    registry.register(
        "nmap_scan",
        "Run a local Nmap scan against a specified target and return formatted results. "
        "Requires nmap to be installed and on PATH.",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "IP, hostname, or CIDR range to scan."},
                "flags": {
                    "type": "string",
                    "description": "Nmap flags, e.g. '-sV -T4' or '-p 1-1000'. Defaults to '-sV -T4'.",
                },
            },
            "required": ["target"],
        },
        nmap_scan,
        category="recon",
    )
    registry.register(
        "parse_nmap_file",
        "Ingest a local .nmap or .xml Nmap output file and extract open ports, services, and OS guesses.",
        {
            "type": "object",
            "properties": {
                "filepath": {"type": "string", "description": "Path to a .nmap or .xml nmap output file."}
            },
            "required": ["filepath"],
        },
        parse_nmap_file,
        category="recon",
    )
    registry.register(
        "inspect_web_target",
        "Fetch HTTP status/headers and TLS certificate details for a URL, for quick security triage.",
        {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL or hostname to inspect."}},
            "required": ["url"],
        },
        inspect_web_target,
        category="recon",
    )
    registry.register(
        "subdomain_recon",
        "Passive subdomain discovery for a domain using a locally installed tool "
        "(subfinder or dnsrecon, whichever is available).",
        {
            "type": "object",
            "properties": {"domain": {"type": "string", "description": "Domain to enumerate subdomains for."}},
            "required": ["domain"],
        },
        subdomain_recon,
        category="recon",
    )
