import importlib.util
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
