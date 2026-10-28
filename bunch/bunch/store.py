"""SQLite storage. One file, no server. Safe for a single hub process."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  name TEXT PRIMARY KEY,
  key_hash TEXT UNIQUE,
  kind TEXT NOT NULL DEFAULT 'agent',      -- 'agent' or 'human'
  daily_tokens INTEGER,                     -- NULL = use hub default
  created REAL NOT NULL,
  last_seen REAL,
  description TEXT NOT NULL DEFAULT '',     -- what this agent is good at (used by the router)
  model TEXT,                               -- set = the hub answers for this agent (auto agent)
  system TEXT,                              -- persona prompt for auto agents
  pw_hash TEXT,                             -- humans: argon2 password hash
  is_admin INTEGER NOT NULL DEFAULT 0,      -- humans: may manage agents and users
  worker TEXT,                              -- JSON: how clients/worker.py runs this agent (NULL = manual)
  status TEXT,                              -- worker-reported: online / busy / cooling
  status_until REAL                         -- cooling: when the worker expects to answer again
);
CREATE TABLE IF NOT EXISTS rooms(
  name TEXT PRIMARY KEY,
  summary TEXT NOT NULL DEFAULT '',
  summary_upto INTEGER NOT NULL DEFAULT 0,  -- last message id folded into summary
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room TEXT NOT NULL,
  sender TEXT NOT NULL,
  human INTEGER NOT NULL DEFAULT 0,
  recipients TEXT NOT NULL DEFAULT '',      -- ',a,b,' or '' for whole room
  summary TEXT NOT NULL,
  body TEXT,                                -- NULL when summary is the whole message
  reply_to INTEGER,
  auto_sum INTEGER NOT NULL DEFAULT 0,      -- 1 = summary is a truncation; summarizer may improve it
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_room ON messages(room, id);
CREATE INDEX IF NOT EXISTS messages_sender ON messages(sender, created);
CREATE TABLE IF NOT EXISTS cursors(
  agent TEXT NOT NULL, room TEXT NOT NULL, last_id INTEGER NOT NULL,
  PRIMARY KEY(agent, room)
);
CREATE TABLE IF NOT EXISTS usage(
  agent TEXT NOT NULL, day TEXT NOT NULL,
  ask_tokens INTEGER NOT NULL DEFAULT 0,    -- model tokens spent through ask()
  ctx_tokens INTEGER NOT NULL DEFAULT 0,    -- estimated tokens the hub put into the agent's context
  sub_tokens INTEGER NOT NULL DEFAULT 0,    -- tokens a worker spent on its own subscription CLI
  sub_calls INTEGER NOT NULL DEFAULT 0,     -- CLI runs by that worker
  PRIMARY KEY(agent, day)
);
"""

# Columns added after the first release; ALTERed into older databases on startup.
AGENT_COLUMNS = {
    "description": "TEXT NOT NULL DEFAULT ''",
    "model": "TEXT",
    "system": "TEXT",
    "pw_hash": "TEXT",
    "is_admin": "INTEGER NOT NULL DEFAULT 0",
    "worker": "TEXT",
    "status": "TEXT",
    "status_until": "REAL",
}
USAGE_COLUMNS = {
    "sub_tokens": "INTEGER NOT NULL DEFAULT 0",
    "sub_calls": "INTEGER NOT NULL DEFAULT 0",
}

