"""The viewer's WebSocket (#67): access is re-checked while it is open, and a viewer that falls behind is told."""
import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

import bunch.app as appmod

from .test_bunch import ADMIN, adm, env, h  # noqa: F401  (env is a fixture)


def _until_closed(ws):
    with pytest.raises(WebSocketDisconnect) as e:
        for _ in range(50):
            ws.receive_json()
    return e.value.code


def test_broadcast_marks_a_full_queue_as_lagged(env):
    c, k, app, llm = env
    hub, q = app.state.hub, asyncio.Queue(maxsize=2)
    hub.listeners.add(q)
    for n in range(3):
        hub.broadcast({"type": "x", "n": n})
    assert q.qsize() == 1 and q.get_nowait() == {"type": "lagged"}
    hub.listeners.discard(q)


def test_lagging_viewer_is_closed_so_it_catches_up(env):
    c, k, app, llm = env
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"token": ADMIN})
        assert ws.receive_json()["type"] == "ready"
        c.portal.call(app.state.hub.broadcast, {"type": "lagged"})
        assert _until_closed(ws) == 4408
    assert not app.state.hub.listeners


def test_open_socket_ends_with_the_session(env, monkeypatch):
    c, k, app, llm = env
    monkeypatch.setattr(appmod, "WS_PING", 0.05)
    assert c.post("/api/admin/users", json={"name": "pat", "password": "correct horse"}, headers=adm()).status_code == 200
    assert c.post("/api/auth/login", json={"name": "pat", "password": "correct horse"}).status_code == 200
    with c.websocket_connect("/ws") as ws:
        ws.send_json({})
        assert ws.receive_json()["type"] == "ready"
        assert ws.receive_json()["type"] == "ping"  # still signed in: stays open
        assert c.delete("/api/admin/users/pat", headers=adm()).status_code == 200
        assert _until_closed(ws) == 4401


def test_token_socket_stays_open_across_checks(env, monkeypatch):
    c, k, app, llm = env
    monkeypatch.setattr(appmod, "WS_PING", 0.05)
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"token": ADMIN})
        assert [ws.receive_json()["type"] for _ in range(4)] == ["ready", "ping", "ping", "ping"]
        c.post("/api/v1/post", json={"room": "live", "text": "hi"}, headers=h(k["alice"]))
        assert ws.receive_json()["type"] == "message"


def test_closing_the_socket_frees_its_listener_at_once(env):
    c, k, app, llm = env
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"token": ADMIN})
        assert ws.receive_json()["type"] == "ready"
        assert len(app.state.hub.listeners) == 1
    for _ in range(50):
        if not app.state.hub.listeners:
            break
        c.get("/healthz")
    assert not app.state.hub.listeners
