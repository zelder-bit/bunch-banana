"""The small clients in clients/: network and model errors must not kill them or leave people waiting (#70)."""
import http.client
import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest


def _load(name, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    path = Path(__file__).resolve().parents[2] / "clients" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"bunch_client_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("exc", [TimeoutError("timed out"), ConnectionResetError("reset"),
                                 http.client.RemoteDisconnected("closed"), http.client.IncompleteRead(b"")])
def test_bridge_answers_with_an_error_instead_of_dying(monkeypatch, exc):
    b = _load("mcp_stdio_bridge", monkeypatch, BUNCH_KEY="k")

    def boom(*a, **kw):
        raise exc
    monkeypatch.setattr(b.urllib.request, "urlopen", boom)
    out = json.loads(b.forward(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call"})))
    assert out["id"] == 7 and out["error"]["message"].startswith("bunch unreachable")
    assert b.forward(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})) == ""  # no id: nothing to answer


def test_bunchctl_reports_a_dropped_connection_plainly(monkeypatch):
    ctl = _load("bunchctl", monkeypatch, BUNCH_KEY="k")

    def boom(*a, **kw):
        raise ConnectionResetError("reset by peer")
    monkeypatch.setattr(ctl.urllib.request, "urlopen", boom)
    with pytest.raises(SystemExit) as e:
        ctl.call("GET", "/api/v1/rooms")
    assert "cannot reach bunch" in str(e.value)


def _responder(monkeypatch, ask):
    r = _load("responder", monkeypatch, BUNCH_KEY="k", RESPONDER_MODEL="local/x")
    posted = []

    def req(method, path, body=None, params=None, timeout=120):
        if path == "/api/v1/ask":
            return ask()
        if path == "/api/v1/post":
            posted.append(body)
            return "ok #2"
        return ""
    monkeypatch.setattr(r, "req", req)
    return r, posted


MSG = {"id": 1, "room": "r", "sender": "sam", "human": True, "size": 0, "summary": "hello?", "wake": True}


def test_responder_tells_the_room_when_the_model_fails(monkeypatch):
    def over_budget():
        raise urllib.error.HTTPError("u", 400, "Bad Request", {}, __import__("io").BytesIO(b"error: daily token budget used"))
    r, posted = _responder(monkeypatch, over_budget)
    r.answer(MSG, "helper")
    assert len(posted) == 1 and posted[0]["re"] == 1
    assert posted[0]["text"] == "(helper didn't answer: daily token budget used; ask again)"

    r, posted = _responder(monkeypatch, lambda: "error: no such model")
    r.answer(MSG, "helper")
    assert posted[0]["text"] == "(helper didn't answer: no such model; ask again)"


def test_responder_posts_a_normal_answer(monkeypatch):
    r, posted = _responder(monkeypatch, lambda: "hi sam")
    r.answer(MSG, "helper")
    assert posted == [{"room": "r", "text": "hi sam", "re": 1}]


def test_responder_waits_for_the_hub_at_start(monkeypatch):
    r, _ = _responder(monkeypatch, lambda: "")
    calls = []

    class Stop(Exception):
        pass

    def req(method, path, body=None, params=None, timeout=120):
        calls.append(path)
        if len(calls) == 1:
            raise urllib.error.URLError("connection refused")
        if path == "/api/v1/whoami":
            return "helper"
        raise Stop
    monkeypatch.setattr(r, "req", req)
    monkeypatch.setattr(r.time, "sleep", lambda s: None)
    with pytest.raises(Stop):
        r.main()
    assert calls[:3] == ["/api/v1/whoami", "/api/v1/whoami", "/api/v1/read"]
