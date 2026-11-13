import importlib.util

import pytest
from pathlib import Path


def _load_worker():
    path = Path(__file__).resolve().parents[2] / "clients" / "worker.py"
    spec = importlib.util.spec_from_file_location("bunch_worker_env", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _set(monkeypatch):
    for k, v in {"BUNCH_MACHINE_KEY": "secret", "BUNCH_KEY": "k", "BUNCH_URL": "http://hub",
                 "WORKER_DIRS": "/x", "WORKER_MAX_POWER": "full", "PATH": "/usr/bin", "HOME": "/tmp/u"}.items():
        monkeypatch.setenv(k, v)


def test_child_env_strips_secrets(monkeypatch):
    _set(monkeypatch)
    w = _load_worker()
    env = w.child_env()
    assert "BUNCH_MACHINE_KEY" not in env and "BUNCH_KEY" not in env and "BUNCH_URL" not in env
    assert not [k for k in env if k.startswith("WORKER_")]
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/tmp/u"
    assert w.KEY == "secret"  # the worker itself still has it


def test_handle_passes_clean_env(monkeypatch):
    _set(monkeypatch)
    w = _load_worker()
    seen = {}

    class P:
        stdout, stderr, returncode = "hi", "", 0

    def fake_run(argv, **kw):
        seen.update(kw, argv=argv)
        return P()

    monkeypatch.setattr(w.subprocess, "run", fake_run)
    monkeypatch.setattr(w, "report", lambda *a, **k: "")
    wk = w.Worker()
    wk.machine = "m"
    wk.agents = {"a": {"cli": "claude", "power": "answer"}}
    sent = []
    wk.reply = lambda job, text: sent.append(text)
    job = {"agent": "a", "id": 1, "room": "r", "sender": "s", "human": True, "text": "hi",
           "parent": None, "created": w.time.time()}
    wk.handle(job)
    assert "env" in seen, "subprocess.run was not reached"
    assert "BUNCH_MACHINE_KEY" not in seen["env"] and "WORKER_DIRS" not in seen["env"]
    assert seen["env"]["PATH"] == "/usr/bin"



def test_merged_env_keeps_what_was_not_given():
    w = _load_worker()
    old = 'BUNCH_URL=http://old\nBUNCH_MACHINE_KEY=k1\nWORKER_MAX_POWER=full\nWORKER_DIRS=/srv\nWORKER_TIMEOUT=900\n'
    text, kept = w.merged_env(old, {'BUNCH_URL': 'http://new', 'BUNCH_MACHINE_KEY': 'k2'}, {'WORKER_MAX_POWER': 'answer', 'WORKER_COOLDOWN': '60'})
    lines = text.strip().split('\n')
    assert 'BUNCH_URL=http://new' in lines
    assert 'BUNCH_MACHINE_KEY=k2' in lines
    assert 'WORKER_MAX_POWER=full' in lines
    assert 'WORKER_DIRS=/srv' in lines
    assert 'WORKER_TIMEOUT=900' in lines
    assert 'WORKER_COOLDOWN=60' in lines
    assert kept == ['WORKER_MAX_POWER', 'WORKER_DIRS', 'WORKER_TIMEOUT']

def test_merged_env_fresh_install_uses_defaults():
    w = _load_worker()
    text, kept = w.merged_env('', {'BUNCH_URL': 'u'}, {'WORKER_MAX_POWER': 'answer'})
    assert text == 'BUNCH_URL=u\nWORKER_MAX_POWER=answer\n'
    assert kept == []

def test_install_twice_keeps_power_and_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    w = _load_worker()
    monkeypatch.setattr(w.subprocess, 'run', lambda *a, **kw: type('P', (), {'returncode': 0})())
    w.install(['--hub', 'http://hub', '--key', 'k1', '--max-power', 'full', '--dirs', '/srv/app'])
    w.install(['--hub', 'http://hub', '--key', 'k2'])
    env_path = tmp_path / '.config/bunch/bunch-worker.env'
    content = env_path.read_text()
    assert 'BUNCH_MACHINE_KEY=k2' in content
    assert 'WORKER_MAX_POWER=full' in content
    assert 'WORKER_DIRS=/srv/app' in content
    unit = (tmp_path / '.config/systemd/user/bunch-worker.service').read_text()
    assert 'NoNewPrivileges=yes' in unit
    assert 'ProtectHome=' not in unit and 'ReadWritePaths=' not in unit
    assert 'k1' not in content
    assert (env_path.stat().st_mode & 0o777) == 0o600

def test_rotate_key_changes_only_the_key(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    w = _load_worker()
    monkeypatch.setattr(w.subprocess, 'run', lambda *a, **kw: type('P', (), {'returncode': 0})())
    w.install(['--hub', 'http://hub', '--key', 'k1', '--max-power', 'read'])
    w.rotate_key(['--key', 'k9'])
    env_path = tmp_path / '.config/bunch/bunch-worker.env'
    content = env_path.read_text()
    assert 'BUNCH_MACHINE_KEY=k9' in content
    assert 'WORKER_MAX_POWER=read' in content


@pytest.mark.parametrize("power, ceiling, allowed", [
    ("answer", "full", True), ("read", "answer", True), ("full", "answer", True),
    ("read", "full", False), ("full", "full", False),
])
def test_agy_answer_refused_without_starting_cli(monkeypatch, tmp_path, power, ceiling, allowed):
    monkeypatch.setenv("WORKER_MAX_POWER", ceiling)
    monkeypatch.setenv("WORKER_DIRS", str(tmp_path) if allowed else "")
    w = _load_worker()

    def unexpected(*a, **kw):
        pytest.fail("answer-only agy must not start a subprocess")
    monkeypatch.setattr(w.subprocess, "run", unexpected)
    wk = w.Worker()
    wk.machine = "desktop"
    wk.agents = {"a": {"cli": "agy", "power": power, "dir": str(tmp_path)}}
    sent = []
    wk.reply = lambda job, text: sent.append((job["room"], text))
    wk.handle({"agent": "a", "id": 1, "room": "r", "human": True, "text": "hi", "created": w.time.time()})
    assert sent == [("r", "(agy can't be limited to answering: it can read files and run commands on desktop. "
                          "Give this agent 'read' or 'full' power on a machine that allows it; not answered)")]
