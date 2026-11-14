"""Run the local image design review: python pages/status-preview/server.py."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1]))

from renderer import DESIGNS, prepare, render_status_report
from sample_data import SCENARIOS, make_demo


SAMPLES = make_demo()


@lru_cache(maxsize=64)
def png(design, scenario, page):
    image = render_status_report(SAMPLES[scenario], design, page=page)
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), image.size


class Handler(BaseHTTPRequestHandler):
    def send(self, body, content_type, status=HTTPStatus.OK, *, download=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        request = urlsplit(self.path)
        query = parse_qs(request.query)
        if request.path == "/api/designs":
            scenarios = []
            for key, name in SCENARIOS.items():
                data, _ = prepare(SAMPLES[key])
                scenarios.append({"id": key, "name": name, "pages": data["pages"]})
            payload = {"designs": DESIGNS, "scenarios": scenarios}
            self.send(json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if request.path == "/report.png":
            design = query.get("design", ["1"])[0]
            scenario = query.get("scenario", ["mixed"])[0]
            try:
                page = int(query.get("page", ["1"])[0])
            except ValueError:
                self.send(b"Invalid page", "text/plain", HTTPStatus.BAD_REQUEST)
                return
            if design not in {item["id"] for item in DESIGNS} or scenario not in SAMPLES:
                self.send(b"Unknown design or scenario", "text/plain", HTTPStatus.BAD_REQUEST)
                return
            data, _ = prepare(SAMPLES[scenario], page)
            try:
                body, _ = png(design, scenario, data["page"])
            except (OSError, RuntimeError) as exc:
                self.send(str(exc).encode(), "text/plain; charset=utf-8", HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            filename = f"status-v{design}-{scenario}-p{data['page']}.png" if "download" in query else None
            self.send(body, "image/png", download=filename)
            return
        if request.path == "/favicon.ico":
            self.send(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
            return
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8")}
        asset = assets.get(request.path)
        if asset is None:
            self.send('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>页面未找到</title><p>页面未找到，<a href="/">返回状态图预览</a></p>'.encode(),
                      "text/html; charset=utf-8", HTTPStatus.NOT_FOUND)
            return
        self.send((ROOT / asset[0]).read_bytes(), asset[1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError as exc:
            if exc.errno != 98:
                raise
    else:
        raise RuntimeError("No free preview port in requested range")
    print("Rendering five designs...", flush=True)
    # Warm the default previews before accepting browser requests.
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda design: png(design["id"], "mixed", 1), DESIGNS))
    print(f"Status image preview: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
