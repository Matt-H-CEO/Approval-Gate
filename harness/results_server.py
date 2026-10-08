"""Local results viewer: `python3 gate.py results`. Reads results/ on every request, so it always shows the newest run."""
import json
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from .runner import REPO

PAGE = Path(__file__).with_name("results.html")
RESULTS = REPO / "results"
DEFAULT_PORT = 8800


def listing():
    files = sorted((p for p in RESULTS.glob("*.json") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for path in files:
        try:
            data = json.loads(path.read_text())
            out.append({"name": path.name, "mode": data.get("mode", "?"), "team": data.get("team", "?"),
                        "persona": data.get("persona", "?"), "finished": (data.get("meta") or {}).get("finished", "")})
        except (OSError, ValueError, AttributeError):
            continue
    return out


def payload(selected=None):
    files = listing()
    names = [f["name"] for f in files]
    name = selected if selected in names else (names[0] if names else None)
    result = json.loads((RESULTS / name).read_text()) if name else None
    return {"files": files, "selected": name, "result": result}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route, _, query = self.path.partition("?")
        if route in ("/", "/results.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if route == "/api/results":
            selected = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("file")
            return self._send(200, json.dumps(payload(unquote(selected) if selected else None)).encode(), "application/json")
        self._send(404, b'{"error": "Unknown endpoint"}', "application/json")


def already_running(port):
    """True when a results viewer from an earlier command is already listening on this port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/results", timeout=1) as response:
            return "files" in json.load(response)
    except (OSError, ValueError):
        return False


def serve(port=DEFAULT_PORT, open_browser=True):
    if already_running(port):
        url = f"http://127.0.0.1:{port}/"
        print(f"Results viewer already running: {url}   (it shows the newest run; refresh is automatic)", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Results viewer: {url}   (shows the newest file in results/, refreshes by itself; Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
