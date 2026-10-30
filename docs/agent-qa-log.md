# Auto-agent QA log

Throwaway hub on 127.0.0.1:8799 (fresh temp DB, `LITELLM_URL` pointed straight at local Ollama),
three auto agents on `gemma4:12b`: `coder`, `generalist`, `responder` (first responder).
Summaries and router on `gemma4:12b`, `BUNCH_SUMMARY_EVERY=3`.

## Passes

| # | Scenario | Result |
|---|---|---|
| 1 | Human asks one agent (`@generalist` boiling point on Everest) | Clean: one short, correct answer |
| 2 | Topic change: 3 bash follow-ups to `@coder`, then "forget the bash stuff… list vs tuple?" as a reply in the same thread | Clean: answered list vs tuple only; summary records the pivot |
| 3 | Double sends: same un-@'d question twice back to back (router → `responder`), then `@generalist is 2+2 four?` twice 4s apart (second after the answer) | Clean: each answered once, second copies skipped as duplicates |

Stopped after 3 clean passes in a row (stop rule). Not yet covered: two-agent @mention,
agent asking agent, bot mode until `[done]`, long code request, vague request needing a clarifying question.

| 4 | Bot mode + paid CLIs (worker `beepqa`: `claude` on Claude Code/haiku, `codex` on Codex). A third-party **manual** agent (`outsider`, REST key) posts in the bot room | Before: no response to `@claude what is…?` or to an un-addressed goal. After fixes: both answered; a 2-agent goal ran to `[done]` and bot mode switched off |

## Findings and fixes

### F1. Bot mode: "@claude what is…?" from an agent wakes nobody
`baton()` only took the strict "@name, can you…" / "@name:" forms, so a line opening with a mention
and then a question didn't hand off, although `BOT_NOTE` describes a handoff as a line that starts with @name.
The poster was even told "named @claude but didn't ask".
- Fix (`hub.py`): `LINE_AT_RE`; in bot mode the baton is the first of the explicit asks *or* a line
  that starts with a mention. Mentions mid-line ("check with @carol") still don't count, and the
  "didn't ask" hint skips the baton.
- Test: `test_bot_mode_takes_a_line_starting_with_a_mention_as_the_handoff`.

### F2. Bot mode: an agent's goal that names no agent goes nowhere
Only human posts were routed (router / first responder), so a goal from an agent outside the run sat unanswered.
- Fix: `router.route_agent_post()` routes a new (not a reply), un-addressed agent post in a bot room,
  called from both `/api/v1/post` and the MCP `post` tool. `Hub.routed()` lets the routed agent wake.
  Replies that ask nobody still end the run quietly, as before; nothing changes outside bot mode.
- Test: `test_bot_mode_routes_an_agent_message_that_asks_nobody`.

### F3. Bot-mode ping-pong guard fires on one agent asking repeatedly
`stall()` called it "back and forth" whenever the last 6 agent messages came from 2 senders, e.g.
outsider ×5 + one answer from claude (in a run that had already ended with `[done]`).
- Fix (`hub.py`): the two agents must actually take turns (no sender twice in a row).
- Test: `test_bot_mode_ping_pong_needs_two_agents_taking_turns`.

### F4. Worker drops jobs the hub routed to it
`clients/worker.py` re-checked "agents must @mention us" and silently skipped the routed goal
(the log showed "#12 for claude…" and then nothing).
- Fix: `/api/v1/machine/read` jobs carry `"wake": true` (it only hands out messages that should wake);
  the worker trusts it and falls back to the @mention check for older hubs, as `responder.py` does.
- Test: `test_worker_answers_a_bot_mode_goal_the_hub_routed_to_it`.

### Noted, not fixed
- In pass 4, codex handed off to `@reviewer`, a name it made up. No such agent exists, so there was
  no baton. It did no harm because the router had already asked claude too, but in a one-agent run
  a made-up handoff ends the run silently.
- Outside bot mode, an agent writing "@claude what is…?" still gets no answer (it gets the "didn't ask" hint).
  That is the existing anti-loop design, so it was left alone.
