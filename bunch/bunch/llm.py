"""Thin client for the LiteLLM gateway (OpenAI-compatible API)."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, base_url: str, key: str, timeout: float = 180.0, think_max_tokens: int = 4_096):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.think_max_tokens = think_max_tokens

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
        text, total, thought, cut = await self._complete(payload)
        if (not text or cut) and thought and max_tokens < self.think_max_tokens:
            # A thinking model (gemma4, qwen3...) spent max_tokens on hidden reasoning and said nothing,
            # or stopped mid-answer: give it room to think and still answer.
            text, more, thought, cut = await self._complete({**payload, "max_tokens": self.think_max_tokens})
            total += more
        if not text and thought:
            # Still nothing: ask with thinking off. Only as a last resort, since answers get weaker
            # and some providers reject "none".
            try:
                text, more, _, cut = await self._complete({**payload, "reasoning_effort": "none"})
                total += more
            except LLMError:
                pass
        if text and cut:  # don't pass half an answer off as a whole one
            text = text.rstrip() + "\n\n…(cut off at the length limit; ask me to continue)"
        return text, total

    async def _complete(self, payload: dict) -> tuple[str, int, bool, bool]:
        """Returns (text, total_tokens, whether the model produced reasoning, whether it hit max_tokens)."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(f"{self.base_url}/v1/chat/completions", json=payload, headers=self._headers())
        except httpx.HTTPError as e:
            raise LLMError(f"gateway unreachable: {type(e).__name__}") from e
        if r.status_code != 200:
            raise LLMError(f"gateway {r.status_code}: {_short_error(r)}")
        data = r.json()
        try:
            choice = data["choices"][0]
            msg = choice["message"]
            text = msg.get("content") or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("gateway returned no choices") from e
        thought = bool(msg.get("reasoning") or msg.get("reasoning_content"))
        usage = data.get("usage") or {}
        messages = payload["messages"]
        total = usage.get("total_tokens") or (len(text) // 4 + sum(len(m.get("content", "")) for m in messages) // 4)
        return text, int(total), thought, choice.get("finish_reason") == "length"

    async def models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(f"{self.base_url}/v1/models", headers=self._headers())
        except httpx.HTTPError as e:
            raise LLMError(f"gateway unreachable: {type(e).__name__}") from e
        if r.status_code != 200:
            raise LLMError(f"gateway {r.status_code}: {_short_error(r)}")
        return sorted(m["id"] for m in r.json().get("data", []))


    async def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 10) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.request(method, f"{self.base_url}{path}", json=body, headers=self._headers())
        except httpx.HTTPError as e:
            raise LLMError(f"gateway unreachable: {type(e).__name__}") from e
        if r.status_code != 200:
            msg = _short_error(r)
            if NO_DB_RE.search(msg):
                raise LLMError(NO_DB)
            raise LLMError(f"gateway {r.status_code}: {msg}")
        return r.json()

    async def model_info(self) -> list[dict]:
        """Every deployment the gateway has: [{name, id, api_base, model, stored}].
        stored = added at runtime (kept in LiteLLM's database), so bunch may remove it; config-file ones can't be."""
        out = []
        for m in (await self._call("GET", "/model/info")).get("data", []):
            p, i = m.get("litellm_params") or {}, m.get("model_info") or {}
            if m.get("model_name"):
                out.append({"name": m["model_name"], "id": i.get("id") or "", "api_base": (p.get("api_base") or "").strip(),
                            "model": p.get("model") or "", "stored": bool(i.get("db_model"))})
        return out

    async def model_hosts(self) -> dict[str, str]:
        """Model name -> host it runs on, from LiteLLM's api_base. Models without one (paid APIs) are left out."""
        out = {}
        for m in await self.model_info():
            host = urlsplit(m["api_base"]).hostname if m["api_base"] else None
            if host:
                out[m["name"]] = host
        return out

    async def add_model(self, name: str, model: str, api_base: str | None = None) -> None:
        """Register a deployment at runtime. Needs the gateway's database (STORE_MODEL_IN_DB=True)."""
        params = {"model": model, **({"api_base": api_base} if api_base else {})}
        await self._call("POST", "/model/new", {"model_name": name, "litellm_params": params, "model_info": {}})

    async def delete_model(self, model_id: str) -> None:
        await self._call("POST", "/model/delete", {"id": model_id})


# LiteLLM's answer when /model/new or /model/delete has no database to write to.
NO_DB_RE = re.compile(r"STORE_MODEL_IN_DB|no db connected|database_url|prisma", re.I)
NO_DB = ("adding models from bunch needs the gateway's database: start with --profile keys and set "
         "DATABASE_URL and STORE_MODEL_IN_DB=True (docs/models.md, \"Adding models from the viewer\")")


def _short_error(r: httpx.Response) -> str:
    try:
        err = r.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        msg = r.text
    return (msg or "").replace("\n", " ")[:200]
