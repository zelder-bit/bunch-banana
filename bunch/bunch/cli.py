"""Admin commands run on the bunch server, straight against the database.

  docker compose exec bunch python -m bunch.cli add-agent coder [--daily 100000]
  docker compose exec bunch python -m bunch.cli list
  docker compose exec bunch python -m bunch.cli rotate coder
  docker compose exec bunch python -m bunch.cli budget coder 50000     (0 = unlimited, 'default' = server default)
  docker compose exec bunch python -m bunch.cli remove coder
"""
from __future__ import annotations

import argparse
import sys

from .config import load
from .store import Store


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bunch.cli", description="bunch admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-agent", help="create an agent and print its key once")
    a.add_argument("name")
    a.add_argument("--daily", type=int, default=None, help="daily ask() token budget")
    sub.add_parser("list", help="list agents and today's usage")
    r = sub.add_parser("rotate", help="issue a new key (old one stops working)")
    r.add_argument("name")
    b = sub.add_parser("budget", help="set daily ask() budget")
    b.add_argument("name")
    b.add_argument("tokens")
    d = sub.add_parser("remove", help="delete an agent")
    d.add_argument("name")
    args = p.parse_args(argv)

    s = load()
    st = Store(s.db_path)
    try:
        if args.cmd == "add-agent":
            key = st.create_agent(args.name.lower(), args.daily)
            print(f"agent: {args.name.lower()}\nkey:   {key}\n(shown once; store it on the agent's machine)")
        elif args.cmd == "list":
            print(f"{'name':<20} {'kind':<6} {'budget':>9} {'ask today':>10} {'ctx today':>10}")
            for row in st.agents():
                u = st.usage_today(row["name"])
                bud = row["daily_tokens"] if row["daily_tokens"] is not None else s.daily_tokens
                print(f"{row['name']:<20} {row['kind']:<6} {bud if row['kind'] == 'agent' else '-':>9} "
                      f"{u['ask']:>10} {u['ctx']:>10}")
        elif args.cmd == "rotate":
            print(f"new key: {st.rotate_key(args.name)}")
        elif args.cmd == "budget":
            val = None if args.tokens == "default" else int(args.tokens)
            st.set_budget(args.name, val)
            print("ok")
        elif args.cmd == "remove":
            print("removed" if st.delete_agent(args.name) else "no such agent")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
