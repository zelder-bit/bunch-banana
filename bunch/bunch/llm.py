"""Thin client for the LiteLLM gateway (OpenAI-compatible API)."""
from __future__ import annotations

from urllib.parse import urlsplit

import httpx


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, base_url: str, key: str, timeout: float = 180.0):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}

    async def chat(self, model: str, messages: list[dict], max_tokens: int, user: str | None = None) -> tuple[str, int]:
        """Returns (text, total_tokens)."""
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if user:
            payload["user"] = user  # lets LiteLLM attribute spend per agent
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(f"{self.base_url}/v1/chat/completions", json=payload, headers=self._headers())
        except httpx.HTTPError as e:
            raise LLMError(f"gateway unreachable: {type(e).__name__}") from e
        if r.status_code != 200:
            raise LLMError(f"gateway {r.status_code}: {_short_error(r)}")
        data = r.json()
        try:
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("gateway returned no choices") from e
        usage = data.get("usage") or {}
        total = usage.get("total_tokens") or (len(text) // 4 + sum(len(m.get("content", "")) for m in messages) // 4)
        return text, int(total)

    async def models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(f"{self.base_url}/v1/models", headers=self._headers())
        except httpx.HTTPError as e:
            raise LLMError(f"gateway unreachable: {type(e).__name__}") from e
        if r.status_code != 200:
            raise LLMError(f"gateway {r.status_code}: {_short_error(r)}")
        return sorted(m["id"] for m in r.json().get("data", []))


    async def model_hosts(self) -> dict[str, str]:
        """Model name -> host it runs on, from LiteLLM's api_base. Models without one (paid APIs) are left out."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self.base_url}/model/info", headers=self._headers())
        except httpx.HTTPError as e:
            raise LLMError(f"gateway unreachable: {type(e).__name__}") from e
        if r.status_code != 200:
            raise LLMError(f"gateway {r.status_code}: {_short_error(r)}")
        out = {}
        for m in r.json().get("data", []):
            base = ((m.get("litellm_params") or {}).get("api_base") or "").strip()
            host = urlsplit(base).hostname if base else None
            if m.get("model_name") and host:
                out[m["model_name"]] = host
        return out


def _short_error(r: httpx.Response) -> str:
    try:
        err = r.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        msg = r.text
    return (msg or "").replace("\n", " ")[:200]
