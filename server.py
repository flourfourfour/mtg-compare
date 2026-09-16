#!/usr/bin/env python3
"""MTG Compare - a local web app for buying a decklist from UK shops.

Run it with:  python3 server.py
then open the address it prints. Nothing leaves your machine except the
searches it makes to the shops themselves.
"""

import argparse
import json
import os
import socket
import sys
import threading
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mtgcompare import catalogue, decklist, export, netcache, scryfall, search, stores  # noqa: E402

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class ThreadingHTTPServerV6(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    # Keep the console readable - one line per API call, no static-file noise.
    def log_message(self, fmt, *args):
        if self.path.startswith("/api/"):
            sys.stderr.write("  %s %s\n" % (self.command, self.path))

    def _send_file(self, text, content_type, filename):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._api_get()
        return super().do_GET()

    def do_POST(self):
        if not self.path.startswith("/api/"):
            return self.send_error(404)
        return self._api_post()

    def _api_get(self):
        path = self.path.split("?")[0]

        if path == "/api/stores":
            return self._send_json({"stores": stores.load(), "manual": stores.manual()})

        if path == "/api/index":
            return self._send_json({
                "shops": catalogue.status(stores.load()),
                "build": catalogue.build_state(),
            })

        if path.startswith("/api/export/"):
            name = path.rsplit("/", 1)[-1]
            job_id, _, extension = name.rpartition(".")
            job = search.get(job_id)
            if job is None:
                return self._send_json({"error": "that search has expired"}, 404)
            snapshot = job.snapshot()
            plan = snapshot.get("plan")
            stamp = __import__("datetime").date.today().isoformat()
            if extension == "csv":
                return self._send_file(export.to_csv(plan), "text/csv; charset=utf-8",
                                       "mtg-buy-plan-%s.csv" % stamp)
            return self._send_file(export.to_text(plan, search.cart_links(plan)),
                                   "text/plain; charset=utf-8", "mtg-buy-plan-%s.txt" % stamp)

        if path.startswith("/api/search/"):
            job = search.get(path.rsplit("/", 1)[-1])
            if job is None:
                return self._send_json({"error": "that search has expired"}, 404)
            snapshot = job.snapshot()
            snapshot["cart_links"] = search.cart_links(snapshot.get("plan"))
            return self._send_json(snapshot)

        return self._send_json({"error": "unknown endpoint"}, 404)

    def _api_post(self):
        path = self.path.split("?")[0]
        payload = self._read_json()

        if path == "/api/parse":
            entries, skipped = decklist.parse(payload.get("text", ""))
            if not entries:
                return self._send_json({"cards": [], "unknown": [], "skipped": skipped})
            resolved, unknown = scryfall.resolve([e["name"] for e in entries])
            cards, unresolved = [], list(unknown)
            for entry in entries:
                card = resolved.get(entry["name"].lower())
                if card is None:
                    continue
                cards.append({
                    "name": card["name"],
                    "qty": entry["qty"],
                    "image": card.get("image"),
                    "scryfall_uri": card.get("scryfall_uri"),
                    "typed_as": entry["name"],
                })
            return self._send_json({"cards": cards, "unknown": unresolved, "skipped": skipped})

        if path == "/api/search":
            cards = [
                {"name": c["name"], "qty": max(1, int(c.get("qty") or 1)), "image": c.get("image")}
                for c in payload.get("cards") or []
                if c.get("name")
            ]
            if not cards:
                return self._send_json({"error": "no cards to search for"}, 400)
            if payload.get("fresh"):
                netcache.clear_cache()
            job = search.start(cards, payload.get("options") or {})
            return self._send_json({"id": job.id, "total": len(cards)})

        if path == "/api/replan":
            job = search.replan(payload.get("id"), payload.get("options") or {})
            if job is None:
                return self._send_json({"error": "that search has expired"}, 404)
            snapshot = job.snapshot()
            snapshot["cart_links"] = search.cart_links(snapshot.get("plan"))
            return self._send_json(snapshot)

        if path == "/api/stores":
            return self._send_json({"stores": stores.update(payload.get("stores") or {})})

        if path == "/api/index/build":
            enabled = [s for s in stores.load() if s.get("enabled", True)]
            return self._send_json({"build": catalogue.build_all(enabled)})

        if path == "/api/index/stop":
            catalogue.stop_build()
            return self._send_json({"build": catalogue.build_state()})

        if path == "/api/cache/clear":
            return self._send_json({"removed": netcache.clear_cache()})

        return self._send_json({"error": "unknown endpoint"}, 404)


def _free_port(preferred):
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    port = _free_port(args.port)
    url = "http://127.0.0.1:%d/" % port
    server = ThreadingHTTPServerV6(("127.0.0.1", port), Handler)

    print("MTG Compare is running at %s" % url)
    print("Shops configured: %d   (change the list in data/stores.json or in the app)"
          % len([s for s in stores.load() if s.get("enabled", True)]))
    print("Press Ctrl-C to stop.\n")
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
