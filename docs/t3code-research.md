# What bunch can borrow from t3code

Research notes from 2026-11-27. They are based on [pingdotgg/t3code](https://github.com/pingdotgg/t3code) at the head of `main` on that day. All t3code paths below are in that repository.

## License and trademark

- **License:** t3code is MIT licensed, © 2026 T3 Tools Inc.
  - Borrowing ideas, or writing them again in Python, carries no obligation.
  - If we ever copy code, we must keep their copyright and permission notice with it. Nothing in this repository is copied today.
- **Trademark:** `TRADEMARK.md` says the MIT license covers the source code only. These are reserved:
  - the names "T3 Code", "T3 Connect" and "T3 Tools"
  - the T3 wordmark, logos, icons and `assets/`
  - their domains

  We don't use any of them. A plain factual mention such as "based on ideas from T3 Code" is fine.

## What t3code is

A Node server that wraps agent CLIs. You control it from a web app, an Electron desktop app or a phone app (Expo).

- **Server:** the server owns every process. Clients only send commands over an authenticated WebSocket RPC (`apps/server/src/ws.ts`, `packages/contracts/src/rpc.ts`).
- **Remote access:** you can reach it over the LAN with a one-time pairing code or QR code, over Tailscale, over SSH, or through their own relay (`docs/user/remote-access.md`).

It is one person driving many agent threads. There is no shared room where several agents read the same transcript. The closest it gets is a `delegate_task` MCP tool that starts a child thread on another provider (`apps/server/src/mcp/toolkits/orchestrator/tools.ts`), plus a mid-thread provider switch with a context handoff (`ContextHandoffService.ts`). bunch's rooms, @mentions and loop breaker have no equivalent there.

### How it drives each agent

All providers sit behind one interface, `ProviderAdapterV2` (`packages/provider-core/src/server/ProviderAdapter.ts`):

- **Methods:**
  - `ensureThread` and `resumeThread`
  - `startTurn`, `steerTurn` and `interruptTurn`
  - `respondToRuntimeRequest`
  - `rollbackThread` and `forkThread`
- **Events:** a single stream that includes `message.updated`, `runtime_request.updated` and `command_output.delta`. It ends each turn with `turn.terminal`, whose status is `completed`, `interrupted`, `cancelled` or `failed`.
- **Capabilities:** each adapter declares what it really supports (`OrchestrationV2ProviderCapabilities` in `packages/contracts/src/orchestrationV2.ts`). The rule in `docs/internals/03_providers.md` is that the rest of the code reads those capabilities and never checks provider names.

Each provider is driven differently:

| Provider | How |
|---|---|
| Claude Code | `@anthropic-ai/claude-agent-sdk` `query()` with a long-lived process per session. Prompts are fed through a streaming-input queue. It uses `canUseTool` for approvals, plus `resume`, `interrupt()` and `setModel` (`apps/server/src/orchestration-v2/Adapters/ClaudeAdapterV2.ts`). |
| Codex | One `codex app-server` process speaking JSON-RPC over stdio, with every request given a deadline (`packages/effect-codex-app-server`, `CodexAdapterV2.ts`). |
| OpenCode | Its own server process, driven with `@opencode-ai/sdk` (`packages/provider-opencode`). |
| Others (Gemini-style, Antigravity, Kiro …) | The generic ACP protocol (`packages/provider-acp`). |

### Other parts worth knowing

- **Approvals:** one record type, `OrchestrationV2RuntimeRequest`, covers every kind of approval request:
  - The kinds are command, file read, file change, MCP elicitation and user input.
  - The decisions are `accept`, `acceptForSession`, `acceptAlways`, `decline` and `cancel`.
  - Each request also says whether the answer is still deliverable: `live`, `message` or `not_resumable`.
  - t3code never fakes an approval mode that the provider doesn't enforce itself.
- **Failures:** every failure gets a class (`usage_limit`, `provider_error`, `transport_error`, `permission_error` …) together with `retryable` and `resetAt`. `UsageLimitRecoveryWorker.ts` resumes the work once the limit resets.
- **Workspaces:**
  - Each thread gets its own `git worktree` (`apps/server/src/vcs/GitVcsDriverCore.ts`).
  - Each turn is checkpointed under hidden refs (`refs/t3/...`, `CheckpointService.ts`), so the user's branch gets no extra commits.
  - A diff view shows changes per turn, uncommitted changes, the branch and single commits (`docs/user/diffs.md`).
- **Tests:** real CLI protocol traffic is recorded and replayed in tests (`apps/server/scripts/record-*-replay-fixture.ts`), so CI needs no live subscription.
- **Storage:** an event log on SQLite, with projections, command receipts and an effect outbox (`EventSink.ts`, `EffectOutbox.ts`).

## Where bunch stands

`clients/worker.py` runs each job as one `subprocess.run`:
- `claude -p --output-format json`, `codex exec --json` or `agy --output-format json`.
- A fresh, sessionless run every time, which is deliberate for token thrift (see [clients.md](clients.md)).
- One job at a time, with a 600 s timeout.

Compared with t3code:

| | bunch today | t3code |
|---|---|---|
| Progress while an agent works | "typing" only | streamed items and command output |
| Stop a running agent | no, it runs until the timeout | `interruptTurn` |
| A tool the agent isn't allowed | refused silently, so the answer can come back empty | an approval request shown to the human |
| Several agents editing one folder | one shared room clone, with conflicts sorted out afterwards on a side branch | a worktree per thread |
| What each CLI can do | if/elif branches in `build_cmd` and `parse_output`, with agy's limits written into the viewer text | capabilities declared per adapter |
| CLI output format changes | hand-written JSON in tests | recorded real output |
| Usage limits | a cooldown until the parsed reset time | a failure class, then an automatic resume |

## Roadmap, in order

1. **Capability table per CLI.** *(Done in this change.)*
   - The worker reports which powers each CLI supports.
   - The viewer greys out the others, and the hub refuses an agent config the CLI can't run.
   - This replaces agy's special cases.
   - Study: `ProviderAdapter.ts`, `docs/internals/03_providers.md`. Effort: small.
2. **Recorded CLI fixtures.** *(Done in this change.)*
   - `scripts/record_cli_fixtures.py` saves one real "pong" run per installed CLI to `bunch/tests/fixtures/cli/`.
   - `test_cli_fixtures.py` checks `parse_output` against those runs.
   - Record them again after a CLI upgrade.
   - Effort: small.
3. **Failure classes and redelivery after a limit.**
   - The worker reports why a run failed: limit, timeout, permission or CLI error.
   - The hub requeues limit failures once the limit resets, instead of the room only seeing an error. This builds on `LIMIT_RE` and `reset_time`.
   - Study: `ProviderFailureClass`, `UsageLimitRecoveryWorker.ts`, `claudeUsageLimits.ts`. Effort: small to medium.
4. **Live progress and cancel.**
   - Switch from `subprocess.run` to `Popen` with streaming JSON (`claude --output-format stream-json`, and codex's JSONL is already line by line).
   - Send short progress lines through the existing `/ws` typing event.
   - Add a hub command that kills the run.
   - Study: `ProviderTurnControlService.ts`. Its lesson: only allow a cancel once the CLI has confirmed the turn started. Effort: medium.
5. **Approvals as room messages.**
   - When an agent wants a tool it isn't allowed, it posts a "may I run X?" message, and a human answers in the room.
   - This needs a live session, for example the Python `claude-agent-sdk` with `can_use_tool`. A one-shot `claude -p` can't wait for an answer.
   - Study: `OrchestrationV2RuntimeRequest`, `RuntimeRequestService.ts`. Effort: medium to large.
6. **A worktree per job in room folders, plus a diff view.**
   - Each run works in its own `git worktree`, then merges back.
   - The viewer shows the diff of each job.
   - Study: `GitVcsDriverCore.ts`, `CheckpointService.ts`, `CheckpointDiffQuery.ts`. Effort: medium.
7. **Machine pairing codes.**
   - Pair a machine with a short one-time code instead of pasting a key.
   - Study: `apps/server/src/auth/PairingGrantStore.ts`, `docs/internals/04_environment-auth.md`. Effort: small to medium.

## Not worth copying

- **Event log, outbox and command receipts.** These are built for a multi-process server with crash recovery. bunch is one process on SQLite, and t3code's own docs say its event log can't rebuild state by itself.
- **Resuming sessions by default.** Resuming keeps a growing context that costs tokens on every turn. bunch's summary plus last-lines prompt is cheaper. If resume is ever added, it should be an opt-in per agent.
- **The phone apps and cloud relay.** The bunch viewer already works on phones, and the hub stays on the home network.
