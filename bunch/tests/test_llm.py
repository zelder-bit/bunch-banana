"""The gateway client: malformed answers, the shared connection, retries within the budget (#66)."""
import asyncio

import httpx
import pytest

from bunch.llm import LLM, LLMError

def test_chat_gateway_returns_html():
    def handler(request):
        return httpx.Response(200, text="<html>oops</html>")

    llm = make(handler)
    with pytest.raises(LLMError, match="gateway returned something that isn't JSON"):
        asyncio.run(llm.chat("m", [{"role": "user", "content": "hi"}], 10))


def make(handler):
    llm = LLM("http://gw", "k", think_max_tokens=500)
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": []}) if req.url.path == "/model/info" else handler(req)))
    return llm


def test_chat_gateway_returns_list():
    def handler(request):
        return httpx.Response(200, json=[1, 2])

    llm = make(handler)
    with pytest.raises(LLMError, match="gateway returned an unexpected answer"):
        asyncio.run(llm.chat("m", [{"role": "user", "content": "hi"}], 10))


def test_models_invalid_response():
    def handler(request):
        return httpx.Response(200, json={"data": [{"name": "x"}]})

    llm = make(handler)
    with pytest.raises(LLMError, match="gateway returned an unexpected model list"):
        asyncio.run(llm.models())


def test_models_sorting():
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}]})

    llm = make(handler)
    result = asyncio.run(llm.models())
    assert result == ["a", "b"]


def test_chat_no_usage():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]
        })

    llm = make(handler)
    text, total = asyncio.run(llm.chat("m", [{"role": "user", "content": None}], 10))
    assert text == "hi" and total == 0  # a None content must not raise in the token estimate


def test_chat_respects_remaining():
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "reasoning_content": "hmm"}, "finish_reason": "length"}],
            "usage": {"total_tokens": 100}
        })

    llm = make(handler)
    # First call with remaining=100 should make only 1 call
    asyncio.run(llm.chat("m", [{"role": "user", "content": "hi"}], 50, remaining=100))
    assert len(calls) == 1

    # Fresh llm without budget should make 3 calls
    calls.clear()
    llm2 = make(handler)
    asyncio.run(llm2.chat("m", [{"role": "user", "content": "hi"}], 50))
    assert len(calls) == 3


@pytest.mark.parametrize("error, wording", [
    (httpx.ConnectError, "gateway unreachable"),
    (httpx.ConnectTimeout, "gateway unreachable"),
    (httpx.ReadTimeout, "gateway reached, but no answer after 180 s"),
    (httpx.WriteError, "gateway request failed"),
])
def test_chat_transport_errors(error, wording):
    def handler(request):
        raise error("failed")
    llm = make(handler)
    with pytest.raises(LLMError) as exc:
        asyncio.run(llm.chat("local/coder", [], 10))
    assert str(exc.value).startswith("local/coder: " + wording)


def test_model_hosts_keep_ports_and_ipv6():
    llm = make(lambda req: httpx.Response(200))
    async def info():
        return [{"name": n, "api_base": base} for n, base in [
            ("local", "http://host:11434/v1"), ("tls", "https://host/v1"),
            ("http", "http://host"), ("ipv6", "http://[::1]:11434"),
            ("paid", ""), ("bad", "http://host:nope")]]
    llm.model_info = info
    assert asyncio.run(llm.model_hosts()) == {
        "local": "host:11434", "tls": "host:443", "http": "host:80", "ipv6": "[::1]:11434"}


@pytest.mark.parametrize("reachable", [True, False])
def test_probe_cached_per_host_port(monkeypatch, reachable):
    import bunch.llm as module
    now, connects, closed = [1000], [], []
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    class Writer:
        def close(self):
            closed.append(True)
    async def connect(host, port):
        connects.append((host, port))
        if not reachable:
            raise OSError("refused")
        return None, Writer()
    monkeypatch.setattr(module.asyncio, "open_connection", connect)
    llm = make(lambda req: httpx.Response(200, json={
        "choices": [{"message": {"content": "OK"}}]}))
    llm._hosts = {"a": "host:11434", "b": "host:11434", "c": "host:11435"}
    llm._hosts_at = now[0]
    async def chat(model):
        if reachable:
            assert (await llm.chat(model, [], 10))[0] == "OK"
        else:
            with pytest.raises(LLMError) as exc:
                await llm.chat(model, [], 10)
            assert f"{model} can't answer: its model server host:" in str(exc.value)
            assert "isn't reachable from the hub" in str(exc.value)[:160]
    async def run():
        await asyncio.gather(chat("a"), chat("b"))
        assert len(connects) == 1
        await chat("c")
        assert len(connects) == 2
        now[0] += 31
        await chat("b")
        assert len(connects) == 3
    asyncio.run(run())
    assert len(closed) == (3 if reachable else 0)


