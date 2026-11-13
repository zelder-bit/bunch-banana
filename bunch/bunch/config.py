"""Settings, all read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace


POSITIVE = ("BUNCH_READ_LIMIT", "BUNCH_GET_MAX", "BUNCH_SUMMARY_MAX", "BUNCH_MAX_FILE")  # zero or less makes no sense


def _num(name: str, default, kind):
    """A number from the environment; a bad value stops start-up with one clear line, not a traceback."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = kind(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, not '{raw}'") from None
    if n < 0 or (n == 0 and name in POSITIVE):
        raise SystemExit(f"{name} must be {'at least 1' if name in POSITIVE else '0 or more'}, not {raw}")
    return n


def _int(name: str, default: int) -> int:
    return _num(name, default, int)


def _float(name: str, default: float) -> float:
    return _num(name, default, float)


@dataclass(frozen=True)
class Settings:
    db_path: str = "/data/bunch.db"
    admin_token: str = ""          # web viewer + admin API; required
    human_name: str = "human"      # name you post as from the web viewer
    litellm_url: str = ""          # e.g. http://litellm:4000 ; blank disables ask/summaries
    litellm_key: str = ""
    model_probe: bool = True      # hub and gateway may have different network access
    summary_model: str = ""        # cheap model for rolling summaries; blank = no LLM summaries
    router_model: str = ""         # cheap model that addresses un-@'d human messages; blank = off
    ask_timeout: float = 180.0     # seconds one model call may take before the hub gives up
    router_timeout: float = 8.0    # seconds the router model gets before the message goes room-wide
    session_secret: str = ""       # signs login cookies; blank = a random one kept in the database
    secure_cookies: bool = False   # login cookie only sent over HTTPS (set when behind TLS)
    summary_max: int = 160         # max chars of a one-line message summary
    summary_every: int = 6         # re-summarize a room after this many new messages
    max_body: int = 200_000        # hard cap on a message body (chars)
    get_max: int = 4_000           # chars returned per get() page
    read_limit: int = 30           # max lines returned by one read()
    join_tail: int = 5             # messages shown when first joining a room
    max_chain: int = 20            # agent-only messages in a row before a human must speak (0 = off)
    posts_per_min: int = 20        # per-agent post rate limit (0 = off)
    daily_tokens: int = 200_000    # default per-agent daily ask() budget (0 = unlimited)
    max_file: int = 10_000_000     # largest single upload (bytes)
    max_request: int = 1_000_000   # largest request body on any other route (bytes); never below 4 x max_body
    max_files_total: int = 2_000_000_000  # all attachments together (bytes); uploads past it get 413 (0 = unlimited)
    max_folder: int = 500_000_000  # one room's shared folder, history included (bytes; 0 = unlimited)
    ask_max_tokens: int = 1_024    # default max_tokens for ask()
    ask_max_tokens_limit: int = 8_192  # caller ceiling; never below the default
    think_max_tokens: int = 4_096  # retry budget when a thinking model spends max_tokens on reasoning (0 = off)

    def __post_init__(self):
        object.__setattr__(self, "ask_max_tokens_limit", max(self.ask_max_tokens, self.ask_max_tokens_limit))


def load(**overrides) -> Settings:
    env = os.environ.get
    s = Settings(
        db_path=env("BUNCH_DB", "/data/bunch.db"),
        admin_token=env("BUNCH_ADMIN_TOKEN", "").strip(),
        human_name=env("BUNCH_HUMAN_NAME", "human").strip().lower() or "human",
        litellm_url=env("LITELLM_URL", "").strip().rstrip("/"),
        litellm_key=env("LITELLM_KEY", "").strip(),
        model_probe=env("BUNCH_MODEL_PROBE", "1").strip().lower() not in ("0", "false", "no"),
        summary_model=env("BUNCH_SUMMARY_MODEL", "").strip(),
        router_model=env("BUNCH_ROUTER_MODEL", "").strip(),
        ask_timeout=_float("BUNCH_ASK_TIMEOUT", 180.0),
        router_timeout=_float("BUNCH_ROUTER_TIMEOUT", 8.0),
        session_secret=env("BUNCH_SESSION_SECRET", "").strip(),
        secure_cookies=env("BUNCH_SECURE_COOKIES", "").strip().lower() in ("1", "true", "yes"),
        summary_max=_int("BUNCH_SUMMARY_MAX", 160),
        summary_every=_int("BUNCH_SUMMARY_EVERY", 6),
        max_body=_int("BUNCH_MAX_BODY", 200_000),
        get_max=_int("BUNCH_GET_MAX", 4_000),
        read_limit=_int("BUNCH_READ_LIMIT", 30),
        join_tail=_int("BUNCH_JOIN_TAIL", 5),
        max_chain=_int("BUNCH_MAX_CHAIN", 20),
        posts_per_min=_int("BUNCH_POSTS_PER_MIN", 20),
        daily_tokens=_int("BUNCH_DAILY_TOKENS", 200_000),
        max_file=_int("BUNCH_MAX_FILE", 10_000_000),
        max_request=_int("BUNCH_MAX_REQUEST", 1_000_000),
        max_files_total=_int("BUNCH_MAX_FILES_TOTAL", 2_000_000_000),
        max_folder=_int("BUNCH_MAX_FOLDER", 500_000_000),
        ask_max_tokens=_int("BUNCH_ASK_MAX_TOKENS", 1_024),
        ask_max_tokens_limit=_int("BUNCH_ASK_MAX_TOKENS_LIMIT", 8_192),
        think_max_tokens=_int("BUNCH_THINK_MAX_TOKENS", 4_096),
    )
    return replace(s, **overrides) if overrides else s
