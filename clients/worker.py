#!/usr/bin/env python3
"""worker: answers bunch messages with a subscription CLI (Claude Code, Codex, Gemini CLI).
Python 3.8+, no dependencies.

Run it on the machine that is logged in to the CLI. It waits for messages addressed to its
agent (@name, or replies to it) and runs the CLI headless on each one, so you can ping that
machine's subscription from anywhere. How it runs (CLI, model, powers, folder) is set in the
web viewer's agent panel; this machine only sets the ceiling of what it will allow.

  export BUNCH_URL=http://bunch.lan:8700  BUNCH_KEY=bunch_...
  export WORKER_MAX_POWER=answer            # answer | read | full: the most this host allows
  export WORKER_DIRS=~/code/app:~/notes     # folders "read"/"full" may use
  python3 worker.py

Token thrift: every answer is a fresh CLI run that sees only the room summary, the message and
the message it replies to. In "answer" mode Claude Code also drops its tools, MCP servers and
default system prompt. Jobs run one at a time. When the CLI says the plan's limit is hit, the
worker cools down until the reset and the hub's router stops sending it work.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HUB = os.environ.get("BUNCH_URL", "http://127.0.0.1:8700").rstrip("/")
KEY = os.environ.get("BUNCH_KEY", "")
POWERS = ("answer", "read", "full")
MAX_POWER = os.environ.get("WORKER_MAX_POWER", "answer").strip().lower()
DIRS = [os.path.realpath(os.path.expanduser(d)) for d in os.environ.get("WORKER_DIRS", "").split(os.pathsep) if d]
TIMEOUT = int(os.environ.get("WORKER_TIMEOUT", "600"))
COOLDOWN = int(os.environ.get("WORKER_COOLDOWN", "1800"))  # when the CLI doesn't say when the limit resets
STALE_AFTER = 300  # ignore backlog older than this when the worker starts
DEFAULT_SYSTEM = ("You are {name}, an AI agent in a shared chat room with other agents and humans. "
                  "Answer the message addressed to you. Be brief and concrete. "
                  "If you need another agent, @mention them.")
LIMIT_RE = re.compile(r"usage limit|rate.?limit|hit your limit|limit reached|quota|resource.?exhausted|"
                      r"too many requests|\b429\b|out of (?:extra )?usage", re.I)


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


def report(status, tokens=0, calls=0, until=None, room=None):
    try:
        return req("POST", "/api/v1/worker/report",
                   {"status": status, "tokens": tokens, "calls": calls, "until": until, "room": room})
    except (urllib.error.URLError, OSError) as e:
        print(f"report failed: {e}", file=sys.stderr)
        return ""


# ------------------------------------------------------------------ pure parts (tested)
def effective_power(cfg, max_power=MAX_POWER, dirs=DIRS):
    """The viewer's power, clamped to this host's ceiling and allowed folders. Returns (power, dir)."""
    want = cfg.get("power") or "answer"
    power = want if POWERS.index(want) <= POWERS.index(max_power if max_power in POWERS else "answer") else max_power
    if power == "answer":
        return "answer", ""
    d = os.path.realpath(os.path.expanduser(cfg.get("dir") or ""))
    if not any(d == a or d.startswith(a + os.sep) for a in dirs):
        return "answer", ""  # folder not allowed on this host
    return power, d


def build_cmd(cfg, system, power, workdir, deep=False):
    """argv for one headless run. The prompt goes on stdin. Returns (argv, cwd)."""
    cli = cfg["cli"]
    model = (cfg.get("deep_model") if deep else "") or cfg.get("model") or ""
    if cli == "claude":
        argv = ["claude", "-p", "--output-format", "json", "--no-session-persistence", "--strict-mcp-config"]
        argv += ["--model", model] if model else []
        if power == "answer":  # no tools, no settings, short system prompt: ~1k tokens of overhead
            argv += ["--tools", "", "--setting-sources", "", "--system-prompt", system]
        elif power == "read":
            argv += ["--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob",
                     "--append-system-prompt", system]
        else:
            argv += ["--permission-mode", "acceptEdits", "--allowedTools", "Read,Grep,Glob,Edit,Write,Bash",
                     "--append-system-prompt", system]
        return argv, workdir
    if cli == "codex":
        argv = ["codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--ignore-rules"]
        argv += ["-m", model] if model else []
        argv += ["-s", "workspace-write" if power == "full" else "read-only", "-C", workdir, "-"]
        return argv, workdir
    if cli == "gemini":
        argv = ["gemini", "--output-format", "json"]
        argv += ["-m", model] if model else []
        argv += ["--approval-mode", "auto_edit" if power == "full" else "default",
                 "-p", "Answer the message above."]
        return argv, workdir
    raise ValueError(f"unknown cli {cli}")


def stdin_for(cfg, system, prompt):
    # Claude Code takes the persona as a flag; the others get it in front of the prompt.
    return prompt if cfg["cli"] == "claude" else f"{system}\n\n{prompt}"


def parse_output(cli, out, err, rc):
    """Returns (text, tokens, error). error is None on success."""
    text, tokens, error = "", 0, None
    try:
        if cli == "claude":
            d = json.loads(out)
            u = d.get("usage") or {}
            tokens = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens", "output_tokens"))
            text = d.get("result") or ""
            if d.get("is_error"):
                error = text or d.get("subtype") or "error"
        elif cli == "codex":
            for line in out.splitlines():
                if not line.startswith("{"):
                    continue
                e = json.loads(line)
                item = e.get("item") or {}
                if e.get("type") == "item.completed" and item.get("type") == "agent_message":
                    text = item.get("text") or text
                elif e.get("type") == "turn.completed":
                    u = e.get("usage") or {}
                    tokens += int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
                elif e.get("type") in ("error", "turn.failed"):
                    error = e.get("message") or (e.get("error") or {}).get("message") or "error"
        elif cli == "gemini":
            d = json.loads(out)
            text = d.get("response") or ""
            for m in ((d.get("stats") or {}).get("models") or {}).values():
                tokens += int(((m or {}).get("tokens") or {}).get("total") or 0)
            if d.get("error"):
                error = (d["error"] or {}).get("message") or "error"
    except (ValueError, TypeError, AttributeError):
        pass
    if not text and not error:
        error = (err or out or f"exit {rc}").strip()[-300:] if rc else "(empty answer)"
    return text.strip(), tokens, error


def reset_time(msg, now=None):
    """When a usage limit lifts, from the CLI's message. Falls back to now + COOLDOWN."""
    now = now or time.time()
    m = re.search(r"\|(\d{10})\b", msg)  # Claude: "...limit reached|1767225600"
    if m:
        return float(m.group(1))
    m = re.search(r"\bin\s+(?:(\d+)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?", msg, re.I)
    if m and (m.group(1) or m.group(2)):
        return now + int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)?", msg, re.I)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3):
            h = h % 12 + (12 if m.group(3).lower() == "pm" else 0)
        t = time.localtime(now)
        at = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, mi, 0, 0, 0, -1))
        return at if at > now else at + 86400
    return now + COOLDOWN


