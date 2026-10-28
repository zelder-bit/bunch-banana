"""Admin commands run on the bunch server, straight against the database.

  docker compose exec bunch python -m bunch.cli add-agent coder [--daily 100000] [--desc "..."]
  docker compose exec bunch python -m bunch.cli add-agent helper --model local/general [--system "..."]
  docker compose exec -it bunch python -m bunch.cli add-user alice [--admin]     (prompts for a password)
  docker compose exec -it bunch python -m bunch.cli passwd alice
  docker compose exec bunch python -m bunch.cli describe coder "reviews Python code"
  docker compose exec bunch python -m bunch.cli list
  docker compose exec bunch python -m bunch.cli rotate coder
  docker compose exec bunch python -m bunch.cli budget coder 50000     (0 = unlimited, 'default' = server default)
  docker compose exec bunch python -m bunch.cli remove coder
  docker compose exec bunch python -m bunch.cli backup /data/backup.db  (safe while the hub runs)
      attachments are copied to /data/backup.db.files/ (restore it as /data/files/)
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from .config import load
from .store import Store


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bunch.cli", description="bunch admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-agent", help="create an agent and print its key once")
    a.add_argument("name")
    a.add_argument("--daily", type=int, default=None, help="daily ask() token budget")
    a.add_argument("--desc", default="", help="what it's good at (helps the router pick it)")
    a.add_argument("--model", default=None, help="gateway model: the hub answers for this agent itself")
    a.add_argument("--system", default=None, help="persona prompt for --model agents")
    u = sub.add_parser("add-user", help="create a human login for the web viewer")
    u.add_argument("name")
    u.add_argument("--admin", action="store_true", help="may manage agents and users")
    pw = sub.add_parser("passwd", help="set a user's password")
    pw.add_argument("name")
    ds = sub.add_parser("describe", help="set an agent's description, used by the router")
    ds.add_argument("name")
    ds.add_argument("text")
    sub.add_parser("list", help="list agents and today's usage")
    r = sub.add_parser("rotate", help="issue a new key (old one stops working)")
    r.add_argument("name")
    b = sub.add_parser("budget", help="set daily ask() budget")
    b.add_argument("name")
    b.add_argument("tokens")
    d = sub.add_parser("remove", help="delete an agent")
    d.add_argument("name")
    bk = sub.add_parser("backup", help="copy the database to a file (safe while the hub runs)")
    bk.add_argument("path")
    args = p.parse_args(argv)

    s = load()
    st = Store(s.db_path)
    try:
        if args.cmd == "add-agent":
            key = st.create_agent(args.name.lower(), args.daily, args.desc, args.model, args.system)
            print(f"agent: {args.name.lower()}\nkey:   {key}\n(shown once; store it on the agent's machine)")
        elif args.cmd in ("add-user", "passwd"):
            password = getpass.getpass("password: ")
            if password != getpass.getpass("again:    "):
                raise ValueError("passwords don't match")
            if args.cmd == "add-user":
                st.create_user(args.name.lower(), password, args.admin)
                print(f"user {args.name.lower()} created" + (" (admin)" if args.admin else ""))
            else:
                st.set_password(args.name.lower(), password)
                print("ok")
        elif args.cmd == "describe":
            if not st.agent(args.name):
                raise ValueError("no such agent")
            st.set_agent_info(args.name, description=args.text)
            print("ok")
        elif args.cmd == "list":
            print(f"{'name':<20} {'kind':<6} {'budget':>9} {'ask today':>10} {'ctx today':>10}  model")
            for row in st.agents():
                u = st.usage_today(row["name"])
                bud = row["daily_tokens"] if row["daily_tokens"] is not None else s.daily_tokens
                print(f"{row['name']:<20} {row['kind']:<6} {bud if row['kind'] == 'agent' else '-':>9} "
                      f"{u['ask']:>10} {u['ctx']:>10}  {row['model'] or ''}")
        elif args.cmd == "rotate":
            print(f"new key: {st.rotate_key(args.name)}")
        elif args.cmd == "budget":
            val = None if args.tokens == "default" else int(args.tokens)
            st.set_budget(args.name, val)
            print("ok")
        elif args.cmd == "remove":
            print("removed" if st.delete_agent(args.name) else "no such agent")
        elif args.cmd == "backup":
            if os.path.abspath(args.path) == os.path.abspath(s.db_path):
                raise ValueError("backup path is the live database")
            st.backup(args.path)
            print(f"backed up to {args.path}")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
