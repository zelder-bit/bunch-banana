#!/usr/bin/env python3
"""worker: puts this machine's paid CLIs (Claude Code, Codex, Antigravity) in the chat.
Python 3.8+, no dependencies.

Add the machine in the web viewer (Machines → + add machine). That shows a key once; run the
install line it gives you on this machine, as the user who is logged in to the CLIs:

  python3 worker.py install --hub http://bunch.lan:8700 --key bunchm_...

That writes ~/.config/bunch/bunch-worker.env and a systemd user service that starts at boot.
Then, in the viewer, put agents "On <this machine>" and pick a CLI for each. One worker serves
every agent on the machine. From any other device, @mention one of those agents and this
machine answers; while it's off, the hub replies at once that it's offline.

What the viewer may ask for is capped here, in the env file:
  WORKER_MAX_POWER=answer          # answer | read | full: the most this machine allows
  WORKER_DIRS=~/code/app:~/notes   # folders "read"/"full" may use
  WORKER_SHARE_OLLAMA=1            # offer this machine's Ollama models in the viewer (off by default)

Token thrift: every answer is a fresh CLI run that sees only the room summary, one-line
summaries of the latest messages, the message and the message it replies to. In "answer" mode Claude Code also drops its tools, MCP servers and
default system prompt. Jobs run one at a time. When a CLI says its plan's limit is hit, that CLI
cools down until the reset, and the hub tells anyone who asks in the meantime.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HUB = os.environ.get("BUNCH_URL", "http://127.0.0.1:8700").rstrip("/")
KEY = os.environ.get("BUNCH_MACHINE_KEY", "")
POWERS = ("answer", "read", "full")
CLIS = ("claude", "codex", "agy")
MAX_POWER = os.environ.get("WORKER_MAX_POWER", "answer").strip().lower()
DIRS = [os.path.realpath(os.path.expanduser(d)) for d in os.environ.get("WORKER_DIRS", "").split(os.pathsep) if d]
TIMEOUT = int(os.environ.get("WORKER_TIMEOUT", "600"))
COOLDOWN = int(os.environ.get("WORKER_COOLDOWN", "1800"))  # when the CLI doesn't say when the limit resets
SHARE_OLLAMA = os.environ.get("WORKER_SHARE_OLLAMA", "").strip().lower() in ("1", "true", "yes")
OLLAMA_URL = os.environ.get("WORKER_OLLAMA_URL", "http://localhost:11434").rstrip("/")
STALE_AFTER = 300  # ignore backlog older than this when the worker starts
DEFAULT_SYSTEM = ("You are {name}, an AI agent in a shared chat room with other agents and humans. "
                  "Answer the message addressed to you. Be brief and concrete. "
                  "If you need another agent, @mention them.")
# Every run's answer is posted for it: a CLI that tries its own bunch MCP tool instead gets denied headless.
PLAIN_REPLY = " Reply with your answer as plain text; it is posted to the room for you. Don't call chat tools."
LIMIT_RE = re.compile(r"usage limit|rate.?limit|hit your limit|limit reached|quota|resource.?exhausted|"
                      r"too many requests|\b429\b|out of (?:extra )?usage", re.I)
CLAUDE_MODELS = ["haiku", "sonnet", "opus"]
MENTION_RE = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9_.-]{0,31})")  # same as the hub's


def req(method, path, body=None, params=None, timeout=120):
    url = HUB + path + ("?" + urllib.parse.urlencode(params) if params else "")
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read().decode()
        is_json = resp.headers.get("Content-Type", "").startswith("application/json")
    return json.loads(raw) if is_json else raw


HEARTBEAT = 60  # seconds between pings while a CLI runs; the hub calls a machine offline after 180 s of silence


def ping():
    req("GET", "/api/v1/machine/ping", timeout=10)


class heartbeat:
    """While a CLI run blocks this thread, a daemon thread pings the hub so the machine stays online
    (the hub marks it offline after 3 quiet minutes, and runs can take 10). Ping only: it consumes no
    jobs, changes no agent status and touches none of the worker's state. A failed ping (hub down, an
    older hub without /ping) is ignored: the run must not die for it. Stops when the `with` block ends,
    however it ends."""

    def __init__(self, interval=HEARTBEAT, send=ping):
        self.interval, self.send, self.stop = interval, send, threading.Event()
        self.thread = threading.Thread(target=self._beat, daemon=True)

    def _beat(self):
        while not self.stop.wait(self.interval):
            try:
                self.send()
            except Exception as e:  # never raise out of the thread
                print(f"heartbeat failed: {e}", file=sys.stderr)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(1)  # it's waiting on the event, so this is instant unless a ping is in flight


OUT_DIR = "bunch-out"  # in a project folder: where the CLI leaves pictures for the room
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
               ".svg": "image/svg+xml"}  # the hub shows SVG only as a sandboxed image, never inline

# ------------------------------------------------------------------ the room's shared folder (#84)
# A room can have one folder kept on the hub, either a git repository or a plain directory. With
# WORKER_ROOM_FOLDERS=1 this machine keeps a copy per room, runs the CLI inside it and sends the changes back.
ROOM_FOLDERS = os.environ.get("WORKER_ROOM_FOLDERS", "").strip().lower() in ("1", "true", "yes")
ROOMS_DIR = os.path.realpath(os.path.expanduser(
    os.environ.get("WORKER_ROOMS_DIR", "") or os.path.join("~", ".local", "share", "bunch-worker", "rooms")))


def raw(method, path, params, data=None, timeout=300):
    """Like req, for bytes in and out."""
    r = urllib.request.Request(HUB + path + "?" + urllib.parse.urlencode(params), method=method, data=data, headers={
        "Authorization": "Bearer " + KEY, "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.read()


def capped_power(cfg, max_power=MAX_POWER):
    """The agent's power under this machine's ceiling, before any folder is considered."""
    want = cfg.get("power") if cfg.get("power") in POWERS else "answer"
    top = max_power if max_power in POWERS else "answer"
    return want if POWERS.index(want) <= POWERS.index(top) else top


def folder_hint(kind, power):
    """What the run is told about where it is working, so it knows what happens to its changes."""
    if kind == "git":
        text = (" You are working in this room's shared folder, a git repository that every agent in the room uses."
                " History is kept: each change is recorded with who made it and can be undone.")
        if power == "full":
            text += (" Just edit files: your changes are committed under your name and shared for you when you finish."
                     " Don't push, and don't rewrite history. If someone changed the same lines meanwhile, your work"
                     " is kept on a side branch and the room is told.")
    else:
        text = (" You are working in this room's shared folder, a plain folder that every agent in the room uses."
                " It is NOT a git repository: there is no history and no undo.")
        if power == "full":
            text += (" Files you change replace the shared copies when you finish, even if someone else changed them"
                     " meanwhile, so change only what you need to and never delete files you weren't asked to.")
    return text if power == "full" else text + " You can read these files but not change them in this run."


def _g(d, *args, check=True):
    p = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "advice.detachedHead=false", *args],
                       cwd=d, env=child_env(), capture_output=True, text=True, timeout=300)
    if check and p.returncode:
        raise OSError("git " + args[0] + ": " + (p.stderr.strip().splitlines() or ["failed"])[-1][:200])
    return p.stdout.strip()


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _local_files(d):
    """Relative path -> sha256 of everything in a plain folder copy, minus the worker's own picture folder."""
    out = {}
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if not (root == d and x == OUT_DIR) and not os.path.islink(os.path.join(root, x))]
        for f in files:
            full = os.path.join(root, f)
            if not os.path.islink(full):
                out[os.path.relpath(full, d).replace(os.sep, "/")] = _sha(full)
    return out


