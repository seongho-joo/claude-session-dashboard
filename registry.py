"""Live Claude Code session registry.

Reads ~/.claude/sessions/{pid}.json, which Claude Code keeps current for every
running session, and cross-checks each pid with kill(pid, 0) so stale files
from crashed sessions are dropped.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude" / "sessions"


def _alive(pid: int) -> bool:
    """True if the process exists. PermissionError still means it exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def scan() -> list[dict]:
    """Every live session, newest activity first."""
    now_ms = time.time() * 1000
    sessions = []

    for path in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        pid = raw.get("pid")
        if not isinstance(pid, int) or not _alive(pid):
            continue

        cwd = raw.get("cwd") or ""
        updated = raw.get("updatedAt") or raw.get("statusUpdatedAt") or 0

        sessions.append(
            {
                "pid": pid,
                "sessionId": raw.get("sessionId"),
                "name": raw.get("name") or f"pid {pid}",
                "status": raw.get("status") or "unknown",
                "kind": raw.get("kind") or "interactive",
                "entrypoint": raw.get("entrypoint"),
                "cwd": cwd,
                "project": os.path.basename(cwd.rstrip("/")) or cwd,
                "version": raw.get("version"),
                "startedAt": raw.get("startedAt"),
                "updatedAt": updated,
                "statusUpdatedAt": raw.get("statusUpdatedAt"),
                "idleSeconds": int((now_ms - updated) / 1000) if updated else None,
                "uptimeSeconds": (
                    int((now_ms - raw["startedAt"]) / 1000)
                    if raw.get("startedAt")
                    else None
                ),
            }
        )

    busy_first = {"busy": 0, "idle": 1}
    sessions.sort(key=lambda s: (busy_first.get(s["status"], 2), -(s["updatedAt"] or 0)))
    return sessions


if __name__ == "__main__":
    live = scan()
    print(f"{len(live)} live sessions\n")
    for s in live:
        print(
            f"  {s['status']:5}  {s['name']:20}  {s['project']:12}  "
            f"v{s['version']}  pid {s['pid']}"
        )
