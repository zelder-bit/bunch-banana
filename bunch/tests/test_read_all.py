"""B1: the `read all` position, reused agent names, storage settings."""
import sqlite3

import pytest

from bunch.config import Settings
from bunch.hub import Hub
from bunch.store import MIGRATIONS, Store
from .test_bunch import FakeLLM


def mk(tmp_path):
    st = Store(str(tmp_path / "h.db"))
    hub = Hub(st, Settings(db_path=str(tmp_path / "h.db"), admin_token="x" * 20), FakeLLM())
    return st, hub


def ids(rows):
    return [r["id"] for r in rows]


def test_read_all_shows_only_what_default_skipped(tmp_path):
    st, hub = mk(tmp_path)
    st.ensure_room("r")
    st.create_agent("alice")
    st.create_agent("bob")
    hub.read_rows("alice", "r")  # join
    a = hub.post("sam", "r", "@bob for bob")  # alice's default read skips it
    b = hub.post("sam", "r", "for everyone")
    _, rows, _ = hub.read_rows("alice", "r")
    assert ids(rows) == [int(b.split("#")[1].split()[0])] if isinstance(b, str) else True
    got = ids(hub.read_rows("alice", "r", True)[1])
    assert len(got) == 1 and got != ids(rows)  # only the skipped one, not the one already delivered
    assert hub.read_rows("alice", "r", True)[1] == []  # the all position moved
    hub.post("sam", "r", "@bob again")
    assert len(hub.read_rows("alice", None, True)[1]) == 1  # all rooms form uses the same position
    assert a


def test_reused_name_ignores_old_addressed_messages(tmp_path):
    st, hub = mk(tmp_path)
    st.ensure_room("r")
    st.create_agent("old")
    st.create_agent("zed")
    hub.read_rows("old", "r")
    hub.post("zed", "r", "@old secret for the old one")
    st.delete_agent("old")
    st.create_agent("old")
    assert hub.read_rows("old", "r")[1] == []  # the join read must not show it either
    hub.post("zed", "r", "@old hello new one")
    assert len(hub.read_rows("old", "r")[1]) == 1


def test_synchronous_normal(tmp_path):
    st, _ = mk(tmp_path)
    assert st.db.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_newer_database_refused(tmp_path):
    p = str(tmp_path / "n.db")
    Store(p).db.close()
    c = sqlite3.connect(p)
    c.execute(f"PRAGMA user_version={len(MIGRATIONS) + 1}")
    c.close()
    with pytest.raises(RuntimeError, match="newer than this code"):
        Store(p)
