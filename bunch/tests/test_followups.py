"""Follow-ups #148 (double send routed once), #149 (cli list shows a missing model), #150 (one wording for no answer)."""
import asyncio

from .test_bunch import adm, env  # noqa: F401  (env is a fixture)


def test_double_send_is_routed_once(env):  # #148
    c, k, app, llm = env
    hub = app.state.hub
    hub.s = hub.s.__class__(**{**hub.s.__dict__, "router_model": "local/small"})
    asked = []

    async def pick_bob(model, messages, max_tokens, user=None):
        asked.append(user)
        return "bob", 10
    llm.chat = pick_bob
    for _ in range(2):
        r = c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "why is the db slow?"}, headers=adm())
        assert r.status_code == 200
    assert len(c.get("/api/admin/rooms/r/messages", headers=adm()).json()) == 1
    assert asked == ["_router"]


def test_echo_then_empty_retry_gets_the_no_usable_answer_note(env):  # #150
    from bunch.autoreply import answer
    c, k, app, _ = env
    hub = app.state.hub
    replies = ["human>bob re#1: did it work?", ""]

    async def echo_then_nothing(*a, raw=False, **kw):
        return replies.pop(0)
    hub.ask = echo_then_nothing
    mid = int(hub.post("human", "r", "@bob hi", human=True).split("#")[1])
    asyncio.run(answer(hub, dict(hub.store.agent("bob"), model="m", system=""), hub.store.message(mid)))
    assert replies == []
    assert "m gave no usable answer" in hub.store.q("SELECT summary FROM messages WHERE sender='bob'")[0]["summary"]


def test_cli_list_marks_a_model_the_gateway_lacks(env, monkeypatch, capsys):  # #149
    from bunch import cli
    from bunch.llm import LLMUnreachable
    c, k, app, _ = env
    for name, model in (("helper", "local/gone"), ("fine", "local/small")):
        assert c.post("/api/admin/agents", json={"name": name, "model": model}, headers=adm()).status_code == 200
    monkeypatch.setenv("BUNCH_DB", app.state.hub.s.db_path)
    monkeypatch.setenv("LITELLM_URL", "http://gateway.invalid")

    async def info(self):
        return [{"name": "local/small"}]
    monkeypatch.setattr(cli.LLM, "model_info", info)
    assert cli.main(["list"]) == 0
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert "no model" in lines["helper"]
    assert "no model" not in lines["fine"] and "no model" not in lines["alice"]

    async def down(self):
        raise LLMUnreachable("gateway unreachable: ConnectError")
    monkeypatch.setattr(cli.LLM, "model_info", down)
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "no model" not in out and "couldn't check which models the gateway has: gateway unreachable" in out

    monkeypatch.setenv("LITELLM_URL", "")
    assert cli.main(["list"]) == 0
    assert "couldn't check" not in capsys.readouterr().out