class RoomFolder:
    """This machine's copy of one room's folder for one job. fetch() before the CLI runs, publish() after."""

    def __init__(self, agent, room, kind, rooms_dir=None):
        self.agent, self.room, self.kind = agent, room, kind
        self.dir = os.path.join(os.path.realpath(rooms_dir or ROOMS_DIR), urllib.parse.quote(room, safe=""))
        self.q = {"agent": agent, "room": room}
        self.base = {}  # plain: what each file looked like when we fetched

    def fetch(self):
        os.makedirs(self.dir, exist_ok=True)
        if self.kind == "git":
            self._fetch_git()
            # Whatever the last run left is already on the hub (main or a side branch): start from the room's latest.
            _g(self.dir, "checkout", "-q", "-f", "-B", "main", "origin/main")
            _g(self.dir, "clean", "-q", "-fd")
        else:
            listing = {f["path"]: f["sha"] for f in req("GET", "/api/v1/machine/folder", params=self.q)["files"]}
            have = _local_files(self.dir)
            for path in have.keys() - listing.keys():
                os.remove(os.path.join(self.dir, *path.split("/")))
            for path, sha in listing.items():
                if have.get(path) != sha and self._inside(path):
                    full = os.path.join(self.dir, *path.split("/"))
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    with open(full, "wb") as f:
                        f.write(raw("GET", "/api/v1/machine/folder/file", {**self.q, "path": path}))
            self.base = listing
        return self.dir

    def _inside(self, path):
        full = os.path.realpath(os.path.join(self.dir, *path.split("/")))
        return full.startswith(self.dir + os.sep) and ".." not in path.split("/")

    def _fetch_git(self):
        if not os.path.isdir(os.path.join(self.dir, ".git")):
            _g(self.dir, "init", "-q", "-b", "main")
            with open(os.path.join(self.dir, ".git", "info", "exclude"), "a") as f:
                f.write(f"/{OUT_DIR}/\n")  # the worker's picture folder is never part of the room's files
        with tempfile.NamedTemporaryFile(suffix=".bundle") as tmp:
            tmp.write(raw("GET", "/api/v1/machine/folder/bundle", self.q))
            tmp.flush()
            _g(self.dir, "fetch", "-q", "--no-tags", tmp.name, "+refs/heads/main:refs/remotes/origin/main")

    def publish(self, msg_id):
        """Sends what the run changed. Returns a short note for the room ("" when nothing changed)."""
        return self._publish_git(msg_id) if self.kind == "git" else self._publish_plain()

    def _publish_git(self, msg_id):
        d = self.dir
        _g(d, "add", "-A")
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=d, env=child_env()).returncode:
            _g(d, "-c", f"user.name={self.agent}", "-c", f"user.email={self.agent}@bunch.invalid", "commit", "-q",
               "-m", f"{self.agent}: work for message #{msg_id} in #{self.room}")
        if _g(d, "rev-parse", "HEAD") == _g(d, "rev-parse", "origin/main"):
            return ""
        _g(d, "branch", "-f", f"unsent-{msg_id}", "HEAD")  # kept here if the hub can't be reached below
        before = _g(d, "rev-parse", "origin/main")
        self._fetch_git()
        if _g(d, "rev-parse", "origin/main") != before:  # someone else got there first: put ours on top if it fits
            p = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", f"user.name={self.agent}",
                                "-c", f"user.email={self.agent}@bunch.invalid", "rebase", "-q", "origin/main"],
                               cwd=d, env=child_env(), capture_output=True)
            if p.returncode:
                _g(d, "rebase", "--abort", check=False)
        _g(d, "branch", "-f", "bunch-send", "HEAD")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.bundle")
            _g(d, "bundle", "create", "-q", path, "origin/main..bunch-send")
            with open(path, "rb") as f:
                r = json.loads(raw("POST", "/api/v1/machine/folder/bundle", {**self.q, "id": msg_id}, f.read()))
        _g(d, "branch", "-D", f"unsent-{msg_id}", check=False)
        n = r.get("files", 0)
        files = f"{n} file{'' if n == 1 else 's'}"
        if r.get("result") == "branch":
            return (f"(changed {files} in the room folder, but it clashes with newer work there: mine is kept on "
                    f"branch {r['branch']} until someone merges it)")
        return f"(changed {files} in the room folder)"

    def _publish_plain(self):
        now, sent, gone, replaced, failed = _local_files(self.dir), [], [], [], []
        for path, sha in now.items():
            if self.base.get(path) == sha:
                continue
            try:
                with open(os.path.join(self.dir, *path.split("/")), "rb") as f:
                    r = json.loads(raw("PUT", "/api/v1/machine/folder/file",
                                       {**self.q, "path": path, "base": self.base.get(path, "")}, f.read()))
                sent.append(path)
                if r.get("overwrote"):
                    replaced.append(path)
            except urllib.error.HTTPError as e:
                failed.append(f"{path} ({e.code})")
        for path in self.base.keys() - now.keys():
            try:
                raw("DELETE", "/api/v1/machine/folder/file", {**self.q, "path": path})
                gone.append(path)
            except urllib.error.HTTPError as e:
                failed.append(f"{path} ({e.code})")
        if not (sent or gone or failed):
            return ""
        n = len(sent) + len(gone)
        note = f"changed {n} file{'' if n == 1 else 's'} in the room folder"
        if replaced:
            note += "; replaced someone's newer copy of " + ", ".join(replaced[:5])
        if failed:
            note += "; the hub refused " + ", ".join(failed[:5])
        return f"({note})"