# ------------------------------------------------------------------ the loop
class Worker:
    def __init__(self, me):
        self.me = me
        self.cfg = {"mode": "manual"}
        self.fetched = 0.0
        self.cool_until = 0.0
        self.over_budget_day = ""
        self.scratch = tempfile.mkdtemp(prefix="bunch-worker-")  # empty cwd for "answer" runs
        self.started = time.time()

    def refresh(self, force=False):
        if force or time.time() - self.fetched > 60:
            cfg = req("GET", "/api/v1/worker")
            if cfg != self.cfg:
                print(f"config: {json.dumps(cfg)}", file=sys.stderr)
            self.cfg, self.fetched = cfg, time.time()

    def reply(self, m, text):
        req("POST", "/api/v1/post", {"room": m["room"], "text": text, "re": m["id"]})

    def handle(self, m):
        cfg, me = self.cfg, self.me
        body = full_text(m["id"]) if m["size"] else m["summary"]
        # Loop guard: answer humans always, other agents only when they explicitly @mention us.
        if not m["human"] and f"@{me}" not in body.lower():
            return
        if m["created"] < self.started - STALE_AFTER:
            return
        if time.time() < self.cool_until:
            return self.reply(m, f"(out of {cfg['cli']} quota until {time.strftime('%H:%M', time.localtime(self.cool_until))})")
        if self.over_budget_day == time.strftime("%Y-%m-%d", time.gmtime()):
            return self.reply(m, "(daily token budget used; resets 00:00 UTC)")
        deep = "!deep" in body
        limit = int(cfg.get("max_input_chars") or 12000)
        parts = [f"Room #{m['room']} summary: {room_summary(m['room']) or '(none)'}"]
        if m.get("reply_to"):
            parts.append(f"It replies to #{m['reply_to']}:\n{full_text(m['reply_to'])[:limit // 3]}")
        parts.append(f"Message #{m['id']} from {m['sender']}:\n{body.replace('!deep', '').strip()[:limit]}")
        system = (cfg.get("system") or DEFAULT_SYSTEM.format(name=me)) + \
            f" Keep the answer under about {int(cfg.get('max_output') or 800)} tokens."
        power, d = effective_power(cfg)
        if power != (cfg.get("power") or "answer"):
            print(f"  power {cfg.get('power')} not allowed here (WORKER_MAX_POWER/WORKER_DIRS); using {power}",
                  file=sys.stderr)
        argv, cwd = build_cmd(cfg, system, power, d or self.scratch, deep)
        report("busy", room=m["room"])
        try:
            p = subprocess.run(argv, input=stdin_for(cfg, system, "\n\n".join(parts)), cwd=cwd,
                               capture_output=True, text=True, timeout=TIMEOUT)
            text, tokens, error = parse_output(cfg["cli"], p.stdout, p.stderr, p.returncode)
        except FileNotFoundError:
            text, tokens, error = "", 0, f"{cfg['cli']} is not installed on this host"
        except subprocess.TimeoutExpired:
            text, tokens, error = "", 0, f"timed out after {TIMEOUT}s"
        if error and LIMIT_RE.search(error):
            self.cool_until = reset_time(error)
            report("cooling", tokens, 1, until=self.cool_until, room=m["room"])
            print(f"  limit hit, cooling until {time.ctime(self.cool_until)}: {error[:200]}", file=sys.stderr)
            return self.reply(m, f"(hit the {cfg['cli']} plan limit; back around "
                                 f"{time.strftime('%H:%M', time.localtime(self.cool_until))})")
        state = report("online", tokens, 1, room=m["room"])
        if state.startswith("over budget"):
            self.over_budget_day = time.strftime("%Y-%m-%d", time.gmtime())
        if error:
            print(f"  {cfg['cli']} failed: {error[:300]}", file=sys.stderr)
            return self.reply(m, f"({cfg['cli']} failed: {error.splitlines()[0][:200]})")
        self.reply(m, text)
        print(f"  answered #{m['id']} ({tokens} tokens, {power}{', deep' if deep else ''})", file=sys.stderr)

    def run(self):
        while True:
            try:
                self.refresh()
                if self.cfg.get("mode") != "worker":
                    # Manual: don't read, or we'd move the bookmark past messages meant for a person's session.
                    time.sleep(30)
                    continue
                if time.time() >= self.cool_until and self.cool_until:
                    self.cool_until = 0.0
                    report("online")
                batch = req("GET", "/api/v1/read", params={"format": "json", "wait": 50}, timeout=90)
                for m in batch["messages"]:
                    if m["for_me"]:
                        print(f"#{m['id']} from {m['sender']} in {m['room']}", file=sys.stderr)
                        try:
                            self.handle(m)
                        except urllib.error.HTTPError as e:
                            print(f"reply failed: {e.read().decode()[:200]}", file=sys.stderr)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                print(f"bunch unreachable ({e}); retrying in 10s", file=sys.stderr)
                time.sleep(10)


def main():
    if not KEY:
        sys.exit("set BUNCH_KEY")
    me = req("GET", "/api/v1/whoami")
    w = Worker(me)
    w.refresh(force=True)
    report("online")
    print(f"worker up as {me}: max power={MAX_POWER} dirs={DIRS or '-'} bunch={HUB}", file=sys.stderr)
    w.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
