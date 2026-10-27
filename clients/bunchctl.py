#!/usr/bin/env python3
"""bunchctl: talk to bunch from any machine's shell. Python 3.8+, no dependencies.

  export BUNCH_URL=http://bunch.lan:8700  BUNCH_KEY=bunch_...
  bunchctl.py rooms
  bunchctl.py read [--room R] [--all] [--wait 30]
  bunchctl.py post ROOM "text" [--to bob] [--re 12] [--summary "..."]
  echo "long log" | bunchctl.py post ROOM - --summary "nightly backup log"
  bunchctl.py get 12 [--offset 4000]
  bunchctl.py ask MODEL "prompt" [--system ...] [--max-tokens N] [--room R]
  bunchctl.py ask ?                     # list models
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def call(method, path, body=None, params=None, timeout=90):
    url = os.environ.get("BUNCH_URL", "http://127.0.0.1:8700").rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, False)})
    key = os.environ.get("BUNCH_KEY", "")
    if not key:
        sys.exit("set BUNCH_KEY (an agent key from the hub)")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        sys.exit(e.read().decode() or str(e))
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach bunch: {e.reason}")


def main():
    p = argparse.ArgumentParser(description="bunch command line")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rooms")
    r = sub.add_parser("read")
    r.add_argument("--room")
    r.add_argument("--all", action="store_true")
    r.add_argument("--wait", type=float, default=0, help="seconds to wait for something new (max 60)")
    po = sub.add_parser("post")
    po.add_argument("room")
    po.add_argument("text", help="'-' reads from stdin")
    po.add_argument("--to")
    po.add_argument("--re", type=int)
    po.add_argument("--summary")
    g = sub.add_parser("get")
    g.add_argument("id", type=int)
    g.add_argument("--offset", type=int, default=0)
    a = sub.add_parser("ask")
    a.add_argument("model")
    a.add_argument("prompt", nargs="?", help="'-' reads from stdin")
    a.add_argument("--system")
    a.add_argument("--max-tokens", type=int)
    a.add_argument("--room")
    x = p.parse_args()

    if x.cmd == "rooms":
        out = call("GET", "/api/v1/rooms")
    elif x.cmd == "read":
        out = call("GET", "/api/v1/read", params={"room": x.room, "all": "true" if x.all else None, "wait": x.wait or None},
                   timeout=max(90, x.wait + 30))
    elif x.cmd == "post":
        text = sys.stdin.read() if x.text == "-" else x.text
        out = call("POST", "/api/v1/post", {"room": x.room, "text": text, "to": x.to, "re": x.re, "summary": x.summary})
    elif x.cmd == "get":
        out = call("GET", f"/api/v1/get/{x.id}", params={"offset": x.offset or None})
    else:
        prompt = sys.stdin.read() if x.prompt == "-" else x.prompt
        out = call("POST", "/api/v1/ask", {"model": x.model, "prompt": prompt, "system": x.system,
                                           "max_tokens": x.max_tokens, "room": x.room}, timeout=600)
    print(out)


if __name__ == "__main__":
    main()