# A reply that says it made a picture or file. Checked against what was actually uploaded.
CLAIM_RE = re.compile(r"\b(?:i(?:'ve| have)?\s+)?(?:created|made|generated|drew|drawn|painted|rendered|saved|attached)\b"
                      r"(?:[^.!?\n]|\.(?=\S)){0,60}\b(?:image|picture|pic|png|jpe?g|svg|drawing|illustration|painting|"
                      r"watercolou?r|sketch|logo|photo|wallpaper|background|file)s?\b", re.I)


def find_images(root, since):
    """Image files the CLI wrote under root (3 levels deep) since `since`: how a CLI hands back a picture."""
    found = []
    base = root.rstrip(os.sep).count(os.sep)
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".") and d.count(os.sep) - base < 2]
        for f in files:
            p = os.path.join(d, f)
            try:
                if os.path.splitext(f)[1].lower() in IMAGE_TYPES and os.path.getmtime(p) >= since:
                    found.append(p)
            except OSError:
                pass
    return sorted(found)[:4]


AGY_BRAIN = os.path.expanduser("~/.gemini/antigravity-cli/brain")  # agy keeps each conversation's files here


def agy_images(out, since, brain=AGY_BRAIN):
    """Pictures agy's image tool made in this run. It saves them in its conversation folder, not the
    working folder; the JSON output names that conversation, so nothing from other agy sessions is picked up."""
    try:
        cid = json.loads(out).get("conversation_id") or ""
    except (ValueError, AttributeError):
        return []
    if not re.fullmatch(r"[0-9a-f-]{8,64}", cid):
        return []
    d = os.path.join(brain, cid)
    return find_images(d, since) if os.path.isdir(d) else []


