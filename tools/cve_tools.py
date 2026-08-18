"""CVE lookup and search against the National Vulnerability Database (NVD).
No API key required for normal interactive use (unauthenticated rate limit
is low but workable); an optional key in Settings raises that limit."""

import requests

from config import load_config
from .common import truncate

NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
NVD_TIMEOUT = 20


def _headers() -> dict:
    api_key = load_config().nvd_api_key.strip()
    return {"apiKey": api_key} if api_key else {}


def _format_vuln(vuln: dict) -> str:
    cve_id = vuln.get("id", "unknown")
    published = vuln.get("published", "unknown")
    descriptions = vuln.get("descriptions", [])
    description = next((d["value"] for d in descriptions if d.get("lang") == "en"), "")

    metrics = vuln.get("metrics", {})
    score_line = "CVSS: not scored"
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        if key in metrics and metrics[key]:
            data = metrics[key][0]["cvssData"]
            score = data.get("baseScore")
            severity = data.get("baseSeverity", metrics[key][0].get("baseSeverity", ""))
            score_line = f"CVSS: {score} ({severity})" if score is not None else score_line
            break

    refs = vuln.get("references", [])
    ref_lines = "\n".join(f"  - {r['url']}" for r in refs[:5])
    ref_note = f"\n\nReferences ({len(refs)} total, showing up to 5):\n{ref_lines}" if refs else ""

    return f"{cve_id}\nPublished: {published}\n{score_line}\n\n{description}{ref_note}"


def cve_lookup(cve_id: str) -> str:
    """Look up a specific CVE by ID (e.g. CVE-2021-44228)."""
    try:
        resp = requests.get(
            NVD_URL, params={"cveId": cve_id}, headers=_headers(), timeout=NVD_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return f"CVE lookup failed: {exc}"

    vulns = data.get("vulnerabilities", [])
    if not vulns:
        return f"No CVE found matching '{cve_id}'."
    return truncate(_format_vuln(vulns[0]["cve"]))


def cve_search(keyword: str, max_results: int = 5) -> str:
    """Search for CVEs matching a keyword."""
    try:
        resp = requests.get(
            NVD_URL,
            params={"keywordSearch": keyword, "resultsPerPage": max(1, min(max_results, 20))},
            headers=_headers(),
            timeout=NVD_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return f"CVE search failed: {exc}"

    vulns = data.get("vulnerabilities", [])
    if not vulns:
        return f"No CVEs found matching '{keyword}'."
    blocks = [_format_vuln(v["cve"]) for v in vulns[:max_results]]
    return truncate("\n\n---\n\n".join(blocks))


def register(registry):
    registry.register(
        "cve_lookup",
        "Look up a specific CVE by ID and return its description, CVSS score/severity, and references.",
        {
            "type": "object",
            "properties": {"cve_id": {"type": "string", "description": "CVE ID, e.g. 'CVE-2021-44228'."}},
            "required": ["cve_id"],
        },
        cve_lookup,
        category="vuln",
    )
    registry.register(
        "cve_search",
        "Search the NVD database for CVEs matching a keyword (e.g. a product name).",
        {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Search term, e.g. 'log4j' or 'ollama'."},
                "max_results": {"type": "integer", "description": "Maximum results to return. Defaults to 5."},
            },
            "required": ["keyword"],
        },
        cve_search,
        category="vuln",
    )
