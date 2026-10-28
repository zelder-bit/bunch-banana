"""The hub's actual behaviour. Every agent-facing tool lives here and returns
compact plain text, because whatever we return lands in an LLM's context window.
MCP and the REST API are just two doors into these same functions."""
from __future__ import annotations

import asyncio
import json
import re
import time

from .config import Settings
from .llm import LLM, LLMError
from .store import Store, valid_name

WORKER_CLIS = ("claude", "codex", "agy")
WORKER_POWERS = ("answer", "read", "full")  # no tools / read files in dir / edit + run in dir
WORKER_STATUSES = ("online", "busy", "cooling")
WORKER_OFFLINE_AFTER = 180  # a machine long-polls every ~50 s; silent this long = off or not running

MENTION_RE = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9_.-]{0,31})")


class HubError(Exception):
    """A user-facing error. Message is short on purpose (it costs tokens)."""


def est_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def fmt_size(n: int) -> str:
    return str(n) if n < 1000 else f"{n / 1000:.1f}k"


def one_line(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _snippet(text: str, q: str, width: int = 160) -> str:
    """One line of text around the first match of q."""
    i = text.lower().find(q.lower())
    start = max(0, i - width // 3) if i >= 0 else 0
    piece = " ".join(text[start:start + width].split())
    return ("…" if start else "") + piece + ("…" if start + width < len(text) else "")


def _recips(m) -> list[str]:
    return [r for r in (m["recipients"] or "").strip(",").split(",") if r]


class Hub:
    def __init__(self, store: Store, settings: Settings, llm: LLM):
        self.store = store
        self.s = settings
        self.llm = llm
        self.listeners: set[asyncio.Queue] = set()  # web viewer sockets
        self._event = asyncio.Event()                # wakes long-polling readers
        self._hosts: dict[str, str] = {}            # gateway model -> host, from LiteLLM
        self._hosts_at = 0.0

    # ---------------------------------------------------------------- events
    def broadcast(self, event: dict) -> None:
        """Send an event to every open web viewer."""
        for q in list(self.listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _notify(self, mid: int) -> None:
        self.broadcast({"type": "message", "message": self.message_json(self.store.message(mid))})
        self._event.set()
        self._event = asyncio.Event()

    async def wait_for_new(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    def message_json(m) -> dict:
        return {
            "id": m["id"], "room": m["room"], "sender": m["sender"], "human": bool(m["human"]),
            "to": _recips(m), "summary": m["summary"], "body": m["body"],
            "reply_to": m["reply_to"], "created": m["created"],
        }

    def _charge_ctx(self, agent: str, text: str) -> str:
        self.store.add_usage(agent, ctx=est_tokens(text))
        return text

    # ------------------------------------------------------------------ post
    def post(self, sender: str, room: str, text: str, to=None, summary: str | None = None,
             reply_to: int | None = None, human: bool = False) -> str:
        room = (room or "").strip().lower().lstrip("#")
        if not valid_name(room):
            raise HubError("bad room name (lowercase a-z 0-9 _ . -)")
        text = (text or "").strip()
        if not text:
            raise HubError("empty text")
        if len(text) > self.s.max_body:
            raise HubError(f"text too long ({len(text)} > {self.s.max_body} chars)")

        if not human:
            self._check_rate(sender)
            self._check_chain(room)

        # Recipients: explicit `to` + @mentions of known names; replies default to the parent's author.
        known = self.store.agent_names()
        if isinstance(to, str):
            to = [t for t in re.split(r"[,\s]+", to) if t]
        recips: list[str] = []
        for name in list(to or []) + MENTION_RE.findall(text):
            name = name.lower().lstrip("@").rstrip(".")  # "@coder." ends a sentence
            if name in known and name != sender and name not in recips:
                recips.append(name)
        if reply_to is not None:
            parent = self.store.message(int(reply_to))
            if not parent:
                raise HubError(f"no message #{reply_to}")
            if not recips and parent["sender"] != sender:
                recips = [parent["sender"]]

        # Short text is its own summary. Long text is stored as a body; the summary is what readers see.
        auto = False
        if summary:
            summ, body = one_line(summary, self.s.summary_max), text
        elif len(text) <= self.s.summary_max and "\n" not in text:
            summ, body = text, None
        else:  # keep formatting in the body; summarizer may later improve a truncated summary
            summ, body = one_line(text, self.s.summary_max), text
            auto = len(text) > self.s.summary_max

        self.store.ensure_room(room)
        if self.store.cursor(sender, room) is None:          # posting joins you, skipping old backlog
            self.store.set_cursor(sender, room, self.store.room_max_id(room))
        mid = self.store.add_message(room, sender, human, recips, summ, body, reply_to, auto)
        self._notify(mid)
        if recips:
            self._unavailable_notices(mid)
        return f"ok #{mid}"

    def _check_rate(self, sender: str) -> None:
        if self.s.posts_per_min <= 0:
            return
        row = self.store.one("SELECT COUNT(*) AS n FROM messages WHERE sender=? AND created>?",
                             (sender, time.time() - 60))
        if row["n"] >= self.s.posts_per_min:
            raise HubError("rate limit: too many posts this minute")

    def paused(self, room: str) -> bool:
        """Loop breaker: after max_chain agent-only messages in a row, a human has to speak."""
        n = self.s.max_chain
        if n <= 0:
            return False
        rows = self.store.q("SELECT human FROM messages WHERE room=? ORDER BY id DESC LIMIT ?", (room, n))
        return len(rows) >= n and not any(r["human"] for r in rows)

    def _check_chain(self, room: str) -> None:
        if self.paused(room):
            raise HubError(f"paused: {self.s.max_chain} agent messages in a row in '{room}'; waiting for a human")

    # ------------------------------------------------------------------ read
    def read_rows(self, agent: str, room: str | None = None, everything: bool = False):
        """Returns (header_lines, rows, more). Advances the agent's cursors."""
        st, limit = self.store, self.s.read_limit
        me_like = f",{agent},"  # exact match (LIKE would treat _ as a wildcard)
        header: list[str] = []

        if room:
            room = room.strip().lower().lstrip("#")
            if not st.room(room):
                raise HubError(f"no room '{room}'")
            cur = st.cursor(agent, room)
            relevant = "1=1" if everything else "(recipients='' OR instr(recipients, ?) > 0)"
            args = () if everything else (me_like,)
            if cur is None:  # first visit: summary + short tail instead of the whole backlog
                r = st.room(room)
                if r["summary"]:
                    header.append(f"joined {room}. summary: {self.room_summary(room)}")
                else:
                    total = st.one("SELECT COUNT(*) AS n FROM messages WHERE room=?", (room,))["n"]
                    header.append(f"joined {room} ({total} msgs; latest below)")
                rows = st.q(f"SELECT * FROM messages WHERE room=? AND sender!=? AND {relevant} "
                            f"ORDER BY id DESC LIMIT ?", (room, agent, *args, self.s.join_tail))[::-1]
                st.set_cursor(agent, room, st.room_max_id(room))
                return header, rows, False
            rows = st.q(f"SELECT * FROM messages WHERE room=? AND id>? AND sender!=? AND {relevant} "
                        f"ORDER BY id LIMIT ?", (room, cur, agent, *args, limit + 1))
            more = len(rows) > limit
            rows = rows[:limit]
            st.set_cursor(agent, room, rows[-1]["id"] if more else st.room_max_id(room))
            return header, rows, more

        # All rooms: joined rooms (room-wide + addressed to me) and anywhere I'm @mentioned.
        joined_clause = "1=1" if everything else "(m.recipients='' OR instr(m.recipients, ?) > 0)"
        args = () if everything else (me_like,)
        rows = st.q(
            "SELECT m.* FROM messages m LEFT JOIN cursors c ON c.agent=? AND c.room=m.room "
            "WHERE m.sender!=? AND m.id>COALESCE(c.last_id,0) AND ("
            f"  (c.last_id IS NOT NULL AND {joined_clause}) OR instr(m.recipients, ?) > 0"
            ") ORDER BY m.id LIMIT ?",
            (agent, agent, *args, me_like, limit + 1),
        )
        more = len(rows) > limit
        rows = rows[:limit]
        if more:
            last = {}
            for r in rows:
                last[r["room"]] = r["id"]
            for rm, mid in last.items():
                st.set_cursor(agent, rm, mid)
        else:
            touched = set(st.joined_rooms(agent)) | {r["room"] for r in rows}
            for rm in touched:
                st.set_cursor(agent, rm, st.room_max_id(rm))
        return header, rows, more

    def fmt_line(self, m, agent: str, show_room: bool) -> str:
        s = f"#{m['id']}"
        if show_room:
            s += f" [{m['room']}]"
        s += f" {m['sender']}"
        rec = _recips(m)
        if rec:
            s += ">" + ",".join("you" if r == agent else r for r in rec)
        if m["reply_to"]:
            s += f" re#{m['reply_to']}"
        s += f": {m['summary']}"
        if m["body"]:
            s += f" (+{fmt_size(len(m['body']))})"
        return s

    def read(self, agent: str, room: str | None = None, everything: bool = False) -> str:
        header, rows, more = self.read_rows(agent, room, everything)
        lines = header + [self.fmt_line(m, agent, show_room=not room) for m in rows]
        if more:
            lines.append("(more: read again)")
        if not rows and not header:
            lines.append("no new messages")
        return self._charge_ctx(agent, "\n".join(lines))

    def room_summary(self, room: str) -> str:
        r = self.store.room(room)
        if r and r["summary"]:
            newer = self.store.one("SELECT COUNT(*) AS n FROM messages WHERE room=? AND id>?",
                                   (room, r["summary_upto"]))["n"]
            return r["summary"] + (f" (+{newer} newer)" if newer else "")
        rows = self.store.q("SELECT sender, summary FROM messages WHERE room=? ORDER BY id DESC LIMIT 5", (room,))
        if not rows:
            return "(empty)"
        return "recent: " + " | ".join(f"{x['sender']}: {one_line(x['summary'], 70)}" for x in rows[::-1])

    # ---------------------------------------------------------------- search
    def search(self, q: str, room: str | None = None, limit: int = 50) -> list[dict]:
        """Viewer search: newest messages whose summary or body contains q (case-insensitive for ASCII)."""
        q = " ".join((q or "").split())
        if len(q) < 2:
            raise HubError("search for at least 2 characters")
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = "(summary LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
        args: list = [like, like]
        if room:
            where += " AND room=?"
            args.append(room.strip().lower().lstrip("#"))
        rows = self.store.q(f"SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?",
                            (*args, max(1, min(int(limit), 200))))
        out = []
        for m in rows:
            j = self.message_json(m)
            body = j.pop("body")  # bodies can be huge; send a line around the match instead
            j["snippet"] = j["summary"] if not body or q.lower() in j["summary"].lower() else _snippet(body, q)
            out.append(j)
        return out

    # ------------------------------------------------------------------- get
    def get(self, agent: str, mid: int, offset: int = 0) -> str:
        m = self.store.message(int(mid))
        if not m:
            raise HubError(f"no message #{mid}")
        text = m["body"] or m["summary"]
        offset = max(0, int(offset or 0))
        chunk = text[offset: offset + self.s.get_max]
        head = f"#{m['id']} {m['sender']} in {m['room']}"
        if m["reply_to"]:
            head += f" re#{m['reply_to']}"
        if len(text) > self.s.get_max or offset:
            head += f" [chars {offset}-{offset + len(chunk)} of {len(text)}]"
        out = head + "\n" + chunk
        if offset + len(chunk) < len(text):
            out += f"\n(more: get id={m['id']} offset={offset + len(chunk)})"
        return self._charge_ctx(agent, out)

    # ----------------------------------------------------------------- rooms
    def rooms(self, agent: str) -> str:
        st = self.store
        joined = st.joined_rooms(agent)
        lines = []
        for r in st.rooms()[:50]:
            name = r["name"]
            mine = st.one("SELECT COUNT(*) AS n FROM messages WHERE room=? AND id>? AND sender!=? "
                          "AND instr(recipients, ?) > 0", (name, joined.get(name, 0), agent, f",{agent},"))["n"]
            if name in joined:
                new = st.one("SELECT COUNT(*) AS n FROM messages WHERE room=? AND id>? AND sender!=? "
                             "AND (recipients='' OR instr(recipients, ?) > 0)",
                             (name, joined[name], agent, f",{agent},"))["n"]
                status = f"{new} new" + (f", {mine} for you" if mine else "")
            else:
                status = "not joined" + (f", {mine} for you" if mine else "")
            lines.append(f"{name} ({status}): {one_line(self.room_summary(name), 100)}")
        if not lines:
            lines.append("no rooms yet (post to create one)")
        cutoff = time.time() - 600
        names = [a["name"] + ("*" if (a["last_seen"] or 0) > cutoff else "")
                 for a in st.agents() if a["kind"] == "agent" and a["name"] != agent]
        lines.append("agents: " + (", ".join(names) or "none") + "  (*=active 10m)")
        return self._charge_ctx(agent, "\n".join(lines))

    # ------------------------------------------------------------------- ask
    def budget(self, agent: str) -> int:
        row = self.store.agent(agent)
        if row and row["daily_tokens"] is not None:
            return int(row["daily_tokens"])
        return self.s.daily_tokens

    def spent(self, agent: str) -> int:
        """Tokens counted against the daily budget: gateway asks plus a worker's own CLI runs."""
        u = self.store.usage_today(agent)
        return u["ask"] + u["sub"]

    # ---------------------------------------------------------------- worker
    # An agent can live in one of three places: on the hub (a gateway model: `model` set), on a
    # machine (clients/worker.py runs a CLI there: `worker` set), or nowhere we manage (it connects
    # itself with its own key, e.g. an interactive Claude Code session).
    @staticmethod
    def worker_config(row) -> dict:
        """How the machine should run this agent. {"mode": "manual"} = not on a machine."""
        try:
            cfg = json.loads(row["worker"]) if row and row["worker"] else None
        except ValueError:
            cfg = None
        if not cfg or not cfg.get("machine"):
            return {"mode": "manual"}
        return {**cfg, "system": row["system"] or ""}  # persona, shared with hub agents

    def machine_agents(self, machine: str) -> list:
        return [a for a in self.store.agents()
                if a["kind"] == "agent" and not a["model"] and self.worker_config(a).get("machine") == machine]

    @staticmethod
    def machine_online(m) -> bool:
        return bool(m) and (m["last_seen"] or 0) > time.time() - WORKER_OFFLINE_AFTER

    def worker_state(self, row) -> str:
        """auto / manual / offline / online / busy / cooling, as the viewer and router see it."""
        if row["kind"] != "agent":
            return ""
        if row["model"]:
            m = self.model_machine(row["model"])
            return "offline" if m and not self.machine_online(self.store.machine(m)) else "auto"
        cfg = self.worker_config(row)
        if cfg["mode"] != "worker":
            return "manual"
        if not self.machine_online(self.store.machine(cfg["machine"])):
            return "offline"
        if row["status"] == "cooling":
            return "cooling" if (row["status_until"] or 0) > time.time() else "online"
        return row["status"] or "online"

    def clean_worker(self, cfg: dict | None) -> dict | None:
        """Validates a worker config from the viewer; None = not on a machine."""
        if not cfg or cfg.get("mode", "manual") == "manual":
            return None
        m = self.store.machine((cfg.get("machine") or "").strip())
        if not m:
            raise HubError("pick a machine (add one under Machines first)")
        cli = {"gemini": "agy"}.get(cfg.get("cli"), cfg.get("cli"))
        if cli not in WORKER_CLIS:
            raise HubError(f"cli must be one of {', '.join(WORKER_CLIS)}")
        found = [c["cli"] for c in json.loads(m["clis"] or "[]")]
        if found and cli not in found:
            raise HubError(f"{m['name']} doesn't have {cli} (it has {', '.join(found)})")
        power = cfg.get("power") or "answer"
        if power not in WORKER_POWERS:
            raise HubError(f"power must be one of {', '.join(WORKER_POWERS)}")
        if power != "answer" and not (cfg.get("dir") or "").strip():
            raise HubError(f"power '{power}' needs a folder")

        def num(key, default, lo, hi):
            try:
                return max(lo, min(hi, int(cfg.get(key) or default)))
            except (TypeError, ValueError):
                raise HubError(f"{key} must be a number") from None
        return {
            "mode": "worker", "machine": m["name"], "cli": cli, "power": power,
            "model": (cfg.get("model") or "").strip()[:64],
            "deep_model": (cfg.get("deep_model") or "").strip()[:64],
            "dir": (cfg.get("dir") or "").strip()[:512] if power != "answer" else "",
            "max_output": num("max_output", 800, 50, 8000),
            "max_input_chars": num("max_input_chars", 12000, 1000, 100_000),
        }

    async def refresh_model_hosts(self) -> None:
        if not self.llm.enabled or time.time() - self._hosts_at < 300:
            return
        self._hosts_at = time.time()
        try:
            self._hosts = await self.llm.model_hosts()
        except Exception:  # older LiteLLM / key without access: just don't show hosts
            self._hosts = {}

    def model_machine(self, model: str | None) -> str | None:
        """The paired machine a gateway model runs on (its api_base points there), if any."""
        host = (self._hosts.get(model or "") or "").lower()
        if not host:
            return None
        short = host if host[:1].isdigit() or ":" in host else host.split(".")[0]  # beep-boop.example.ts.net -> beep-boop
        for m in self.store.machines():
            names = {m["name"], (m["hostname"] or "").lower(), *(a.lower() for a in json.loads(m["addresses"] or "[]"))}
            if host in names or short in names:
                return m["name"]
        return None

    def model_host(self, model: str | None) -> str | None:
        """Where a gateway model runs: a paired machine's name if it's one of ours, else the raw host."""
        return self.model_machine(model) or self._hosts.get(model or "")

    def agent_machine(self, row) -> str | None:
        """The computer that actually answers for an agent: its worker's machine, or where its model runs."""
        if row["kind"] != "agent":
            return None
        if row["model"]:
            return self.model_machine(row["model"])
        return self.worker_config(row).get("machine")

    def _unavailable_notices(self, mid: int) -> None:
        """A message to an agent whose machine is off (or out of quota) gets an answer right away
        saying so, instead of silence. That reply also marks it handled: the machine skips it later."""
        m = self.store.message(mid)
        for name in _recips(m):
            row = self.store.agent(name)
            if not row or row["kind"] != "agent":
                continue
            state = self.worker_state(row)
            cfg = self.worker_config(row)
            if state == "offline":
                what = row["model"] or cfg["cli"]
                note = f"({self.agent_machine(row)} is offline, so {what} can't answer; not answered)"
            elif state == "cooling":
                until = time.strftime("%H:%M", time.localtime(row["status_until"]))
                note = f"(out of {cfg['cli']} quota on {cfg['machine']} until {until}; not answered)"
            else:
                continue
            try:
                self.post(name, m["room"], note, reply_to=mid)
            except HubError:
                pass  # rate limit / paused room: the notice is a courtesy

    def worker_report(self, agent: str, status: str, tokens: int = 0, calls: int = 0,
                      until: float | None = None, room: str | None = None) -> str:
        """A worker says what it's doing and what its CLI spent. Returns the budget state in one line."""
        if status not in WORKER_STATUSES:
            raise HubError(f"status must be one of {', '.join(WORKER_STATUSES)}")
        st = self.store
        if tokens or calls:
            st.add_usage(agent, sub=max(0, int(tokens)), calls=max(0, int(calls)))
        st.set_status(agent, status, until if status == "cooling" else None)
        if room:  # viewer shows "thinking…" while the CLI runs
            self.broadcast({"type": "typing", "agent": agent, "room": room, **({} if status == "busy" else {"done": True})})
        self.broadcast({"type": "agents"})
        budget, used = self.budget(agent), self.spent(agent)
        if budget and used >= budget:
            return f"over budget {used}/{budget}"
        return f"ok {used}/{budget or 'unlimited'}"

    async def ask(self, agent: str, model: str, prompt: str | None = None, system: str | None = None,
                  max_tokens: int | None = None, room: str | None = None) -> str:
        if not self.llm.enabled:
            raise HubError("no model gateway configured (LITELLM_URL)")
        model = (model or "").strip()
        try:
            if model in ("", "?"):
                return self._charge_ctx(agent, "models: " + ", ".join(await self.llm.models()))
            if not prompt:
                raise HubError("prompt required")
            budget = self.budget(agent)
            used = self.spent(agent)
            if budget and used >= budget:
                raise HubError(f"daily token budget used ({used}/{budget}); resets 00:00 UTC")
            mt = max(1, min(int(max_tokens or self.s.ask_max_tokens), self.s.ask_max_tokens))
            msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
            text, tokens = await self.llm.chat(model, msgs, mt, user=agent)
        except LLMError as e:
            raise HubError(str(e)) from e
        self.store.add_usage(agent, ask=tokens)
        used += tokens
        note = f"\n[budget {used}/{budget} today]" if budget and used > 0.8 * budget else ""
        if room:  # put the answer in a room for visibility; hand back only the short line
            res = self.post(agent, room, text or "(empty answer)")
            mid = int(res.split("#")[1])
            return self._charge_ctx(agent, f"posted {self.fmt_line(self.store.message(mid), agent, True)}{note}")
        return self._charge_ctx(agent, (text or "(empty answer)") + note)