def upload(agent, room, path):
    """Stores the file on the hub; returns the ![name](file:ID) text that makes the viewer show it."""
    with open(path, "rb") as f:
        data = f.read()
    q = urllib.parse.urlencode({"agent": agent, "room": room, "name": os.path.basename(path)})
    r = urllib.request.Request(f"{HUB}/api/v1/machine/file?{q}", method="POST", data=data, headers={
        "Authorization": "Bearer " + KEY, "Content-Type": IMAGE_TYPES[os.path.splitext(path)[1].lower()]})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read().decode())["markdown"]


def report(agent, status, tokens=0, calls=0, until=None, room=None, cached=0):
    try:
        return req("POST", "/api/v1/machine/report", {"agent": agent, "status": status, "tokens": tokens,
                                                      "cached": cached, "calls": calls, "until": until, "room": room})
    except (urllib.error.URLError, OSError) as e:
        print(f"report failed: {e}", file=sys.stderr)
        return ""


# ------------------------------------------------------------------ what this machine has
def child_env():
    # what the CLIs we start get to see: no machine key, hub address or worker settings
    drop = ("BUNCH_MACHINE_KEY", "BUNCH_KEY", "BUNCH_URL")
    return {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("WORKER_")}


def _run(argv, timeout=30):
    try:
        return subprocess.run(argv, capture_output=True, env=child_env(), text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def detect_clis():
    found = []
    for cli in CLIS:
        if not shutil.which(cli):
            continue
        v = re.search(r"\d+(?:\.\d+)+", _run([cli, "--version"], 15))  # "2.1.312 (Claude Code)" -> 2.1.312
        version = v.group(0) if v else ""
        models = []
        if cli == "claude":
            models = CLAUDE_MODELS
        elif cli == "agy":  # "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)" per line
            models = [ln.split("\t")[0].strip() for ln in _run(["agy", "models"], 60).splitlines()
                      if "\t" in ln and ln.split("\t")[0].strip()]
        found.append({"cli": cli, "version": version, "models": models})
    return found


def ollama_models(url=OLLAMA_URL):
    """The local Ollama's model tags, for the hub to offer in the viewer. None when Ollama isn't answering."""
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=5) as r:
            tags = [m.get("name") or m.get("model") for m in json.loads(r.read().decode()).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return {"port": urllib.parse.urlsplit(url).port or 11434, "models": sorted(t for t in tags if t)}


def addresses():
    """Names and IPs this machine is reachable as, so the hub can tell which gateway models run here."""
    out = {socket.gethostname(), socket.getfqdn()}
    try:  # the LAN address the default route uses
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            out.add(s.getsockname()[0])
    except OSError:
        pass
    if shutil.which("tailscale"):
        out.update(_run(["tailscale", "ip"], 5).split())
        try:
            dns = json.loads(_run(["tailscale", "status", "--json", "--peers=false"], 5) or "{}")
            out.add((dns.get("Self") or {}).get("DNSName", "").rstrip("."))
        except ValueError:
            pass
    return sorted(a for a in out if a and a not in ("localhost", "127.0.0.1", "::1"))


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


CODEX_ANSWER_OFF = ("shell_tool", "unified_exec", "view_image", "browser_use", "browser_use_external",
                    "computer_use", "apps", "plugins", "multi_agent")


def build_cmd(cfg, system, power, workdir, prompt, deep=False):
    """One headless run. Returns (argv, cwd, stdin)."""
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
        return argv, workdir, prompt
    if cli == "codex":
        argv = ["codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--ignore-rules"]
        argv += ["-m", model] if model else []
        if power == "answer":  # Codex's read-only sandbox still lets commands read any file the user can: no shell,
            # nor the other tools that can open files or reach out (checked on codex-cli 0.166), nor the
            # person's own MCP servers. An older Codex without one of these flags stops with "Unknown feature
            # flag" instead of running unprotected.
            argv += ["-c", "mcp_servers={}"]
            for feature in CODEX_ANSWER_OFF:
                argv += ["--disable", feature]
        argv += ["-s", "workspace-write" if power == "full" else "read-only", "-C", workdir, "-"]
        return argv, workdir, f"{system}\n\n{prompt}"
    if cli == "agy":  # agy ignores stdin: the prompt rides in -p
        argv = ["agy", "--output-format", "json", "--print-timeout", f"{TIMEOUT}s"]
        argv += ["--model", model] if model else []
        # Not --mode plan: that writes a plan file and asks to confirm it instead of answering (3-5x the tokens).
        argv += ["--mode", "accept-edits"] if power == "full" else ["--disable-slash-commands"]
        argv += ["--sandbox"] if power == "answer" else []
        argv += ["-p", f"{system}\n\n{prompt}"]
        return argv, workdir, None
    raise ValueError(f"unknown cli {cli}")


def parse_output(cli, out, err, rc):
    """Returns (text, tokens, cached, error). tokens leaves out cache reads, which come back separately
    in cached (they cost about a tenth as much). error is None on success."""
    text, tokens, cached, error = "", 0, 0, None
    try:
        if cli == "claude":
            d = json.loads(out)
            u = d.get("usage") or {}
            tokens = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "output_tokens"))
            cached = int(u.get("cache_read_input_tokens") or 0)
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
                    hit = int(u.get("cached_input_tokens") or 0)  # part of input_tokens
                    tokens += int(u.get("input_tokens") or 0) - hit + int(u.get("output_tokens") or 0)
                    cached += hit
                elif e.get("type") in ("error", "turn.failed"):
                    error = e.get("message") or (e.get("error") or {}).get("message") or "error"
        elif cli == "agy":
            d = json.loads(out)
            text = d.get("response") or ""
            u = d.get("usage") or {}
            cached = int(u.get("cache_read_tokens") or 0)  # part of total_tokens
            tokens = int(u.get("total_tokens") or 0) - cached
            if d.get("status") not in (None, "SUCCESS"):
                err_ = d.get("error")
                error = (err_.get("message") if isinstance(err_, dict) else err_) or text or d["status"]
            elif not text and d.get("denied_actions"):  # "SUCCESS" with nothing said: a tool was refused
                tools = ", ".join(a.get("display_name") or a.get("action") or "?" for a in d["denied_actions"])
                error = f"no answer: it tried {tools}, which headless runs can't use" + \
                    (" (agy mcp list shows the MCP servers it can reach)" if "mcp" in tools.lower() else "")
    except (ValueError, TypeError, AttributeError):
        pass
    if not text and not error:
        error = (err or out or f"exit {rc}").strip()[-300:] if rc or (err or "").strip() else "(empty answer)"
    return text.strip(), tokens, cached, error


