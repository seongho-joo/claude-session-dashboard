"""Transcript indexer for ~/.claude/projects.

Walks every session transcript, builds a compact per-session summary, and keeps
an incremental cache keyed on (size, mtime) so repeat scans cost milliseconds.

Layout it understands:
    projects/<slug>/<sessionId>.jsonl              a session
    projects/<slug>/<sessionId>/subagents/*.jsonl  its subagents
    projects/<slug>/<sessionId>/tool-results/*     offloaded tool output
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
# The index holds summaries of transcripts Claude Code has already deleted, so
# it is durable data, not a cache — it does not belong under ~/.cache.
STORE_PATH = Path.home() / ".local" / "share" / "cc-dashboard" / "index.json"
LEGACY_CACHE_PATH = Path.home() / ".cache" / "cc-dashboard" / "index.json"
CACHE_VERSION = 4

LOCAL_TZ = ZoneInfo("Asia/Seoul")

# A gap longer than this between two entries is the session sitting idle,
# not work — wall-clock duration counts overnight resumes, active time does not.
ACTIVE_GAP_SECONDS = 300

SMALL_CONTEXT = 200_000
LARGE_CONTEXT = 1_000_000

_TOKEN_KEYS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cacheRead", "cache_read_input_tokens"),
    ("cacheCreation", "cache_creation_input_tokens"),
)


# --------------------------------------------------------------------------
# jsonl helpers
# --------------------------------------------------------------------------


def iter_entries(path: Path):
    """Yield every parseable JSON object in a transcript, skipping junk."""
    try:
        handle = path.open(encoding="utf-8", errors="ignore")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def message_text(message: dict) -> str:
    """Readable text of a message. Thinking and tool payloads are left out."""
    parts = []
    for block in blocks(message):
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text") or "")
        elif kind == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                parts.extend(
                    b.get("text") or ""
                    for b in inner
                    if isinstance(b, dict) and b.get("type") == "text"
                )
    return "\n".join(p for p in parts if p).strip()


_WRAPPED_BLOCK = re.compile(
    r"<(system-reminder|local-command-caveat|local-command-stdout|command-message"
    r"|command-args|command-contents)>.*?</\1>",
    re.S,
)
_COMMAND_NAME = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
_ANY_TAG = re.compile(r"</?[a-z][a-z0-9-]*>")


def clean_prompt(text: str) -> str:
    """Strip the harness wrappers so a prompt reads the way the user typed it."""
    if not text:
        return ""
    command = _COMMAND_NAME.search(text)
    body = _COMMAND_NAME.sub("", _WRAPPED_BLOCK.sub("", text))
    body = _ANY_TAG.sub("", body).strip()
    if command:
        label = command.group(1).strip()
        return f"{label} {body}".strip() if body else label
    return body


def is_real_prompt(entry: dict) -> bool:
    """A prompt the user actually typed, not a tool result or injected meta."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    if entry.get("toolUseResult") is not None:
        return False
    return not any(b.get("type") == "tool_result" for b in blocks(entry.get("message", {})))


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def local_day(value) -> str | None:
    dt = parse_ts(value)
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d") if dt else None


def empty_tokens() -> dict:
    return {"input": 0, "output": 0, "cacheRead": 0, "cacheCreation": 0}


def add_usage(acc: dict, usage: dict) -> None:
    for key, source in _TOKEN_KEYS:
        acc[key] += usage.get(source) or 0


def response_id(entry: dict) -> str:
    """Identity of the API response an assistant entry belongs to.

    Claude Code writes one entry per content block — thinking, text, each
    tool_use — and repeats the full usage on every one of them. Everything
    that counts a response or its tokens must collapse on this id first.
    """
    message = entry.get("message") or {}
    return message.get("id") or entry.get("requestId") or entry.get("uuid") or ""


def context_size(usage: dict) -> int:
    """Everything the model had to read on this turn."""
    return (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
    )


# --------------------------------------------------------------------------
# summarizing
# --------------------------------------------------------------------------


def _subagent_dir(path: Path) -> Path:
    return path.parent / path.stem / "subagents"


