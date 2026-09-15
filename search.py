"""Full-text search across every stored transcript.

Transcripts hold literal UTF-8, so the hot loop is a compiled byte regex run
over raw lines. Only the lines that actually match are decoded and parsed.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from index import blocks, clean_prompt, is_real_prompt, message_text, transcript_paths

SNIPPET_PAD = 90
MAX_MATCHES_PER_SESSION = 4


def _snippet(text: str, pattern: re.Pattern) -> str | None:
    found = pattern.search(text)
    if not found:
        return None
    start = max(0, found.start() - SNIPPET_PAD)
    end = min(len(text), found.end() + SNIPPET_PAD)
    body = text[start:end].replace("\n", " ").strip()
    return ("…" if start else "") + body + ("…" if end < len(text) else "")


def _entry_text(entry: dict) -> tuple[str, str] | None:
    """(role, text) for entries worth showing as a search hit."""
    kind = entry.get("type")
    message = entry.get("message") or {}

    if kind == "user":
        if not is_real_prompt(entry):
            return None
        return "user", clean_prompt(message_text(message))

    if kind == "assistant":
        parts = [message_text(message)]
        parts += [
            f"{b.get('name')} {json.dumps(b.get('input'), ensure_ascii=False)[:400]}"
            for b in blocks(message)
            if b.get("type") == "tool_use"
        ]
        return "assistant", "\n".join(p for p in parts if p)

    return None


def search(
    query: str,
    sessions: list[dict],
    limit: int = 40,
    project: str | None = None,
) -> dict:
    """Sessions containing the query, each with a few matching snippets."""
    query = (query or "").strip()
    if len(query) < 2:
        return {"query": query, "results": [], "elapsed": 0.0, "scanned": 0}

    began = time.perf_counter()
    raw = re.compile(re.escape(query).encode("utf-8"), re.IGNORECASE)
    text_pattern = re.compile(re.escape(query), re.IGNORECASE)

    # Archived sessions have no transcript left to search — only their summary.
    by_path = {s["path"]: s for s in sessions if not s.get("archived")}
    candidates = [
        Path(p) for p in by_path if not project or by_path[p].get("project") == project
    ]
    if not candidates:
        candidates = transcript_paths()

    results = []
    scanned = 0

    for path in candidates:
        scanned += 1
        matches = []
        try:
            handle = path.open("rb")
        except OSError:
            continue

        with handle:
            for line in handle:
                if not raw.search(line):
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pair = _entry_text(entry)
                if not pair:
                    continue
                role, text = pair
                snippet = _snippet(text, text_pattern)
                if not snippet:
                    continue
                matches.append(
                    {"ts": entry.get("timestamp"), "role": role, "snippet": snippet}
                )
                if len(matches) >= MAX_MATCHES_PER_SESSION:
                    break

        if not matches:
            continue

        summary = by_path.get(str(path), {})
        results.append(
            {
                "sessionId": summary.get("sessionId") or path.stem,
                "title": summary.get("title") or path.stem,
                "project": summary.get("project"),
                "branch": summary.get("branch"),
                "day": summary.get("day"),
                "start": summary.get("start"),
                "matches": matches,
            }
        )

    results.sort(key=lambda r: r.get("start") or "", reverse=True)
    return {
        "query": query,
        "results": results[:limit],
        "total": len(results),
        "scanned": scanned,
        "elapsed": round(time.perf_counter() - began, 3),
    }


if __name__ == "__main__":
    import sys

    import index

    term = " ".join(sys.argv[1:]) or "kafka"
    found = search(term, index.build()["sessions"])
    print(
        f"'{term}' → {found['total']} sessions "
        f"({found['scanned']} scanned, {found['elapsed']}s)\n"
    )
    for hit in found["results"][:6]:
        print(f"  {hit['day']}  {hit['title'][:56]}")
        for match in hit["matches"][:2]:
            print(f"      {match['role']:9} {match['snippet'][:110]}")
