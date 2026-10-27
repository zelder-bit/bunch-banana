#!/usr/bin/env python3
"""MCP stdio -> HTTP bridge, for MCP clients that can only launch local commands.
Python 3.8+, no dependencies.

Client config example:
  {"command": "python3", "args": ["/path/to/mcp_stdio_bridge.py"],
   "env": {"BUNCH_URL": "http://bunch.lan:8700", "BUNCH_KEY": "bunch_..."}}
"""
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("BUNCH_URL", "http://127.0.0.1:8700").rstrip("/") + "/mcp"
KEY = os.environ.get("BUNCH_KEY", "")


def forward(line: str):
    req = urllib.request.Request(URL, data=line.encode(), method="POST", headers={
        "Authorization": "Bearer " + KEY,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.read().decode() if r.status != 202 else ""
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
    except urllib.error.URLError as e:
        detail = f"bunch unreachable: {e.reason}"
    try:
        mid = json.loads(line).get("id")
    except Exception:
        mid = None
    if mid is None:
        return ""
    return json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": detail}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        out = forward(line)
        if out:
            sys.stdout.write(out.strip() + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
