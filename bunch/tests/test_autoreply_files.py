"""Hub-run agents save files to the room folder (#123) and the echo filter (#124)."""
import asyncio

from bunch import autoreply
from bunch.autoreply import autoreply_once
from bunch.hub import HubError
from bunch.store import text_of

from .test_agent_folders import _folder
from .test_bunch import adm, env  # noqa: F401  (env is a fixture)
from .test_ux import _auto, _from


def _run(env, replies, room="proj", folder=True, text="@aa save the script"):
    """Posts a human message to auto agent aa; the model returns replies in turn. Returns (client, hub, prompts)."""
    c, _k, app, llm = env
    hub, prompts, it = app.state.hub, [], iter(replies)
    if folder:
        _folder(c, room)
    _auto(c, "aa")

    async def chat(model, messages, max_tokens, user=None, **kw):
        prompts.append(messages)
        return next(it), 10
    llm.chat = chat
    hub.post("human", room, text, human=True)
    asyncio.run(autoreply_once(hub))
    return c, hub, prompts


def _texts(c, room="proj"):
    return [text_of(m) for m in _from(c, room, "aa") if not text_of(m).endswith("in the room folder)")]


SCRIPT = "Here you go.\n\nsave: tools/run.py\n```python\nprint('hi')\n```\nDone."


def test_reply_with_save_block_writes_the_file(env):
    c, hub, prompts = _run(env, [SCRIPT])
    assert "save: <relative/path>" in str(prompts[0])
    assert hub.store.folder("proj")
    reply = _texts(c)[0]
    assert "print('hi')" in reply and reply.endswith("(saved tools/run.py to the room folder)")
    assert "print('hi')" in asyncio.run(hub.files_read("aa", "proj", "tools/run.py"))


def test_save_tolerates_backticks_bold_and_caps_files(env):
    body = "".join(f"**save:** `f{i}.txt`\n```text\nx{i}\n```\n" for i in range(7))
    c, _hub, _p = _run(env, [body])
    reply = _texts(c)[0]
    assert reply.count("(saved ") == autoreply.MAX_SAVES and "f4.txt" in reply and "(saved f5.txt" not in reply


def test_failed_save_still_posts_the_reply(env):
    c, _hub, _p = _run(env, ["save: ../escape.py\n```\nx\n```"])
    reply = _texts(c)[0]
    assert "(could not save ../escape.py:" in reply and "x" in reply


def test_save_error_from_hub_is_reported(env, monkeypatch):
    async def boom(*a, **k):
        raise HubError("quota used up")
    c, _k, app, _llm = env
    monkeypatch.setattr(type(app.state.hub), "files_write", boom)
    c2, _hub, _p = _run(env, [SCRIPT])
    assert "(could not save tools/run.py: quota used up)" in _texts(c2)[0]


def test_no_folder_prompt_says_it_cannot_save_and_nothing_is_written(env):
    c, hub, prompts = _run(env, [SCRIPT], room="plain", folder=False)
    assert "cannot upload, save or run anything" in str(prompts[0])
    assert "save: <relative/path>" not in str(prompts[0])
    assert "(saved" not in _texts(c, "plain")[0]


# ------------------------------------------------------------------ #124 echo filter
SUMMARY = "Room #1 summary: " + "the team planned the release of the new parser and agreed to test it on friday"
RECENT = "#5 bob>human re#4: please check the failing build on the staging server before lunch today"
CTX = [SUMMARY, RECENT]


def echo(raw, name="aa", others=("bob", "cy")):
    return autoreply._is_echo(raw, name, set(others), CTX)


def test_echo_shapes_are_detected():
    assert echo("**Summary of Room #1 (continued):** ### Goals: ship it ### Decisions: none")
    assert echo("Hey @aa, just checking in - are you able to look at this?")
    assert echo("Hey aa, are you there?")
    assert echo("@aa can you do it")
    assert echo("#7 bob>aa re#6: sure thing\n#8 cy>bob re#7: ok")
    assert echo("bob: I will do it\ncy: me too\naa: fine")
    assert echo("bob>human re#4: only a made-up line")
    assert echo("the team planned the release of the new parser and agreed to test it on friday and more")
    assert echo("bob human re 4 please check the failing build on the staging server before lunch today ok")


def test_normal_answers_are_not_flagged():
    assert not echo("Yes, the build on staging fails because of the parser; I will fix it.")
    assert not echo("Use `x = 1`.\nNote: this is fine.\nSee also: docs.")
    assert not echo("The summary of the issue is that it fails on friday.")
    assert not echo("aa>human re#4: Done, fixed the parser.")  # own header is dropped
    assert not echo("Saw the build on the staging server before lunch.")  # short quote of context
    assert not echo("```\nbob: x\ncy: y\n```\nThat is the log format.")


def test_echo_is_retried_once_with_a_corrective_instruction(env):
    c, _hub, prompts = _run(env, ["**Summary of Room #1:** ### Goals: x", "Final answer."], folder=False)
    assert len(prompts) == 2 and "Answer only message #" in str(prompts[1]) and "Answer only message #" not in str(prompts[0])
    assert _texts(c, "proj") == ["Final answer."]


def test_echo_twice_gives_an_honest_note_for_a_human(env):
    bad = "Hey @aa, just checking in - are you there?"
    c, _hub, prompts = _run(env, [bad, bad], folder=False)
    assert len(prompts) == 2
    assert _texts(c, "proj") == ["(local/small gave no usable answer; ask again or rephrase)"]
