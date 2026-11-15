import re

RECORDED_EVENT = re.compile(
    r"\ARecorded event (?P<recorded_at>\S+) \| actor (?P<actor>[^|\n]+?) \| role (?P<role>[^\n]+?)[ \t]*(?:\n(?P<body>.*))?\Z",
    re.S,
)
SUMMARY_SOURCE_ID = re.compile(r"\A\[(?P<source_id>[^\]]+)\]\s*")
BLOCK_NODES = {"doc", "bulletList", "orderedList", "listItem", "blockquote", "table", "tableRow", "tableCell", "panel"}


def text_to_adf(text: str) -> dict:
    paragraphs = []
    for line in text.split("\n"):
        paragraphs.append({"type": "paragraph", "content": [{"type": "text", "text": line}] if line else []})
    return {"type": "doc", "version": 1, "content": paragraphs}


def adf_to_text(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    parts = [adf_to_text(child) for child in node.get("content", [])]
    return ("\n" if kind in BLOCK_NODES else "").join(parts)


def parse_recorded_event(text: str) -> dict | None:
    match = RECORDED_EVENT.match(text)
    if not match:
        return None
    return {
        "recorded_at": match["recorded_at"],
        "actor": match["actor"].strip(),
        "role": match["role"].strip(),
        "body": (match["body"] or "").strip(),
    }


def split_summary(summary: str) -> tuple[str | None, str]:
    match = SUMMARY_SOURCE_ID.match(summary or "")
    if not match:
        return None, summary or ""
    return match["source_id"], summary[match.end():]


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


def labelled_value(text: str, name: str) -> str | None:
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", text, re.M)
    return match[1].strip() if match else None