def reset_time(msg, now=None):
    """When a usage limit lifts, from the CLI's message. Falls back to now + COOLDOWN."""
    now = now or time.time()
    m = re.search(r"\|(\d{10})\b", msg)  # Claude: "...limit reached|1767225600"
    if m:
        return float(m.group(1))
    m = re.search(r"\bin\s+(?=\d)(?:(\d+)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?", msg, re.I)
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


def mentions(text, name):
    """True when text @mentions exactly this name (@gemini is not in "@gemini-local")."""
    return name in (m.rstrip(".") for m in MENTION_RE.findall(text.lower()))


def picture_hint(power, d):
    """What the run is told about pictures: only a run that can write files may promise one."""
    if power == "full":
        return f" To send a picture, save it as a .png/.jpg/.webp file in ./{OUT_DIR}/."
    return (" You can't create files or images in this run: if asked for one, say so plainly "
            "and never claim you made it.")


def claims_picture(text):
    """True when an answer says it made a picture or file (a run that couldn't write one may still say so)."""
    return bool(CLAIM_RE.search(text or ""))


def no_picture_note(machine, power, max_power=MAX_POWER, dirs=DIRS):
    """Why a run couldn't hand back a picture, and what setting would let it."""
    if power == "full":
        return f"(no picture was attached: nothing was saved in ./{OUT_DIR}/)"
    how = 'set this agent\'s power to "Edit files and run commands" with a folder'
    if max_power != "full" or not dirs:
        how += f", and on {machine} set WORKER_MAX_POWER=full with that folder in WORKER_DIRS"
    return f"(no picture was attached: {machine} ran this as {power}, which can't save files. For pictures, {how}.)"


def empty_hint(cli, power):
    """A failed run's note with something the person can act on."""
    if cli == "agy":
        return "it may have tried an MCP tool; `agy mcp list` on that machine shows them"
    return f"try again, or give it {'more power' if power == 'answer' else 'a different model'}"


def build_prompt(job, limit):
    parts = [job["note"]] if job.get("note") else []  # bot mode (older hubs don't send it)
    parts.append(f"Room #{job['room']} summary: {job.get('room_summary') or '(none)'}")
    if job.get("recent"):  # older hubs don't send it
        parts.append(f"Recent messages:\n{job['recent']}")
    if job.get("parent"):
        p = job["parent"]
        parts.append(f"It replies to #{p['id']} from {p['sender']}:\n{p['text'][:limit // 3]}")
    parts.append(f"Message #{job['id']} from {job['sender']}:\n{job['text'].replace('!deep', '').strip()[:limit]}")
    return "\n\n".join(parts)


