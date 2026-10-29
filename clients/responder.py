#!/usr/bin/env python3
"""responder: turns any model into a chat participant. Python 3.8+, no dependencies.

It waits for messages addressed to its agent name (@name, or replies to it),
answers humans always and other agents only when they @mention it (loop guard),
asks a model for an answer through the hub, and replies in the same room.
Each answer sees only: the room summary, one-line summaries of the latest
messages, the message itself, and the message it replies to. No growing
history, so token use stays flat.

  export BUNCH_URL=http://bunch.lan:8700  BUNCH_KEY=bunch_...  RESPONDER_MODEL=local/coder
  export RESPONDER_SYSTEM="You are a careful senior engineer. Be brief."   # optional
  python3 responder.py

Run one per persona/model you want in the chat (each with its own agent key).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HUB = os.environ.get("BUNCH_URL", "http://127.0.0.1:8700").rstrip("/")
KEY = os.environ.get("BUNCH_KEY", "")
MENTION_RE = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9_.-]{0,31})")  # same as the hub's
MODEL = os.environ.get("RESPONDER_MODEL", "")
SYSTEM = os.environ.get("RESPONDER_SYSTEM",
                        "You are an AI agent in a shared chat room with other agents and a human. "
                        "Answer the message addressed to you. Be brief and concrete. "
                        "If you need another agent, @mention them.")
MAX_TOKENS = int(os.environ.get("RESPONDER_MAX_TOKENS", "800"))
MAX_INPUT_CHARS = int(os.environ.get("RESPONDER_MAX_INPUT_CHARS", "12000"))
# "mention" (default): reply to agents only when they write @<my name>. "always": reply to every addressed msg.
REPLY_TO_AGENTS = os.environ.get("RESPONDER_REPLY_TO_AGENTS", "mention")


def req(method, path, body=None, params=None, timeout=120):
    url = HUB + path + ("?" + urllib.parse.urlencode(params) if params else "")
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read().decode()
        is_json = resp.headers.get("Content-Type", "").startswith("application/json")
    return json.loads(raw) if is_json else raw


def full_text(mid):
    text = req("GET", f"/api/v1/get/{mid}")
    return text.split("\n", 1)[1] if "\n" in text else text  # drop the header line


def room_summary(room):
    for line in req("GET", "/api/v1/rooms").splitlines():
        if line.startswith(room + " ("):
            return line.split("): ", 1)[-1]
    return ""


def answer(m, me):
    body = full_text(m["id"]) if m["size"] else m["summary"]
    # Loop guard: answer humans always, other agents only when they explicitly ask us ("@me, can you…").
    asked = m["wake"] if "wake" in m else me in (n.rstrip(".") for n in MENTION_RE.findall(body.lower()))  # older hubs
    if not m["human"] and REPLY_TO_AGENTS != "always" and not asked:
        print(f"  skipped #{m['id']} (agent message that doesn't ask @{me})", file=sys.stderr)
        return
    parts = [m["note"]] if m.get("note") else []  # bot mode (older hubs don't send it)
    parts.append(f"Room #{m['room']} summary: {room_summary(m['room']) or '(none)'}")
    if m.get("recent"):  # older hubs don't send it
        parts.append(f"Recent messages:\n{m['recent']}")
    if m.get("reply_to"):
        parts.append(f"It replies to #{m['reply_to']}:\n{full_text(m['reply_to'])[:MAX_INPUT_CHARS // 3]}")
    parts.append(f"Message #{m['id']} from {m['sender']}:\n{body[:MAX_INPUT_CHARS]}")
    text = req("POST", "/api/v1/ask", {"model": MODEL, "system": SYSTEM, "prompt": "\n\n".join(parts),
                                       "max_tokens": MAX_TOKENS}, timeout=600)
    if text.startswith("error:"):
        print(text, file=sys.stderr)
        return
    req("POST", "/api/v1/post", {"room": m["room"], "text": text, "re": m["id"]})


def main():
    if not KEY or not MODEL:
        sys.exit("set BUNCH_KEY and RESPONDER_MODEL")
    me = req("GET", "/api/v1/whoami")
    print(f"responder up as {me}: model={MODEL} bunch={HUB}", file=sys.stderr)
    while True:
        try:
            batch = req("GET", "/api/v1/read", params={"format": "json", "wait": 50}, timeout=90)
            for m in batch["messages"]:
                if m["for_me"]:
                    print(f"#{m['id']} from {m['sender']} in {m['room']}", file=sys.stderr)
                    try:
                        answer(m, me)
                    except urllib.error.HTTPError as e:
                        print(f"reply failed: {e.read().decode()[:200]}", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f"bunch unreachable ({e}); retrying in 10s", file=sys.stderr)
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
