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
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
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


OUT_DIR = "bunch-out"  # in a project folder: where the CLI leaves pictures for the room
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
               ".svg": "image/svg+xml"}  # the hub shows SVG only as a sandboxed image, never inline
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
def _run(argv, timeout=30):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
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
        if power == "answer":  # Codex's read-only sandbox still lets commands read any file the user can: no shell
            argv += ["--disable", "shell_tool", "--disable", "unified_exec"]
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
        # Loop guard: answer humans always, other agents only when they explicitly @mention us.
        if not job["human"] and not mentions(job["text"], name):
            return
        if job["created"] < self.started - STALE_AFTER:
            return
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
        # Pictures come back as files in a folder only the worker uses, so nothing else in a project gets posted.
        out = os.path.join(d, OUT_DIR) if d else self.scratch
        system += picture_hint(power, d)
        if cli == "agy" and power == "full":  # it can't run commands headless, but its own image tool works
            system += " For a picture, use your image generation tool, not commands."
        lowered = power != (cfg.get("power") or "answer")
        if lowered:
            print(f"  power {cfg.get('power')} not allowed here (WORKER_MAX_POWER/WORKER_DIRS); using {power}",
                  file=sys.stderr)
        argv, cwd, stdin = build_cmd(cfg, system, power, d or self.scratch,
                                     build_prompt(job, int(cfg.get("max_input_chars") or 12000)), deep)
        report(name, "busy", room=job["room"])
        started_at = time.time() - 1
        try:
            p = subprocess.run(argv, input=stdin, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT)
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
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
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


def install(args):
    flags = [a for a in args if a == "--share-ollama"]
    opts = dict(zip(*[iter([a for a in args if a not in flags])] * 2))
    hub, key = opts.get("--hub", "").rstrip("/"), opts.get("--key", "")
    unit = opts.get("--name", "bunch-worker")
    if not hub or not key:
        sys.exit("usage: worker.py install --hub URL --key MACHINE_KEY [--max-power answer|read|full] "
                 "[--dirs DIR:DIR] [--share-ollama] [--name UNIT]")
    cfg = os.path.expanduser("~/.config/bunch")
    units = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(cfg, exist_ok=True)
    os.makedirs(units, exist_ok=True)
    env = os.path.join(cfg, unit + ".env")
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"BUNCH_URL={hub}\nBUNCH_MACHINE_KEY={key}\n"
                f"WORKER_MAX_POWER={opts.get('--max-power', MAX_POWER)}\n"
                f"WORKER_DIRS={opts.get('--dirs', os.environ.get('WORKER_DIRS', ''))}\n"
                f"WORKER_SHARE_OLLAMA={'1' if '--share-ollama' in args or SHARE_OLLAMA else ''}\n"
                f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}\n")  # services don't get ~/.local/bin otherwise
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