# ------------------------------------------------------------------ the loop
class Worker:
    def __init__(self):
        self.machine = ""
        self.agents = {}           # agent name -> config, from the hub
        self.clis = []
        self.said_hello = 0.0
        self.cool_until = {}       # cli -> when its plan's limit lifts (limits are per login, not per agent)
        self.over_budget = {}      # agent -> UTC day its budget ran out
        self.scratch = tempfile.mkdtemp(prefix="bunch-worker-")  # empty cwd for "answer" runs
        self.started = time.time()

    def hello(self, force=False):
        if not force and time.time() - self.said_hello < 60:
            return
        if force or not self.clis:
            self.clis = detect_clis()
        r = req("POST", "/api/v1/machine/hello", {"hostname": socket.gethostname(), "addresses": addresses(),
                                                  "clis": self.clis, "max_power": MAX_POWER, "dirs": DIRS,
                                                  "ollama": ollama_models() if SHARE_OLLAMA else None})
        agents = {a["name"]: a for a in r["agents"]}
        if agents != self.agents or not self.machine:
            print(f"machine {r['machine']}: " + (", ".join(f"{n} ({a['cli']})" for n, a in agents.items())
                                                 or "no agents yet; add some in the viewer"), file=sys.stderr)
        self.machine, self.agents, self.said_hello = r["machine"], agents, time.time()

    def reply(self, job, text):
        req("POST", "/api/v1/machine/post", {"agent": job["agent"], "room": job["room"], "text": text, "re": job["id"]})

    def handle(self, job):
        name = job["agent"]
        # Loop guard: the hub only hands over messages that should wake us ("wake"); older hubs don't say,
        # so then answer humans always, other agents only when they explicitly @mention us.
        if not job["human"] and not job.get("wake", mentions(job["text"], name)):
            return
        # Backlog from before this worker started. "age" is measured on the hub's clock, so a clock that is
        # off on this machine can't make every job look old; hubs that don't send it fall back to "created".
        age = job.get("age")
        stale = (age - (time.time() - self.started) > STALE_AFTER) if isinstance(age, (int, float)) \
            else job["created"] < self.started - STALE_AFTER
        if stale:
            return self.reply(job, f"(skipped: sent before {self.machine} started)")
        cfg = self.agents.get(name)
        if not cfg:  # made in the viewer since the last hello: the hub already handed us its message
            self.hello(force=True)
            cfg = self.agents.get(name)
        if not cfg:
            return self.reply(job, f"({name} isn't set up on {self.machine} yet; not answered)")
        cli = cfg["cli"]
        until = self.cool_until.get(cli, 0)
        if time.time() < until:
            report(name, "cooling", until=until)
            return self.reply(job, f"(out of {cli} quota until {time.strftime('%H:%M', time.localtime(until))})")
        if self.over_budget.get(name) == time.strftime("%Y-%m-%d", time.gmtime()):
            return self.reply(job, "(daily token budget used; resets 00:00 UTC)")
        deep = "!deep" in job["text"]
        system = (cfg.get("system") or DEFAULT_SYSTEM.format(name=name)) + \
            f" Keep the answer under about {int(cfg.get('max_output') or 800)} tokens." + PLAIN_REPLY
        power, d = effective_power(cfg)
        if power != "answer" and d and not os.path.isdir(d):
            return self.reply(job, f"(the folder {d} is missing on {self.machine}; not answered)")
        # A room with a shared folder: work there instead of the agent's own folder, when this machine allows it.
        shared, shared_note = None, ""
        if job.get("folder") and capped_power(cfg) != "answer":
            if not ROOM_FOLDERS:
                shared_note = f"(#{job['room']} has a shared folder, but {self.machine} hasn't turned on WORKER_ROOM_FOLDERS)"
            else:
                try:
                    shared = RoomFolder(name, job["room"], job["folder"]["kind"])
                    power, d = capped_power(cfg), shared.fetch()
                except (OSError, urllib.error.URLError, subprocess.TimeoutExpired) as e:
                    print(f"  room folder not ready: {e}", file=sys.stderr)
                    shared, shared_note = None, f"(couldn't get the room folder: {str(e)[:160]})"
        # Pictures come back as files in a folder only the worker uses, so nothing else in a project gets posted.
        out = os.path.join(d, OUT_DIR) if d else self.scratch
        system += picture_hint(power, d)
        if shared:
            system += folder_hint(shared.kind, power)
        if cli == "agy" and power == "full":  # it can't run commands headless, but its own image tool works
            system += " For a picture, use your image generation tool, not commands."
        lowered = power != (cfg.get("power") or "answer") and not shared_note
        if lowered:
            print(f"  power {cfg.get('power')} not allowed here (WORKER_MAX_POWER/WORKER_DIRS); using {power}",
                  file=sys.stderr)
        argv, cwd, stdin = build_cmd(cfg, system, power, d or self.scratch,
                                     build_prompt(job, int(cfg.get("max_input_chars") or 12000)), deep)
        report(name, "busy", room=job["room"])
        started_at = time.time() - 1
        try:
            with heartbeat():
                p = subprocess.run(argv, input=stdin, cwd=cwd, env=child_env(), capture_output=True, text=True, timeout=TIMEOUT)
            text, tokens, cached, error = parse_output(cli, p.stdout, p.stderr, p.returncode)
        except FileNotFoundError:
            text, tokens, cached, error = "", 0, 0, f"{cli} is not installed on {self.machine}"
        except subprocess.TimeoutExpired:
            text, tokens, cached, error = "", 0, 0, f"timed out after {TIMEOUT}s"
        pics = []
        if cli == "agy" and power == "full" and not error:
            os.makedirs(out, exist_ok=True)
            for path in agy_images(p.stdout, started_at):
                try:
                    shutil.move(path, os.path.join(out, os.path.basename(path)))
                except OSError as e:
                    print(f"  could not take {path}: {e}", file=sys.stderr)
        if not LIMIT_RE.search(error or ""):
            for path in find_images(out, started_at) if os.path.isdir(out) else []:
                try:
                    pics.append(upload(name, job["room"], path))
                except (urllib.error.URLError, OSError) as e:
                    print(f"  could not upload {path}: {e}", file=sys.stderr)
                    continue
                try:
                    os.remove(path)  # it's on the hub now; the folder is the worker's, not the project's
                except OSError as e:
                    print(f"  uploaded but could not remove {path}: {e}", file=sys.stderr)
        if pics:
            text = (text + "\n\n" + "\n".join(pics)).strip()
            error = None  # an image with no words is still an answer
        if shared and power == "full" and not LIMIT_RE.search(error or ""):  # even a failed run may have changed files
            try:
                shared_note = shared.publish(job["id"])
            except (OSError, urllib.error.URLError, subprocess.TimeoutExpired, ValueError) as e:
                print(f"  room folder not sent: {e}", file=sys.stderr)
                shared_note = f"(my changes to the room folder weren't shared: {str(e)[:160]})"
        if error and LIMIT_RE.search(error):
            self.cool_until[cli] = until = reset_time(error)
            for other, c in self.agents.items():  # same login: every agent on this CLI is out
                if c["cli"] == cli:
                    report(other, "cooling", tokens if other == name else 0, 1 if other == name else 0,
                           until=until, room=job["room"] if other == name else None,
                           cached=cached if other == name else 0)
            print(f"  {cli} limit hit, cooling until {time.ctime(until)}: {error[:200]}", file=sys.stderr)
            return self.reply(job, f"(hit the {cli} plan limit; back around "
                                   f"{time.strftime('%H:%M', time.localtime(until))})")
        state = report(name, "online", tokens, 1, room=job["room"], cached=cached)
        if state.startswith("over budget"):
            self.over_budget[name] = time.strftime("%Y-%m-%d", time.gmtime())
        if error:
            print(f"  {cli} failed: {error[:300]}", file=sys.stderr)
            hint = f"; {empty_hint(cli, power)}" if error == "(empty answer)" else ""
            return self.reply(job, f"({cli} failed: {error.splitlines()[0][:200]}{hint})")
        if lowered:  # say it in the chat too, not only in this machine's journal
            text += f"\n\n({self.machine} allows only {power}, not {cfg.get('power')}: ran as {power})"
        if shared_note:
            text += "\n\n" + shared_note
        if not pics and claims_picture(text):  # it says it made something, but nothing came back
            text += "\n\n" + no_picture_note(self.machine, power)
        self.reply(job, text)
        print(f"  {name} answered #{job['id']} ({tokens} tokens + {cached} cached, {cli}, {power}{', deep' if deep else ''})",
              file=sys.stderr)

    def run(self):
        while True:
            try:
                self.hello()
                for cli, until in list(self.cool_until.items()):
                    if time.time() >= until:  # limit lifted: tell the hub so the router uses it again
                        del self.cool_until[cli]
                        for n, c in self.agents.items():
                            if c["cli"] == cli:
                                report(n, "online")
                batch = req("GET", "/api/v1/machine/read", params={"wait": 50}, timeout=90)
                for job in batch["jobs"]:
                    print(f"#{job['id']} for {job['agent']} from {job['sender']} in {job['room']}", file=sys.stderr)
                    try:
                        self.handle(job)
                    except urllib.error.HTTPError as e:
                        print(f"reply failed: {e.read().decode()[:200]}", file=sys.stderr)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    sys.exit("the hub rejected this machine key (deleted or rotated?); run install again")
                print(f"hub error {e.code}; retrying in 10s", file=sys.stderr)
                time.sleep(10)
            except OSError as e:  # URLError, timeouts (socket.timeout on Python < 3.10), resets
                print(f"bunch unreachable ({e}); retrying in 10s", file=sys.stderr)
                time.sleep(10)


