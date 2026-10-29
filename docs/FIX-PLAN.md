# Fix plan for the 2026-10-28 user test

Issue numbers refer to [TEST-REPORT.md](TEST-REPORT.md).

## Steps


1. **Pictures are honest about power** (`clients/worker.py`, issues 1, 2, 7)
   - Add the "To send a picture…" line only when the run can actually write files (`power == "full"` with an allowed dir). Otherwise add: "You cannot create files or images here; say so plainly."
   - When the requested power is lowered, add one short line to the reply: "(ran as answer: this machine allows only answer)", and report it to the hub. Then the viewer's `hello` data (`max_power`, `dirs`) can grey out the options a machine won't allow in the "Who answers?" picker (`bunch/bunch/static`, viewer JS).
   - Docs: say that image generation needs `WORKER_MAX_POWER=full` plus `WORKER_DIRS`, and which CLIs can actually make images.
2. **No silent drops** (`worker.py:handle`, issue 4)
   - On an unknown agent, call `self.hello(force=True)` and look it up again. If it's still unknown, reply "(not set up on <machine> yet)" instead of returning.
3. **Exact @mention match** (issue 5)
   - Use a word-boundary regex `(?<![\w-])@name(?![\w-])` in both `worker.py` and `autoreply.py`, with a shared helper or the same pattern. The hub's existing mention parser in `bunch/bunch/hub.py` may already handle this; reuse it if so.
4. **agy empty answers** (issue 3)
   - Run `agy --output-format json --disable-slash-commands --sandbox -p "say hi"` and the image prompt once by hand, and look at the stdout, stderr and status.
   - Make `parse_output` include stderr or the status in the error when `response` is empty. Change the flags if `--sandbox` turns out to be the cause.
5. **Router and filler answers** (issues 6, 8)
   - Router: for a short reply with no @ (like "try again"), send it to whoever answered last in the room instead of picking up to 2 agents.
   - autoreply: treat a model answer that is only a parenthesised error (`^\(.*\)$` matching the worker's error forms) as empty.
6. **Usage display** (issue 12): reproduce on the `:8799` hub with a fake worker report, then fix wherever the count or label goes wrong. Make the per-message summary prompt name the sender (issue 13).
7. **Low items**: trace the double "opened" post (`hub.py`, look for "opened"); post "no agent named X" on an unknown @; check codex token counting against the README, and fix whichever is wrong.
8. Add tests in `bunch/tests` (the worker's pure parts are already tested there) for: the mention regex, the prompt by power, re-reading the agent list for an unknown agent, and `parse_output` for empty agy output.

## Verification
- Unit tests: venv per memory, `pytest -p no:cacheprovider bunch/tests`.
- End to end on a throwaway hub on `:8799` (never 8711, never the proxmox hub config): start the hub, pair a worker with `BUNCH_URL=http://127.0.0.1:8799`, then as a user:
  - @claude says hi → it answers.
  - Create a new agent and @ it straight away → it answers, or says it isn't set up. It must not stay silent.
  - Have an agent post `@gemini-local` → `gemini` doesn't run.
  - @codex "make an image" in answer mode → it says plainly that it can't.
  - Stop the worker, then @claude → an instant "offline, not answered" note.
- Then you redeploy the proxmox hub yourself, and we repeat the #pic-generation scenario there.

## Status (2026-10-28, branch `fix/user-test-findings`)

| Issue | Status |
|---|---|
| 1, 2 pictures | Fixed: runs that can't write files are told so; the "save a picture" line appears only with `full` power. Verified: claude answers "I can't create images in this run". |
| 3 agy empty | Root cause: agy reaches for the `bunch` MCP server in its global config, headless mode refuses it, and agy returns SUCCESS with an empty answer. Fixed with a "reply in plain text" instruction (verified: real answer in 10s, 8.7k tokens), plus an error that names the refused tool. `agy mcp disable bunch` on beep-boop is optional; that's the owner's call. |
| 4 dropped jobs | Fixed: the worker re-reads its agents, then answers or says the agent isn't set up. Verified with an agent created after the worker started. |
| 5 @ substring | Fixed in worker, autoreply and responder (exact mention match). |
| 6 "Try again" fan-out | Fixed: a message of three words or fewer goes to the last agent that answered in the room (within 30 min). |
| 7 power lowered silently | Fixed: the answer ends with a note, and the viewer disables powers above the machine's limit. |
| 8 copied "(empty answer)" | Fixed: autoreply treats copied error notes as empty. |
| 9 codex tokens | The count is right (one run used tools). README and clients.md now give the range. |
| 10 double "opened" | Fixed: the new-room form ignores a second submit and skips the post for rooms that already exist. |
| 11 unknown @ | Fixed: the viewer warns "No one here is called @x". |
| 12 usage shows 0 | Not lost: the usage day is UTC, which rolls over at 6 PM MDT. The sidebar now reads "Agents · since 6:00 PM" and explains why on hover. |
| 13 summary speaker | Fixed: the message summarizer is told who sent the message. |
| Theme | Every panel (Files and the rest) and both sign-in cards (name and password, admin token) have fruit on all four edges instead of the top vine. |

Tests: 61 pass, 8 of them new. End to end on a throwaway hub on :8799 with the real claude CLI: answer, new agent, power note, image refusal, unknown @, offline note.
