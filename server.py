"""Local dashboard server for Claude Code sessions.

Standard library only. Binds to loopback and stays there: transcripts contain
credentials and production query output, so this must never be reachable off
the machine.

    python3 server.py [--port 8787] [--open]
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import index
import registry
import search as search_module

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
STATIC_DIR = Path(__file__).parent / "static"
INDEX_TTL_SECONDS = 2.0
SSE_POLL_SECONDS = 2.0
SSE_HEARTBEAT_SECONDS = 20.0

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class IndexCache:
    """Shares one transcript index across requests, refreshed on a short TTL."""

    def __init__(self, ttl: float = INDEX_TTL_SECONDS):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._value: dict | None = None
        self._at = 0.0

    def get(self, force: bool = False) -> dict:
        with self._lock:
            if force or self._value is None or time.time() - self._at > self._ttl:
                self._value = index.build()
                self._at = time.time()
            return self._value


CACHE = IndexCache()


def live_fingerprint(sessions: list[dict]) -> str:
    return "|".join(
        f"{s['sessionId']}:{s['status']}:{s['updatedAt']}" for s in sessions
    )


def filter_sessions(sessions: list[dict], params: dict) -> list[dict]:
    project = params.get("project", [""])[0]
    branch = params.get("branch", [""])[0]
    term = params.get("q", [""])[0].strip().lower()
    days = params.get("days", [""])[0]
    hide_empty = params.get("nonempty", [""])[0] == "1"
    archived = params.get("archived", [""])[0]

    out = sessions
    if archived == "only":
        out = [s for s in out if s.get("archived")]
    elif archived == "0":
        out = [s for s in out if not s.get("archived")]
    if project:
        out = [s for s in out if s.get("project") == project]
    if branch:
        out = [s for s in out if s.get("branch") == branch]
    if hide_empty:
        out = [s for s in out if s.get("assistantMsgs", 0) > 0]
    if term:
        out = [
            s
            for s in out
            if term in (s.get("title") or "").lower()
            or term in (s.get("branch") or "").lower()
            or term in (s.get("firstPrompt") or "").lower()
        ]
    if days.isdigit():
        cutoff = time.time() - int(days) * 86400
        out = [
            s
            for s in out
            if (ts := index.parse_ts(s.get("start"))) and ts.timestamp() >= cutoff
        ]
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cc-dashboard"

    # ---------------- plumbing ----------------

    def log_message(self, fmt, *args):  # noqa: A003 - base class hook
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _static(self, name: str) -> None:
        target = (STATIC_DIR / name).resolve()
        if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
            self._json({"error": "not found"}, 404)
            return
        content_type = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), content_type)

    # ---------------- routes ----------------

    def do_GET(self) -> None:  # noqa: N802 - base class hook
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        params = parse_qs(parsed.query)

        try:
            if route == "/" or route == "/index.html":
                self._static("index.html")
            elif route.startswith("/static/"):
                self._static(route[len("/static/") :])
            elif route == "/events":
                self._events()
            elif route.startswith("/api/"):
                self._api(route, params)
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:  # surface failures in the UI, don't kill the thread
            try:
                self._json({"error": f"{type(error).__name__}: {error}"}, 500)
            except OSError:
                pass

    def _api(self, route: str, params: dict) -> None:
        if route == "/api/live":
            live = registry.scan()
            by_id = {s["sessionId"]: s for s in CACHE.get()["sessions"]}
            for session in live:
                summary = by_id.get(session["sessionId"])
                if summary:
                    session["context"] = {
                        "tokens": summary["contextTokens"],
                        "limit": summary["contextLimit"],
                        "peak": summary["contextPeak"],
                    }
                    session["title"] = summary["title"]
                    session["branch"] = summary["branch"]
                    session["lastPrompt"] = summary["lastPrompt"]
                    session["toolCalls"] = summary["toolCalls"]
                    session["output"] = summary["tokens"]["output"]
            self._json({"sessions": live, "at": time.time()})
            return

        if route == "/api/sessions":
            data = CACHE.get()
            rows = filter_sessions(data["sessions"], params)
            limit = int(params.get("limit", ["200"])[0])
            self._json(
                {
                    "sessions": rows[:limit],
                    "total": len(rows),
                    "indexed": data["total"],
                    "archived": data["archived"],
                    "live": data["live"],
                    "projects": sorted({s["project"] for s in data["sessions"] if s["project"]}),
                }
            )
            return

        if route.startswith("/api/sessions/"):
            rest = route[len("/api/sessions/") :]
            session_id = rest.split("/")[0]
            detail = index.detail(session_id)
            if detail is None:
                self._json({"error": "session not found"}, 404)
                return
            if rest.endswith("/context"):
                self._json(
                    {
                        "series": detail["contextSeries"],
                        "limit": detail["summary"]["contextLimit"],
                    }
                )
                return
            self._json(detail)
            return

        if route == "/api/search":
            data = CACHE.get()
            self._json(
                search_module.search(
                    params.get("q", [""])[0],
                    data["sessions"],
                    project=params.get("project", [""])[0] or None,
                )
            )
            return

        if route == "/api/stats":
            data = CACHE.get()
            days = int(params.get("days", ["30"])[0])
            self._json(index.stats(data["sessions"], days=days))
            return

        if route == "/api/refresh":
            data = CACHE.get(force=True)
            self._json(
                {
                    "indexed": data["total"],
                    "reparsed": data["reparsed"],
                    "archived": data["archived"],
                    "newlyArchived": data["newlyArchived"],
                }
            )
            return

        self._json({"error": "not found"}, 404)

    def _events(self) -> None:
        """Server-sent events: push the live roster whenever it changes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        previous = None
        last_beat = 0.0

        while True:
            sessions = registry.scan()
            fingerprint = live_fingerprint(sessions)
            now = time.time()

            if fingerprint != previous:
                previous = fingerprint
                payload = json.dumps(
                    {"sessions": sessions, "at": now}, ensure_ascii=False
                )
                self.wfile.write(f"event: live\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                last_beat = now
            elif now - last_beat > SSE_HEARTBEAT_SECONDS:
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                last_beat = now

            time.sleep(SSE_POLL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code session dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="open a browser tab")
    args = parser.parse_args()

    began = time.perf_counter()
    data = CACHE.get(force=True)
    print(
        f"indexed {data['total']} sessions in {time.perf_counter() - began:.2f}s "
        f"({data['live']} live, {data['archived']} archived)"
    )
    if data["newlyArchived"]:
        print(
            f"  {data['newlyArchived']} transcript(s) cleaned up by Claude Code — "
            "summaries kept"
        )

    url = f"http://{HOST}:{args.port}"
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    server.daemon_threads = True
    print(f"dashboard → {url}   (ctrl-c to stop)")

    if args.open:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