# ------------------------------------------------------------------ install as a service
UNIT = """[Unit]
Description=bunch worker: this machine's CLIs in the chat
After=network-online.target

[Service]
EnvironmentFile={env}
ExecStart={python} {script}
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def merged_env(old, given, defaults=None):
    """Merge old env text with given and default key-value pairs, returning updated text and list of kept keys."""
    old_lines = [line for line in old.splitlines() if line and not line.startswith('#')]
    old_dict = dict(line.split('=', 1) for line in old_lines if '=' in line)
    kept = []
    result = []
    seen = set()

    for key, value in old_dict.items():
        if key in given:
            result.append(f"{key}={given[key]}")
            seen.add(key)
        else:
            result.append(f"{key}={value}")
            kept.append(key)
            seen.add(key)

    for key, value in given.items():
        if key not in seen:
            result.append(f"{key}={value}")
            seen.add(key)

    if defaults:
        for key, value in defaults.items():
            if key not in seen:
                result.append(f"{key}={value}")
                seen.add(key)

    return '\n'.join(result) + '\n', kept


def write_env(path, text):
    """The env file holds the machine key: owner-only, also when it already existed with looser permissions."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def read_env(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def rotate_key(args):
    """Swap the machine key and nothing else, then restart the service."""
    opts = dict(zip(args[::2], args[1::2]))
    unit, key = opts.get("--name", "bunch-worker"), opts.get("--key", "")
    env = os.path.expanduser(f"~/.config/bunch/{unit}.env")
    if not key or not os.path.exists(env):
        sys.exit(f"usage: worker.py rotate-key --key MACHINE_KEY [--name UNIT]   (needs an installed worker: {env})")
    text, kept = merged_env(read_env(env), {"BUNCH_MACHINE_KEY": key})
    write_env(env, text)
    if subprocess.run(["systemctl", "--user", "restart", unit]).returncode:
        sys.exit(f"key saved, but systemctl --user restart {unit} failed")
    print(f"new key saved for {unit}; kept {', '.join(kept)}")


