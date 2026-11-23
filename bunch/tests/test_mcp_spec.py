import json

from bunch.hub import est_tokens
from bunch.mcp import PROTOCOLS, TOOLS

from .test_bunch import env, h, mcp  # noqa: F401  (env is a fixture)


def test_tools_list_stays_small():
    assert len(TOOLS) == 6
    assert est_tokens(json.dumps(TOOLS)) <= 600
    ro = {t["name"] for t in TOOLS if t.get("annotations", {}).get("readOnlyHint")}
    assert ro == {"get", "rooms"}


def test_unknown_tool_is_jsonrpc_error(env):
    c, k, _, _ = env
    out = mcp(c, k["alice"], "tools/call", {"name": "nope", "arguments": {}})
    assert out["error"]["code"] == -32602 and "result" not in out


def test_protocol_version_header(env):
    c, k, _, _ = env
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert c.post("/mcp", json=body, headers=h(k["alice"])).status_code == 200
    ok = {**h(k["alice"]), "MCP-Protocol-Version": PROTOCOLS[0]}
    assert c.post("/mcp", json=body, headers=ok).status_code == 200
    bad = {**h(k["alice"]), "MCP-Protocol-Version": "1999-01-01"}
    assert c.post("/mcp", json=body, headers=bad).status_code == 400