def test_probe_timeout_and_read_timeout(monkeypatch):
    import bunch.llm as module
    async def connect(host, port):
        await asyncio.sleep(1)
    monkeypatch.setattr(module.asyncio, "open_connection", connect)
    monkeypatch.setattr(module, "PROBE_TIMEOUT", .001)
    def handler(req):
        raise httpx.ReadTimeout("slow")
    llm = make(handler)
    llm._hosts = {"local/coder": "192.0.2.7:11434"}
    llm._hosts_at = module.time.monotonic()
    with pytest.raises(LLMError, match="isn't reachable from the hub"):
        asyncio.run(llm.chat("local/coder", [], 10))
    async def reachable(address):
        return True
    monkeypatch.setattr(llm, "_reachable", reachable)
    with pytest.raises(LLMError) as exc:
        asyncio.run(llm.chat("local/coder", [], 10))
    assert str(exc.value) == ("local/coder on 192.0.2.7:11434: "
                              "gateway reached, but no answer after 180 s (ReadTimeout)")
    assert len(str(exc.value)) < 160


def test_lookup_failure_skips_probe(monkeypatch):
    llm = make(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}))
    async def info():
        raise LLMError("lookup unavailable")
    async def probe(address):
        pytest.fail("must skip probe")
    monkeypatch.setattr(llm, "model_info", info)
    monkeypatch.setattr(llm, "_reachable", probe)
    assert asyncio.run(llm.chat("paid/model", [], 10))[0] == "OK"


def test_model_changes_refresh_hosts():
    llm = make(lambda req: httpx.Response(200, json={}))
    async def run():
        llm._hosts_at = 100
        await llm.add_model("local/new", "ollama_chat/new", "http://host:11434")
        assert llm._hosts_at == float("-inf")
        llm._hosts_at = 100
        await llm.delete_model("id")
        assert llm._hosts_at == float("-inf")
    asyncio.run(run())


def test_chat_discovers_server_and_skips_models_without_api_base(monkeypatch):
    calls, probes = [], []
    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/model/info":
            return httpx.Response(200, json={"data": [
                {"model_name": "local/coder", "litellm_params": {"api_base": "http://host:11434"}},
                {"model_name": "paid/model", "litellm_params": {}},
            ]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    llm = LLM("http://gw", "k")
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async def probe(address):
        probes.append(address)
        return False
    monkeypatch.setattr(llm, "_reachable", probe)
    async def run():
        with pytest.raises(LLMError, match="host:11434 isn't reachable from the hub"):
            await llm.chat("local/coder", [], 10)
        assert calls == ["/model/info"]
        assert (await llm.chat("paid/model", [], 10))[0] == "OK"
        assert calls == ["/model/info", "/v1/chat/completions"]
        assert probes == ["host:11434"]
        await llm.aclose()
    asyncio.run(run())


def test_probe_disabled_attempts_no_connection(monkeypatch):
    from bunch.config import load
    async def connect(host, port):
        pytest.fail("disabled probe must not connect")
    monkeypatch.setattr(asyncio, "open_connection", connect)
    def handler(req):
        if req.url.path == "/model/info":
            return httpx.Response(200, json={"data": [
                {"model_name": "local/coder", "litellm_params": {"api_base": "http://host:11434"}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    monkeypatch.delenv("BUNCH_MODEL_PROBE", raising=False)
    assert load().model_probe is True
    for value in ("0", "false", "no"):
        monkeypatch.setenv("BUNCH_MODEL_PROBE", value)
        settings = load()
        assert settings.model_probe is False
        llm = LLM("http://gw", "k", model_probe=settings.model_probe)
        llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async def run():
            assert (await llm.chat("local/coder", [], 10))[0] == "OK"
            assert llm._hosts == {"local/coder": "host:11434"}
            await llm.aclose()
        asyncio.run(run())


@pytest.mark.parametrize("remaining,expected", [(None, 300), (250, 150)])
def test_retry_size_and_cut_notice(remaining, expected):
    import json
    sent = []
    def handler(request):
        sent.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "partial", "reasoning_content": "hmm"}, "finish_reason": "length"}],
            "usage": {"total_tokens": 100}
        })
    llm = make(handler)
    text, total = asyncio.run(llm.chat("m", [], 50, remaining=remaining, retry_limit=300,
                                      cut_notice=lambda n: f"cut at {n}"))
    assert sent == [50, expected] and total == 200
    assert text.endswith(f"cut at {expected}")


def test_non_thinking_fallback_size():
    import json
    sent = []
    def handler(request):
        sent.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "reasoning_content": "hmm"}, "finish_reason": "length"}],
            "usage": {"total_tokens": 100}
        })
    asyncio.run(make(handler).chat("m", [], 80, remaining=150, retry_limit=80))
    assert sent == [80, 50]
