"""Thin client for the LiteLLM gateway (OpenAI-compatible API)."""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx


PROBE_TIMEOUT = 3.0
PROBE_TTL = 30.0


class LLMError(Exception):
    pass


class LLMTransport(LLMError):
    """The gateway couldn't be reached or timed out (not a rejection of the request)."""


# Ways to ask for no thinking, tried in this order: OpenAI-style, then Ollama's own flag.
NO_THINK = (("reasoning_effort", "none"), ("think", False))


class LLM:
    def __init__(self, base_url: str, key: str, timeout: float = 180.0, think_max_tokens: int = 4_096,
                 model_probe: bool = True):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.think_max_tokens = think_max_tokens
        self.model_probe = model_probe
        self._client: httpx.AsyncClient | None = None
        self._hosts: dict[str, str] = {}
        self._hosts_at = float("-inf")
        self._probes: dict[str, tuple[float, bool]] = {}
        self._probe_locks: dict[str, asyncio.Lock] = {}
        self._no_think: dict[str, str | None] = {}  # model -> the NO_THINK key the gateway took; None = it took neither

    def _http(self) -> httpx.AsyncClient:
        """One shared client, so connections to the gateway are reused."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}

    async def chat(self, model: str, messages: list[dict], max_tokens: int, user: str | None = None,
                   remaining: int | None = None, retry_limit: int | None = None,
                   cut_notice: Callable[[int], str] | None = None,
                   empty_notice: Callable[[str], None] | None = None) -> tuple[str, int]:
        """Returns (text, total_tokens). remaining: tokens the caller may still spend today (None = no cap);
        a retry is skipped once the calls so far have used that up. empty_notice receives the final
        empty-answer reason separately, so internal callers can keep treating empty text as empty."""
        if time.monotonic() - self._hosts_at >= 300:
            try:
                await self.model_hosts()
            except Exception:  # older gateways / restricted keys must still be usable
                self._hosts = {}
                self._hosts_at = time.monotonic()
        host = self._hosts.get(model)
        if self.model_probe and host and not await self._reachable(host):
            raise LLMError(f"{model} can't answer: its model server {host} isn't reachable from the hub "
                           "(machine off, address changed, or a firewall)")
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if user:
            payload["user"] = user  # lets LiteLLM attribute spend per agent
        final_max = max_tokens
        text, total, thought, cut = await self._complete(payload)
        afford = lambda: remaining is None or total < remaining
        retry_max = min(self.think_max_tokens, retry_limit) if retry_limit is not None else self.think_max_tokens
        if remaining is not None:
            retry_max = min(retry_max, remaining - total)
        if (not text or cut) and thought and max_tokens < retry_max and afford():
            # A thinking model (gemma4, qwen3...) spent max_tokens on hidden reasoning and said nothing,
            # or stopped mid-answer: give it room to think and still answer.
            final_max = retry_max
            text, more, thought, cut = await self._complete({**payload, "max_tokens": final_max})
            total += more
        if not text and thought and afford():
            # Still nothing: ask with thinking off. Only as a last resort, since answers get weaker
            # and some providers reject "none".
            fallback_max = min(max_tokens, remaining - total) if remaining is not None else max_tokens
            known = self._no_think.get(model, "")  # "" = not tried yet
            for key, value in NO_THINK:
                if known is None:
                    break
                if known and known != key:
                    continue
                try:
                    text, more, thought, cut = await self._complete({**payload, "max_tokens": fallback_max, key: value})
                except LLMTransport:
                    break  # unreachable: the other variant would fail the same way
                except LLMError:
                    if known:
                        break  # the variant that worked before now fails: leave it
                    if key == NO_THINK[-1][0]:
                        self._no_think[model] = None  # both rejected: don't try again
                    continue
                self._no_think[model] = key
                final_max = fallback_max
                total += more
                break
        if not text and empty_notice:
            if thought and cut:
                why = f"the model used all {final_max} tokens on reasoning; ask with a higher max_tokens"
            elif thought:
                why = "the model produced reasoning but no final text"
            elif cut:
                why = f"the model hit max_tokens={final_max} without final text; ask with a higher max_tokens"
            else:
                why = "the model returned nothing"
            empty_notice(why)
        if text and cut:  # don't pass half an answer off as a whole one
            notice = cut_notice(final_max) if cut_notice else f"…(cut off at max_tokens={final_max}; ask again with a higher max_tokens or ask for less)"
            text = text.rstrip() + "\n\n" + notice
        return text, total

    async def _complete(self, payload: dict) -> tuple[str, int, bool, bool]:
        """Returns (text, total_tokens, whether the model produced reasoning, whether it hit max_tokens)."""
        try:
            r = await self._http().post(f"{self.base_url}/v1/chat/completions", json=payload, headers=self._headers(), timeout=self.timeout)
        except httpx.HTTPError as e:
            model = payload["model"]
            host = self._hosts.get(model)
            target = f"{model} on {host}" if host else model
            raise LLMTransport(f"{target}: {_transport_error(e, self.timeout)}") from e
        if r.status_code != 200:
            raise LLMError(f"gateway {r.status_code}: {_short_error(r)}")
        data = _json(r)
        try:
            choice = data["choices"][0]
            msg = choice["message"]
            text = msg.get("content") or ""
            if not text.strip():
                text = ""
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMError("gateway returned no choices") from e
        thought = bool(msg.get("reasoning") or msg.get("reasoning_content"))
        usage = data.get("usage") or {}
        messages = payload["messages"]
        total = usage.get("total_tokens") or (len(text) // 4 + sum(len(m.get("content", "") or "") for m in messages) // 4)
        return text, int(total), thought, choice.get("finish_reason") == "length"

    async def models(self) -> list[str]:
        try:
            r = await self._http().get(f"{self.base_url}/v1/models", headers=self._headers(), timeout=30.0)
        except httpx.HTTPError as e:
            raise LLMError(_transport_error(e, 30.0)) from e
        if r.status_code != 200:
            raise LLMError(f"gateway {r.status_code}: {_short_error(r)}")
        try:
            data = _json(r)
            model_list = data.get("data", [])
            return sorted(m["id"] for m in model_list)
        except (KeyError, TypeError, AttributeError):
            raise LLMError("gateway returned an unexpected model list")


    async def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 10) -> dict:
        try:
            r = await self._http().request(method, f"{self.base_url}{path}", json=body, headers=self._headers(), timeout=timeout)
        except httpx.HTTPError as e:
            raise LLMError(_transport_error(e, timeout)) from e
        if r.status_code != 200:
            msg = _short_error(r)
            if NO_DB_RE.search(msg):
                raise LLMError(NO_DB)
            raise LLMError(f"gateway {r.status_code}: {msg}")
        return _json(r)

    async def model_info(self) -> list[dict]:
        """Every deployment, including any description and context limit the gateway knows.
        stored = added at runtime (kept in LiteLLM's database), so bunch may remove it; config-file ones can't be."""
        out = []
        for m in (await self._call("GET", "/model/info")).get("data", []):
            p, i = m.get("litellm_params") or {}, m.get("model_info") or {}
            if m.get("model_name"):
                out.append({"name": m["model_name"], "id": i.get("id") or "", "api_base": (p.get("api_base") or "").strip(),
                            "model": p.get("model") or "", "stored": bool(i.get("db_model")),
                            "description": i.get("description") or "",
                            "context": i.get("max_input_tokens") or i.get("max_tokens")})
        return out

    async def model_hosts(self, info: list[dict] | None = None) -> dict[str, str]:
        """Model name -> host:port from api_base; paid APIs without one are left out."""
        out = {}
        for m in await self.model_info() if info is None else info:
            try:
                url = urlsplit(m["api_base"])
                host = url.hostname
                port = url.port or (443 if url.scheme == "https" else 80)
                if host and url.scheme in ("http", "https"):
                    out[m["name"]] = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            except ValueError:  # a bad api_base must not block other models
                continue
        self._hosts, self._hosts_at = out, time.monotonic()
        return out

    async def _reachable(self, address: str) -> bool:
        # Share both successes and failures across models on the same server.
        async with self._probe_locks.setdefault(address, asyncio.Lock()):
            cached = self._probes.get(address)
            if cached and time.monotonic() - cached[0] < PROBE_TTL:
                return cached[1]
            url = urlsplit(f"//{address}")
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(url.hostname, url.port), PROBE_TIMEOUT)
            except (TimeoutError, OSError):
                reachable = False
            else:
                writer.close()
                reachable = True
            self._probes[address] = (time.monotonic(), reachable)
            return reachable

    async def add_model(self, name: str, model: str, api_base: str | None = None) -> None:
        """Register a deployment at runtime. Needs the gateway's database (STORE_MODEL_IN_DB=True)."""
        params = {"model": model, **({"api_base": api_base} if api_base else {})}
        await self._call("POST", "/model/new", {"model_name": name, "litellm_params": params, "model_info": {}})
        self._hosts_at = float("-inf")

    async def delete_model(self, model_id: str) -> None:
        await self._call("POST", "/model/delete", {"id": model_id})
        self._hosts_at = float("-inf")


# LiteLLM's answer when /model/new or /model/delete has no database to write to.
NO_DB_RE = re.compile(r"STORE_MODEL_IN_DB|no db connected|database_url|prisma", re.I)
NO_DB = ("adding models from bunch needs the gateway's database: start with --profile keys and set "
         "DATABASE_URL and STORE_MODEL_IN_DB=True (docs/models.md, \"Adding models from the viewer\")")


def _json(r: httpx.Response) -> dict:
    """The response body as a JSON object, or LLMError (a proxy's error page can arrive with status 200)."""
    try:
        data = r.json()
    except ValueError as e:
        raise LLMError("gateway returned something that isn't JSON") from e
    if not isinstance(data, dict):
        raise LLMError("gateway returned an unexpected answer")
    return data


def _short_error(r: httpx.Response) -> str:
    try:
        err = r.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        msg = r.text
    return (msg or "").replace("\n", " ")[:200]


def _transport_error(error: httpx.HTTPError, timeout: float) -> str:
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        return f"gateway unreachable: {type(error).__name__}"
    if isinstance(error, httpx.ReadTimeout):
        return f"gateway reached, but no answer after {timeout:g} s (ReadTimeout)"
    return f"gateway request failed: {type(error).__name__}"