def _scan_subagents(path: Path) -> dict:
    """Agent count, types and token cost for one session's subagents."""
    directory = _subagent_dir(path)
    if not directory.is_dir():
        return {"count": 0, "types": {}, "tokens": empty_tokens(), "models": {}}

    types: dict[str, int] = {}
    tokens = empty_tokens()
    models: dict[str, dict] = {}
    count = 0

    for meta_path in sorted(directory.glob("agent-*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        agent_type = meta.get("agentType") or "unknown"
        types[agent_type] = types.get(agent_type, 0) + 1
        count += 1

    for transcript in sorted(directory.glob("agent-*.jsonl")):
        counted: set[str] = set()
        for entry in iter_entries(transcript):
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message") or {}
            usage = message.get("usage")
            if not usage:
                continue
            key = response_id(entry)
            if key in counted:
                continue
            counted.add(key)
            add_usage(tokens, usage)
            model = message.get("model")
            if model:
                add_usage(models.setdefault(model, empty_tokens()), usage)

    return {"count": count, "types": types, "tokens": tokens, "models": models}


def summarize(path: Path) -> dict | None:
    """Compact summary of one session transcript."""
    session_id = path.stem
    tokens = empty_tokens()
    models: dict[str, dict] = {}
    tools: dict[str, int] = {}
    daily: dict[str, dict] = {}

    title = None
    name = None
    first_prompt = None
    last_prompt = None
    cwd = branch = version = entrypoint = None
    start = end = None
    user_msgs = assistant_msgs = tool_calls = thinking_turns = 0
    context_now = context_peak = 0
    counted_responses: set[str] = set()
    active_seconds = 0.0
    previous_at = None

    for entry in iter_entries(path):
        kind = entry.get("type")

        if kind == "ai-title":
            title = entry.get("aiTitle") or title
            continue
        if kind in ("custom-title", "agent-name"):
            # `/rename` writes both. Unlike the live registry, which disappears
            # with the process, this survives in the transcript.
            name = entry.get("customTitle") or entry.get("agentName") or name
            continue
        if kind == "last-prompt":
            last_prompt = clean_prompt(entry.get("lastPrompt") or "") or last_prompt
            continue

        timestamp = entry.get("timestamp")
        if timestamp:
            start = start or timestamp
            end = timestamp
            moment = parse_ts(timestamp)
            if moment and previous_at:
                gap = (moment - previous_at).total_seconds()
                if 0 < gap <= ACTIVE_GAP_SECONDS:
                    active_seconds += gap
            previous_at = moment or previous_at

        cwd = entry.get("cwd") or cwd
        branch = entry.get("gitBranch") or branch
        version = entry.get("version") or version
        entrypoint = entry.get("entrypoint") or entrypoint

        day = local_day(timestamp)
        if day and day not in daily:
            daily[day] = {"messages": 0, "tools": 0, "output": 0, "sessions": 0}

        if kind == "user" and is_real_prompt(entry):
            user_msgs += 1
            if day:
                daily[day]["messages"] += 1
            if first_prompt is None:
                text = clean_prompt(message_text(entry.get("message", {})))
                if text:
                    first_prompt = text[:500]

        elif kind == "assistant":
            message = entry.get("message") or {}
            model = message.get("model")
            usage = message.get("usage") or {}

            first_block = response_id(entry) not in counted_responses
            if first_block:
                counted_responses.add(response_id(entry))
                assistant_msgs += 1
                if day:
                    daily[day]["messages"] += 1

            if usage:
                if first_block:
                    add_usage(tokens, usage)
                    if model:
                        add_usage(models.setdefault(model, empty_tokens()), usage)
                    if day:
                        daily[day]["output"] += usage.get("output_tokens") or 0
                if not entry.get("isSidechain"):
                    size = context_size(usage)
                    if size:
                        context_now = size
                        context_peak = max(context_peak, size)

            for block in blocks(message):
                block_type = block.get("type")
                if block_type == "tool_use":
                    tool_name = block.get("name") or "unknown"
                    tools[tool_name] = tools.get(tool_name, 0) + 1
                    tool_calls += 1
                    if day:
                        daily[day]["tools"] += 1
                elif block_type == "thinking":
                    thinking_turns += 1

    if start is None:
        return None

    if day_keys := sorted(daily):
        daily[day_keys[0]]["sessions"] = 1

    subagents = _scan_subagents(path)
    for key in tokens:
        tokens[key] += subagents["tokens"][key]
    for model, counts in subagents["models"].items():
        bucket = models.setdefault(model, empty_tokens())
        for key in bucket:
            bucket[key] += counts[key]

    started = parse_ts(start)
    ended = parse_ts(end)
    duration = int((ended - started).total_seconds()) if started and ended else 0

    stat = path.stat()
    preview = (title or first_prompt or name or "").strip().replace("\n", " ")

    return {
        "sessionId": session_id,
        "path": str(path),
        "title": preview[:120] or "(제목 없음)",
        "name": name,
        "aiTitle": title,
        "firstPrompt": first_prompt,
        "lastPrompt": (last_prompt or "").strip()[:300] or None,
        "cwd": cwd or "",
        "project": os.path.basename((cwd or "").rstrip("/")) or path.parent.name,
        "branch": branch,
        "version": version,
        "entrypoint": entrypoint,
        "start": start,
        "end": end,
        "day": local_day(start),
        "durationSec": duration,
        "activeSec": int(active_seconds),
        "userMsgs": user_msgs,
        "assistantMsgs": assistant_msgs,
        "toolCalls": tool_calls,
        "thinkingTurns": thinking_turns,
        "tools": tools,
        "models": models,
        "tokens": tokens,
        "contextTokens": context_now,
        "contextPeak": context_peak,
        "contextLimit": LARGE_CONTEXT if context_peak > SMALL_CONTEXT else SMALL_CONTEXT,
        "subagents": subagents["count"],
        "subagentTypes": subagents["types"],
        "daily": daily,
        "sizeBytes": stat.st_size,
        "mtime": stat.st_mtime,
        "archived": False,
    }


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------


def projects_readable() -> bool:
    """False when ~/.claude/projects is missing or unreadable.

    The distinction matters: an empty scan of a readable directory means the
    transcripts really were cleaned up, while an empty scan of an unreadable one
    means we cannot tell — and must not archive the whole index on a bad mount.
    """
    try:
        return PROJECTS_DIR.is_dir() and os.access(PROJECTS_DIR, os.R_OK)
    except OSError:
        return False


def transcript_paths() -> list[Path]:
    """Top-level session transcripts only. Subagents roll up into their parent."""
    try:
        projects = [entry for entry in PROJECTS_DIR.iterdir() if entry.is_dir()]
    except OSError:
        return []
    return sorted(path for project in projects for path in project.glob("*.jsonl"))


def _load_store() -> dict:
    for source in (STORE_PATH, LEGACY_CACHE_PATH):
        try:
            stored = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        files = stored.get("files", {})
        if stored.get("version") == CACHE_VERSION:
            return files
        # Older layout. Live summaries are reparsed from their transcripts
        # anyway, but archived ones are the only copy left — carry them over.
        return {key: value for key, value in files.items() if value.get("archived")}
    return {}


def _save_store(files: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CACHE_VERSION, "savedAt": time.time(), "files": files}
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(STORE_PATH)


def build(force: bool = False) -> dict:
    """All session summaries, newest first. Reparses only changed transcripts.

    Claude Code deletes transcripts after `cleanupPeriodDays` (30 by default).
    Summaries of transcripts that have gone are kept and flagged `archived`, so
    the history keeps accumulating past that window even though the raw
    conversation is no longer readable.
    """
    stored = _load_store()
    fresh: dict[str, dict] = {}
    sessions = []
    reparsed = 0

    paths = transcript_paths()

    for path in paths:
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            continue

        hit = None if force else stored.get(key)
        if (
            hit
            and not hit.get("archived")
            and hit.get("mtime") == stat.st_mtime
            and hit.get("sizeBytes") == stat.st_size
        ):
            fresh[key] = hit
            sessions.append(hit)
            continue

        summary = summarize(path)
        reparsed += 1
        if summary:
            fresh[key] = summary
            sessions.append(summary)

    # A transcript in the store with no file behind it has been cleaned up.
    vanished = [key for key in stored if key not in fresh]

    # Unless ~/.claude/projects itself is gone — a moved home directory or an
    # unmounted volume must not flip the entire index to archived.
    unreadable = not projects_readable()

    archived = 0
    for key in vanished:
        summary = dict(stored[key])
        if not unreadable and not summary.get("archived"):
            summary["archived"] = True
            summary["archivedAt"] = time.time()
            archived += 1
        fresh[key] = summary
        sessions.append(summary)

    _save_store(fresh)
    sessions.sort(key=lambda s: s.get("start") or "", reverse=True)
    return {
        "sessions": sessions,
        "reparsed": reparsed,
        "total": len(sessions),
        "live": len(sessions) - sum(1 for s in sessions if s.get("archived")),
        "archived": sum(1 for s in sessions if s.get("archived")),
        "newlyArchived": archived,
    }


# --------------------------------------------------------------------------
# detail + stats
# --------------------------------------------------------------------------


def find_path(session_id: str) -> Path | None:
    for path in transcript_paths():
        if path.stem == session_id:
            return path
    return None


def archived_summary(session_id: str) -> dict | None:
    """Stored summary of a session whose transcript has been cleaned up."""
    for summary in _load_store().values():
        if summary.get("sessionId") == session_id:
            return summary
    return None


def detail(session_id: str, max_entries: int = 1500) -> dict | None:
    """Full timeline for one session, read straight from its own file."""
    path = find_path(session_id)
    if path is None:
        summary = archived_summary(session_id)
        if summary is None:
            return None
        # The transcript is gone — the summary is all that survives.
        return {
            "summary": summary,
            "timeline": [],
            "contextSeries": [],
            "truncated": False,
            "expired": True,
        }

    summary = summarize(path)
    timeline = []
    context_series = []
    truncated = False

    for entry in iter_entries(path):
        kind = entry.get("type")
        if kind not in ("user", "assistant"):
            continue
        if len(timeline) >= max_entries:
            truncated = True
            break

        message = entry.get("message") or {}
        timestamp = entry.get("timestamp")

        if kind == "user":
            if not is_real_prompt(entry):
                continue
            text = clean_prompt(message_text(message))
            if not text:
                continue
            timeline.append(
                {
                    "role": "user",
                    "ts": timestamp,
                    "text": text[:6000],
                    "clipped": len(text) > 6000,
                }
            )
            continue

        usage = message.get("usage") or {}
        tools = [
            {"name": b.get("name"), "input": _tool_hint(b.get("input"))}
            for b in blocks(message)
            if b.get("type") == "tool_use"
        ]
        text = message_text(message)
        thinking = any(b.get("type") == "thinking" for b in blocks(message))

        # Blocks of one API response arrive as separate entries — fold them
        # back into a single turn so the timeline reads like the conversation.
        rid = response_id(entry)
        previous = timeline[-1] if timeline else None
        merging = (
            previous is not None
            and previous.get("role") == "assistant"
            and previous.get("responseId") == rid
        )

        if merging:
            if text:
                joined = f"{previous['text']}\n{text}" if previous["text"] else text
                previous["clipped"] = previous["clipped"] or len(joined) > 6000
                previous["text"] = joined[:6000]
            previous["tools"].extend(tools)
            previous["thinking"] = previous["thinking"] or thinking
            continue

        if usage and not entry.get("isSidechain"):
            size = context_size(usage)
            if size:
                context_series.append({"ts": timestamp, "tokens": size})

        if not text and not tools and not thinking:
            continue

        timeline.append(
            {
                "role": "assistant",
                "responseId": rid,
                "ts": timestamp,
                "text": text[:6000],
                "clipped": len(text) > 6000,
                "model": message.get("model"),
                "thinking": thinking,
                "tools": tools,
                "outputTokens": usage.get("output_tokens") or 0,
            }
        )

    return {
        "summary": summary,
        "timeline": timeline,
        "contextSeries": context_series,
        "truncated": truncated,
        "expired": False,
    }


def _tool_hint(payload) -> str:
    """One short line describing a tool call, for the timeline."""
    if not isinstance(payload, dict):
        return ""
    for key in ("command", "file_path", "pattern", "query", "prompt", "description", "skill", "url"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\n", " ")[:160]
    return ""


def stats(sessions: list[dict], days: int = 30) -> dict:
    """Daily, project, branch and model rollups across every session."""
    daily: dict[str, dict] = {}
    projects: dict[str, dict] = {}
    branches: dict[str, dict] = {}
    models: dict[str, dict] = {}
    tools: dict[str, int] = {}

    for session in sessions:
        for day, counts in (session.get("daily") or {}).items():
            bucket = daily.setdefault(
                day, {"date": day, "messages": 0, "tools": 0, "output": 0, "sessions": 0}
            )
            bucket["messages"] += counts.get("messages", 0)
            bucket["tools"] += counts.get("tools", 0)
            bucket["output"] += counts.get("output", 0)
            bucket["sessions"] += counts.get("sessions", 0)

        project = session.get("project") or "(unknown)"
        entry = projects.setdefault(
            project, {"name": project, "sessions": 0, "messages": 0, "output": 0, "seconds": 0}
        )
        entry["sessions"] += 1
        entry["messages"] += session.get("userMsgs", 0) + session.get("assistantMsgs", 0)
        entry["output"] += session.get("tokens", {}).get("output", 0)
        entry["seconds"] += session.get("activeSec", 0)

        branch = session.get("branch") or "(no branch)"
        entry = branches.setdefault(branch, {"name": branch, "sessions": 0, "seconds": 0})
        entry["sessions"] += 1
        entry["seconds"] += session.get("activeSec", 0)

        for model, counts in (session.get("models") or {}).items():
            if model == "<synthetic>":
                continue
            bucket = models.setdefault(model, {"name": model, **empty_tokens()})
            for key in empty_tokens():
                bucket[key] += counts.get(key, 0)

        for name, count in (session.get("tools") or {}).items():
            tools[name] = tools.get(name, 0) + count

    ordered_days = sorted(daily.values(), key=lambda d: d["date"])[-days:]

    return {
        "daily": ordered_days,
        "projects": sorted(projects.values(), key=lambda p: -p["sessions"]),
        "branches": sorted(branches.values(), key=lambda b: -b["seconds"])[:20],
        "models": sorted(models.values(), key=lambda m: -m["output"]),
        "tools": sorted(
            ({"name": n, "count": c} for n, c in tools.items()), key=lambda t: -t["count"]
        )[:25],
        "totals": {
            "sessions": len(sessions),
            "messages": sum(
                s.get("userMsgs", 0) + s.get("assistantMsgs", 0) for s in sessions
            ),
            "toolCalls": sum(s.get("toolCalls", 0) for s in sessions),
            "subagents": sum(s.get("subagents", 0) for s in sessions),
            "output": sum(s.get("tokens", {}).get("output", 0) for s in sessions),
            "bytes": sum(
                s.get("sizeBytes", 0) for s in sessions if not s.get("archived")
            ),
            "archived": sum(1 for s in sessions if s.get("archived")),
        },
    }


if __name__ == "__main__":
    import sys

    force = "--force" in sys.argv
    began = time.perf_counter()
    result = build(force=force)
    elapsed = time.perf_counter() - began

    rollup = stats(result["sessions"])
    totals = rollup["totals"]

    print(
        f"{result['total']} sessions — {result['live']} live, "
        f"{result['archived']} archived  ({result['reparsed']} reparsed)  {elapsed:.2f}s"
    )
    if result["newlyArchived"]:
        print(f"  {result['newlyArchived']} transcript(s) cleaned up by Claude Code, summaries kept")
    print(
        f"  messages {totals['messages']:,}   tool calls {totals['toolCalls']:,}   "
        f"subagents {totals['subagents']:,}   {totals['bytes'] / 1e6:.0f}MB"
    )
    print("\n  model output tokens")
    for model in rollup["models"]:
        print(f"    {model['name']:22} {model['output']:>14,}")
    print("\n  recent sessions")
    for session in result["sessions"][:5]:
        print(
            f"    {session['day']}  {session['project']:10}  "
            f"ctx {session['contextTokens']:>7,}  {session['title'][:52]}"
        )
