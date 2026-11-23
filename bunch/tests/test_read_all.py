"""B1: the `read all` position, reused agent names, storage settings."""
import sqlite3

import pytest

from bunch.config import Settings
from bunch.hub import Hub, post_id
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
    skipped = post_id(hub.post("sam", "r", "@bob for bob"))
    shown = post_id(hub.post("sam", "r", "for everyone"))
    assert ids(hub.read_rows("alice", "r")[1]) == [shown]  # default read unchanged
    assert ids(hub.read_rows("alice", "r", True)[1]) == [skipped]  # not the one already delivered
    assert hub.read_rows("alice", "r", True)[1] == []  # the all position moved
    again = post_id(hub.post("sam", "r", "@bob again"))
    assert ids(hub.read_rows("alice", None, True)[1]) == [again]  # all-rooms form shares the position


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


def test_reused_name_is_not_offered_old_jobs(tmp_path):
    st, hub = mk(tmp_path)
    st.ensure_room("r")
    st.create_agent("old")
    hub.post("sam", "r", "@old do this", human=True)
    assert len(hub.jobs("old")) == 1
    st.delete_agent("old")
    st.create_agent("old")
    assert hub.jobs("old") == []


def test_reused_name_as_person_and_in_overview_and_lines(tmp_path):
    st, hub = mk(tmp_path)
    st.ensure_room("r")
    st.create_agent("old")
    old = post_id(hub.post("sam", "r", "@old secret"))
    st.delete_agent("old")
    st.ensure_human("old")  # a person takes the name (ensure_human)
    assert hub.read_rows("old", "r")[1] == []
    assert [r for r in hub.room_overview("old") if r["name"] == "r"][0]["mine"] == 0
    assert hub.fmt_line(st.message(old), "old", True).startswith(f"#{old} [r] sam>old")  # not "you"
    new = post_id(hub.post("sam", "r", "@old hi"))
    assert ids(hub.read_rows("old", "r")[1]) == [new]
    assert [r for r in hub.room_overview("old") if r["name"] == "r"][0]["mine"] == 0  # read now
    assert hub.fmt_line(st.message(new), "old", True).startswith(f"#{new} [r] sam>you")
    st.create_agent("old2")
    hub.post("sam", "r", "@old2 secret")
    st.delete_agent("old2")
    st.create_user("old2", "password123")  # create_user stamps it too
    assert hub.read_rows("old2", "r")[1] == []
