"""Local LLM visibility dashboard.

    python dashboard/server.py            # http://127.0.0.1:8765

Serves the single-page UI in dashboard/static and a small JSON API over the
scraper output CSVs listed in dashboard/config.json. Datasets rebuild
automatically when a CSV changes, so a fresh scrape shows up on reload.
"""
from __future__ import annotations

import argparse
import json
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import data

STATIC = data.ROOT / "static"
conn = data.text_db()
build_lock = threading.Lock()


def projects() -> list[dict]:
    return [p for p in data.load_config()["projects"] if (data.REPO / p["source"]).exists()]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            super().log_message(fmt, *args)

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        if parts[:1] != ["api"]:
            return super().do_GET()

        if parts == ["api", "projects"]:
            return self.send_json([
                {"id": p["id"], "name": p["name"], "source": p["source"]} for p in projects()
            ])

        if len(parts) == 3 and parts[1] == "project":
            project = next((p for p in projects() if p["id"] == parts[2]), None)
            if project is None:
                return self.send_json({"error": "unknown project"}, HTTPStatus.NOT_FOUND)
            with build_lock:
                return self.send_json(data.get_dataset(project, conn))

        if parts == ["api", "response"]:
            query = parse_qs(url.query)
            text = data.get_text(conn, query.get("project", [""])[0], query.get("id", [""])[0])
            if text is None:
                return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return self.send_json({"text": text})

        return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM visibility dashboard")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Dashboard running at http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