def install(args):
    flags = [a for a in args if a == "--share-ollama" or a == "--room-folders"]
    opts = dict(zip(*[iter([a for a in args if a not in flags])] * 2))
    hub, key = opts.get("--hub", "").rstrip("/"), opts.get("--key", "")
    unit = opts.get("--name", "bunch-worker")
    if not hub or not key:
        sys.exit("usage: worker.py install --hub URL --key MACHINE_KEY [--max-power answer|read|full] "
                 "[--dirs DIR:DIR] [--share-ollama] [--room-folders] [--name UNIT]")
    cfg = os.path.expanduser("~/.config/bunch")
    units = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(cfg, exist_ok=True)
    os.makedirs(units, exist_ok=True)
    env = os.path.join(cfg, unit + ".env")

    given = {
        "BUNCH_URL": hub,
        "BUNCH_MACHINE_KEY": key,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),  # services don't get ~/.local/bin otherwise
    }
    if "--max-power" in opts:
        given["WORKER_MAX_POWER"] = opts["--max-power"]
    if "--dirs" in opts:
        given["WORKER_DIRS"] = opts["--dirs"]
    if "--share-ollama" in args:
        given["WORKER_SHARE_OLLAMA"] = "1"
    if "--room-folders" in args:
        given["WORKER_ROOM_FOLDERS"] = "1"

    defaults = {
        "WORKER_MAX_POWER": MAX_POWER,
        "WORKER_DIRS": os.environ.get("WORKER_DIRS", ""),
        "WORKER_SHARE_OLLAMA": "1" if SHARE_OLLAMA else "",
        "WORKER_ROOM_FOLDERS": "1" if ROOM_FOLDERS else "",
    }

    old = read_env(env)
    text, kept = merged_env(old, given, defaults)
    write_env(env, text)
    if kept:
        print(f"kept from the existing {env}: {', '.join(kept)}")

    with open(os.path.join(units, unit + ".service"), "w") as f:
        f.write(UNIT.format(env=env, python=sys.executable, script=os.path.realpath(__file__)))
    for cmd in (["daemon-reload"], ["enable", "--now", unit]):
        if subprocess.run(["systemctl", "--user", *cmd]).returncode:
            sys.exit(f"systemctl --user {' '.join(cmd)} failed")
    print(f"installed {unit}: logs with  journalctl --user -u {unit} -f")
    print(f"to keep it running while you're logged out:  loginctl enable-linger {os.environ.get('USER', '$USER')}")


def uninstall(args):
    opts = dict(zip(args[::2], args[1::2]))
    unit = opts.get("--name", "bunch-worker")
    subprocess.run(["systemctl", "--user", "disable", "--now", unit])
    for p in (f"~/.config/systemd/user/{unit}.service", f"~/.config/bunch/{unit}.env"):
        p = os.path.expanduser(p)
        if os.path.exists(p):
            os.remove(p)
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    print(f"removed {unit}")


def main():
    if sys.argv[1:2] == ["install"]:
        return install(sys.argv[2:])
    if sys.argv[1:2] == ["rotate-key"]:
        return rotate_key(sys.argv[2:])
    if sys.argv[1:2] == ["uninstall"]:
        return uninstall(sys.argv[2:])
    if not KEY:
        sys.exit("set BUNCH_MACHINE_KEY (or run: worker.py install --hub URL --key KEY)")
    w = Worker()
    w.hello(force=True)
    print(f"worker up on {w.machine}: clis={','.join(c['cli'] for c in w.clis) or 'none found'} "
          f"max power={MAX_POWER} dirs={DIRS or '-'} ollama={'shared' if SHARE_OLLAMA else 'not shared'} bunch={HUB}", file=sys.stderr)
    w.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