_ph = PasswordHasher()


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            d = os.path.dirname(os.path.abspath(path))
            os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.executescript(SCHEMA)
            have = {r["name"] for r in self.db.execute("PRAGMA table_info(agents)")}
            for col, decl in AGENT_COLUMNS.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE agents ADD COLUMN {col} {decl}")
            have = {r["name"] for r in self.db.execute("PRAGMA table_info(usage)")}
            for col, decl in USAGE_COLUMNS.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE usage ADD COLUMN {col} {decl}")

    def backup(self, path: str) -> None:
        """Copies the database to path using SQLite's online backup, so it's safe while the hub runs."""
        tmp = path + ".tmp"
        dst = sqlite3.connect(tmp)
        try:
            with self.lock:
                self.db.backup(dst)
        finally:
            dst.close()
        os.replace(tmp, path)  # never leave a half-written file at path

    # -- low level -------------------------------------------------------
    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def x(self, sql: str, args=()) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, args)

    # -- agents ----------------------------------------------------------
    def _check_new_name(self, name: str) -> None:
        if not valid_name(name):
            raise ValueError("name must be lowercase letters, digits, _ . - (max 32)")
        if self.agent(name):
            raise ValueError(f"'{name}' already exists")

    def create_agent(self, name: str, daily_tokens: int | None = None, description: str = "",
                     model: str | None = None, system: str | None = None) -> str:
        self._check_new_name(name)
        key = "bunch_" + secrets.token_urlsafe(24)
        self.x(
            "INSERT INTO agents(name,key_hash,kind,daily_tokens,created,description,model,system) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (name, hash_key(key), "agent", daily_tokens, time.time(), description or "",
             model or None, system or None),
        )
        return key

    def set_agent_info(self, name: str, **fields) -> None:
        """Update description / model / system for an agent. Blank model/system = NULL."""
        for col in ("description", "model", "system"):
            if col in fields:
                val = (fields[col] or "").strip()
                if col != "description":
                    val = val or None
                self.x(f"UPDATE agents SET {col}=? WHERE name=?", (val, name))

    def set_worker(self, name: str, config: dict | None) -> None:
        """config None = manual again (clients/worker.py idles)."""
        self.x("UPDATE agents SET worker=? WHERE name=?", (json.dumps(config) if config else None, name))

    def set_status(self, name: str, status: str | None, until: float | None = None) -> None:
        self.x("UPDATE agents SET status=?, status_until=? WHERE name=?", (status, until, name))

    def auto_agents(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM agents WHERE kind='agent' AND model IS NOT NULL AND model!='' ORDER BY name")

    # -- human users -----------------------------------------------------
    def create_user(self, name: str, password: str, admin: bool = False) -> None:
        self._check_new_name(name)
        if len(password or "") < 8:
            raise ValueError("password must be at least 8 characters")
        self.x("INSERT INTO agents(name,key_hash,kind,created,pw_hash,is_admin) VALUES(?,NULL,'human',?,?,?)",
               (name, time.time(), _ph.hash(password), int(admin)))

    def set_password(self, name: str, password: str) -> None:
        row = self.agent(name)
        if not row or row["kind"] != "human":
            raise ValueError("no such user")
        if len(password or "") < 8:
            raise ValueError("password must be at least 8 characters")
        self.x("UPDATE agents SET pw_hash=? WHERE name=?", (_ph.hash(password), name))

    def verify_user(self, name: str, password: str):
        """Returns the user row if the password matches, else None."""
        row = self.agent((name or "").strip().lower())
        if not row or row["kind"] != "human" or not row["pw_hash"]:
            _ph.hash("timing-equaliser")  # don't reveal which names exist by response time
            return None
        try:
            _ph.verify(row["pw_hash"], password or "")
        except (VerificationError, InvalidHashError):
            return None
        if _ph.check_needs_rehash(row["pw_hash"]):
            self.x("UPDATE agents SET pw_hash=? WHERE name=?", (_ph.hash(password), row["name"]))
        self.x("UPDATE agents SET last_seen=? WHERE name=?", (time.time(), row["name"]))
        return row

    def users(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM agents WHERE kind='human' AND pw_hash IS NOT NULL ORDER BY name")

    def _check_not_last_admin(self, name: str) -> None:
        row = self.agent(name)
        if row and row["is_admin"]:
            n = self.one("SELECT COUNT(*) AS n FROM agents WHERE kind='human' AND pw_hash IS NOT NULL AND is_admin=1")["n"]
            if n <= 1:
                raise ValueError("can't remove the last admin")

    def set_admin(self, name: str, admin: bool, keep_one: bool = True) -> None:
        row = self.agent(name)
        if not row or row["kind"] != "human" or not row["pw_hash"]:
            raise LookupError("no such user")
        if not admin and keep_one:
            self._check_not_last_admin(name)
        self.x("UPDATE agents SET is_admin=? WHERE name=?", (int(admin), name))

    def delete_user(self, name: str, keep_one: bool = False) -> bool:
        if keep_one:
            self._check_not_last_admin(name)
        cur = self.x("DELETE FROM agents WHERE name=? AND kind='human' AND pw_hash IS NOT NULL", (name,))
        self.x("DELETE FROM cursors WHERE agent=?", (name,))
        return cur.rowcount > 0

    def ensure_human(self, name: str) -> None:
        self.x(
            "INSERT OR IGNORE INTO agents(name,key_hash,kind,created) VALUES(?,NULL,'human',?)",
            (name, time.time()),
        )

    def rotate_key(self, name: str) -> str:
        row = self.agent(name)
        if not row or row["kind"] != "agent":
            raise ValueError("no such agent")
        key = "bunch_" + secrets.token_urlsafe(24)
        self.x("UPDATE agents SET key_hash=? WHERE name=?", (hash_key(key), name))
        return key

    def set_budget(self, name: str, daily_tokens: int | None) -> None:
        self.x("UPDATE agents SET daily_tokens=? WHERE name=?", (daily_tokens, name))

    def delete_agent(self, name: str) -> bool:
        cur = self.x("DELETE FROM agents WHERE name=? AND kind='agent'", (name,))
        self.x("DELETE FROM cursors WHERE agent=?", (name,))
        return cur.rowcount > 0

    def agent(self, name: str):
        return self.one("SELECT * FROM agents WHERE name=?", (name,))

    def agent_by_key(self, key: str):
        if not key:
            return None
        row = self.one("SELECT * FROM agents WHERE key_hash=? AND kind='agent'", (hash_key(key),))
        if row:
            self.x("UPDATE agents SET last_seen=? WHERE name=?", (time.time(), row["name"]))
        return row

    def agents(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM agents ORDER BY name")

    def agent_names(self) -> set[str]:
        return {r["name"] for r in self.q("SELECT name FROM agents")}

    # -- usage -----------------------------------------------------------
    def add_usage(self, agent: str, ask: int = 0, ctx: int = 0, sub: int = 0, calls: int = 0) -> None:
        self.x(
            "INSERT INTO usage(agent,day,ask_tokens,ctx_tokens,sub_tokens,sub_calls) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(agent,day) DO UPDATE SET ask_tokens=ask_tokens+excluded.ask_tokens, "
            "ctx_tokens=ctx_tokens+excluded.ctx_tokens, sub_tokens=sub_tokens+excluded.sub_tokens, "
            "sub_calls=sub_calls+excluded.sub_calls",
            (agent, today(), ask, ctx, sub, calls),
        )

    def usage_today(self, agent: str) -> dict:
        row = self.one("SELECT ask_tokens, ctx_tokens, sub_tokens, sub_calls FROM usage WHERE agent=? AND day=?",
                       (agent, today()))
        if not row:
            return {"ask": 0, "ctx": 0, "sub": 0, "calls": 0}
        return {"ask": row["ask_tokens"], "ctx": row["ctx_tokens"], "sub": row["sub_tokens"], "calls": row["sub_calls"]}

    # -- rooms & messages ------------------------------------------------
    def ensure_room(self, room: str) -> None:
        self.x("INSERT OR IGNORE INTO rooms(name,created) VALUES(?,?)", (room, time.time()))

    def room(self, room: str):
        return self.one("SELECT * FROM rooms WHERE name=?", (room,))

    def rooms(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM rooms ORDER BY name")

    def delete_room(self, room: str) -> int | None:
        """Removes a room with all its messages and everyone's bookmarks in it.
        Returns how many messages went, or None if there was no such room."""
        if not self.room(room):
            return None
        with self.lock:  # all or nothing
            self.db.execute("BEGIN")
            try:
                n = self.db.execute("DELETE FROM messages WHERE room=?", (room,)).rowcount
                self.db.execute("DELETE FROM cursors WHERE room=?", (room,))
                self.db.execute("DELETE FROM rooms WHERE name=?", (room,))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return n

    def room_max_id(self, room: str) -> int:
        row = self.one("SELECT MAX(id) AS m FROM messages WHERE room=?", (room,))
        return row["m"] or 0

    def add_message(self, room, sender, human, recipients, summary, body, reply_to, auto_sum) -> int:
        rec = ("," + ",".join(recipients) + ",") if recipients else ""
        cur = self.x(
            "INSERT INTO messages(room,sender,human,recipients,summary,body,reply_to,auto_sum,created) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (room, sender, int(human), rec, summary, body, reply_to, int(auto_sum), time.time()),
        )
        return cur.lastrowid

    def delete_message(self, mid: int):
        """Removes a message for good. Returns the deleted row, or None."""
        row = self.message(mid)
        if row:
            self.x("DELETE FROM messages WHERE id=?", (mid,))
            # the rolling summary may quote it: start the summary over
            self.x("UPDATE rooms SET summary='', summary_upto=0 WHERE name=? AND summary_upto>=?", (row["room"], mid))
        return row

    def message(self, mid: int):
        return self.one("SELECT * FROM messages WHERE id=?", (mid,))

    def cursor(self, agent: str, room: str) -> int | None:
        row = self.one("SELECT last_id FROM cursors WHERE agent=? AND room=?", (agent, room))
        return row["last_id"] if row else None

    def set_cursor(self, agent: str, room: str, last_id: int) -> None:
        self.x(
            "INSERT INTO cursors(agent,room,last_id) VALUES(?,?,?) "
            "ON CONFLICT(agent,room) DO UPDATE SET last_id=MAX(last_id, excluded.last_id)",
            (agent, room, last_id),
        )

    def joined_rooms(self, agent: str) -> dict[str, int]:
        return {r["room"]: r["last_id"] for r in self.q("SELECT room,last_id FROM cursors WHERE agent=?", (agent,))}
