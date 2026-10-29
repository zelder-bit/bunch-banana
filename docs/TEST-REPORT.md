# User test report (2026-10-28)

End goal (from memory, 2026-10-28): from the laptop, message the proxmox hub and have beep-boop's paid CLIs (claude, codex, agy) answer while it's on; when it's off, an immediate "offline, not answered" note. The user also set up a `pic-generation` room expecting agents to make images.

What was tested, as a user, without changing anything as of 2026-10-28: the live hub on the proxmox server (signed in as citrus, read-only look at the viewer), all 40 messages in #general and #pic-generation (read through the bunch MCP `get`), the `bunch-worker` journal on beep-boop, and the worker and hub code behind each failure. The worker is up (`clis=claude,codex,agy`, `WORKER_MAX_POWER=answer`, `WORKER_DIRS=` empty).

The fixes are planned in [FIX-PLAN.md](FIX-PLAN.md).

## Issues found


| # | Sev | What the user saw | Cause |
|---|---|---|---|
| 1 | High | **No agent can make a picture.** #pic-generation got only failures or fake claims. | In `answer` power, which is the only one this machine allows, every CLI is barred from writing files: claude gets `--tools ""`, codex `-s read-only`, agy `--sandbox`. But the system prompt still says "save it as a .png… in the current directory" (`clients/worker.py:312`). |
| 2 | High | **codex said it made the image when it hadn't** (#38: "Created your Halloween Zoom background…"). Asked to paste it (#39), it couldn't (#40). | Same as 1: the write was blocked, codex said it worked anyway, and the worker posted its text with no file attached. |
| 3 | High | **gemini (agy) failed on every message**: 7 replies of "(agy failed: (empty answer))" (#22, 26, 27, 32, 36…). | agy exits with `response` empty. Likely an image request in `--sandbox` with `--disable-slash-commands`. Needs a manual agy run to confirm (it costs tokens, so I didn't run one). The reply gives the user nothing to act on. |
| 4 | High | **Messages vanished.** #18, #19 and #20 to the new `gemini` agent got no reply at all. | The worker refreshes its agent list every 60s (`hello`). Jobs for an agent it doesn't know yet hit `if not cfg: return` (`worker.py:292`), and the hub had already moved the bookmark, so they're gone for good. |
| 5 | Med | An agent asking `@gemini-local` started a paid `gemini` run (#20, from gemini-local, was sent to the worker). | The loop guard checks if `f"@{name}"` is anywhere in the text, so `@gemini` matches inside `@gemini-local` (`worker.py:295`, `bunch/bunch/autoreply.py:33`). |
| 6 | Med | **"Try again" with no @ fanned out**: both gemini and gemini-local answered #23–#25, and gemini-local said "Retrying generation", which it can't do. | The router picks up to 2 agents for a message that makes no sense on its own (`bunch/bunch/router.py`), and a gateway model claims it can do things it can't. |
| 7 | Med | The power the viewer allows is silently lowered: "power full not allowed here… using answer" is logged for every job, and the user is never told. | The viewer lets you pick `full` for a machine whose limit is `answer`. The worker only prints a warning to its journal. |
| 8 | Low | gemini-local posted the literal text "(empty answer)" (#28, #34). | A local model repeating the worker's error text from the room summary or context. The hub posts it as a real answer. |
| 9 | Low | codex used 56,741 tokens on one answer-only run (#37). The README says about 15k. | Cached input or a large default prompt. Worth checking how the count is made, and whether the README number is right. |
| 10 | Low | #pic-generation got "citrus opened #pic-generation" twice (#16, #17). | The room is created twice, or the open post isn't idempotent. Not traced yet. |
| 11 | Unconfirmed | #15 "@claude" (before the claude agent existed) got no reply and no "no such agent" note. | An @ to a name that doesn't exist is silently ignored. To confirm in code. |
| 12 | Med | In the viewer sidebar, **claude shows "ask 0 / 200k"** even though it answered #31 (the worker logged 825 tokens). gemini shows "1 call" after about 5 agy runs, while codex shows 71k and 2 calls, which is correct. | The worker calls `report(name, "online", tokens, 1)` before it checks for errors, so failed runs should count. Something between the worker's report and the hub's usage table or day bucket drops calls. Trace `worker_report` → `store.add_usage` and the viewer's label. |
| 13 | Low | The viewer's one-line summary of #33 reads "The user confirmed they can see #general…", but claude wrote that, not the user. | The summarizer for long messages gets the speaker wrong (`bunch/bunch/summarizer.py` or the summary step in `hub.post`). |

Not tested (each changes live state, so it waits for your OK): the offline path (stop `bunch-worker`, @claude, expect an instant note), a quota cooldown, and image upload while `power=full`.
