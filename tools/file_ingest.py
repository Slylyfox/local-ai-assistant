"""Local file ingestion for chat attachments: extracts a text representation
from an uploaded document so it can be folded into the prompt.

Used directly by the GUI's "Attach File" flow, not registered as an
agent-callable tool — attaching a file is a direct user action (like typing
a message), not autonomous tool execution, so it isn't gated by the tool
execution toggle."""

import os

MAX_INGEST_CHARS = 20000

TEXT_EXTENSIONS = {".txt", ".log", ".py", ".json", ".csv"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".pcap"}


def _truncate(text: str, limit: int = MAX_INGEST_CHARS) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n...[truncated, {len(text)} chars total]"
    return text


def _extract_text(filepath: str) -> str:
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _extract_pdf(filepath: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "PDF support requires the 'pypdf' package (pip install pypdf)."
    try:
        reader = PdfReader(filepath)
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(pages).strip()
        return text or "(no extractable text found in this PDF — it may be scanned/image-based)"
    except Exception as exc:  # noqa: BLE001
        return f"Failed to parse PDF: {exc}"


def _extract_pcap(filepath: str, limit: int = 80) -> str:
    try:
        from scapy.utils import rdpcap
    except ImportError:
        return "Pcap support requires the 'scapy' package (pip install scapy)."
    try:
        packets = rdpcap(filepath)
    except Exception as exc:  # noqa: BLE001
        return f"Failed to parse pcap file: {exc}"

    lines = [f"Total packets: {len(packets)}"]
    for i, pkt in enumerate(packets[:limit]):
        try:
            lines.append(f"[{i}] {pkt.summary()}")
        except Exception:  # noqa: BLE001
            continue
    if len(packets) > limit:
        lines.append(f"...[{len(packets) - limit} more packets omitted]")
    return "\n".join(lines)


def extract_file_for_prompt(filepath: str) -> str:
    """Return a text representation of filepath suitable for folding into a prompt."""
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".pdf":
            content = _extract_pdf(filepath)
        elif ext == ".pcap":
            content = _extract_pcap(filepath)
        else:
            content = _extract_text(filepath)
    except OSError as exc:
        content = f"Failed to read file '{filepath}': {exc}"
    return _truncate(content)
