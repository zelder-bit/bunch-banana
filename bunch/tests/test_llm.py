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
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
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
