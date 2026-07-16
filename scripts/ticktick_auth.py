"""One-time TickTick OAuth flow. Writes data/ticktick.json for the poller.

Prerequisite: register an app at https://developer.ticktick.com/manage
with redirect URI exactly  http://127.0.0.1:8993/callback

Usage:
  .venv/bin/python scripts/ticktick_auth.py CLIENT_ID CLIENT_SECRET [LIST_NAME]

Then open the printed URL in a browser, approve, done. LIST_NAME defaults to
"Podcast" — URLs added to that TickTick list become podcast episodes.
"""
from __future__ import annotations

import http.server
import json
import sys
import threading
import urllib.parse
from pathlib import Path

import httpx

REDIRECT = "http://127.0.0.1:8993/callback"
DATA = Path(__file__).resolve().parent.parent / "data"

code_holder: dict = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = (query.get("code") or [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if code:  # ignore favicon/other hits that carry no code
            code_holder["code"] = code
            self.wfile.write(b"<h2>podcastfeeds: TickTick connected. You can close this tab.</h2>")
        else:
            self.wfile.write(b"<h2>Waiting for the authorization code...</h2>")

    def log_message(self, *args):
        pass


def main() -> None:
    import time

    if len(sys.argv) < 3:
        sys.exit(__doc__)
    client_id, client_secret = sys.argv[1], sys.argv[2]
    list_name = sys.argv[3] if len(sys.argv) > 3 else "Podcast"

    server = http.server.HTTPServer(("127.0.0.1", 8993), Handler)
    # serve_forever (not handle_request) so favicon/preflight hits don't consume
    # our single shot before the real /callback arrives.
    threading.Thread(target=server.serve_forever, daemon=True).start()

    params = urllib.parse.urlencode({
        "client_id": client_id, "scope": "tasks:read tasks:write",
        "response_type": "code", "redirect_uri": REDIRECT, "state": "podcastfeeds",
    })
    print("\nOpen this URL in your browser and approve access:\n")
    print(f"  https://ticktick.com/oauth/authorize?{params}\n")
    print("Waiting for the callback on 127.0.0.1:8993 ...")

    for _ in range(600):  # up to 5 minutes
        if "code" in code_holder:
            break
        time.sleep(0.5)
    server.shutdown()
    if "code" not in code_holder:
        sys.exit("Timed out waiting for TickTick authorization.")

    resp = httpx.post(
        "https://ticktick.com/oauth/token",
        auth=(client_id, client_secret),
        data={
            "code": code_holder["code"], "grant_type": "authorization_code",
            "scope": "tasks:read tasks:write", "redirect_uri": REDIRECT,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]

    DATA.mkdir(exist_ok=True)
    out = DATA / "ticktick.json"
    out.write_text(json.dumps({"access_token": token, "list": list_name}, indent=2))
    print(f"\nSuccess — token saved to {out}.")
    print(f"URLs added to your TickTick list '{list_name}' will now become episodes.")


if __name__ == "__main__":
    main()
