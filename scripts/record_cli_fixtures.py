#!/usr/bin/env python3
"""Record each installed CLI's real output for one tiny prompt into bunch/tests/fixtures/cli/<cli>.json,
so test_cli_fixtures.py notices when a CLI changes its output format. Makes one subscription call per CLI."""
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "bunch" / "tests" / "fixtures" / "cli"
spec = importlib.util.spec_from_file_location("bunch_worker", ROOT / "clients" / "worker.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

SECRET = re.compile(r"sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S{8,}|eyJ[\w-]{10,}\.[\w-]{10,}|"
                    r"(?:api[_-]?key|token|secret|password)[\"']?\s*[=:]\s*[\"']?[A-Za-z0-9_./+-]{12,}|"
                    r"\b(?:ghp_|gho_|xox[abp]-|AKIA)\w{8,}|\b[A-Za-z0-9_]{32,}\b(?!\":)", re.I)  # last: any long bare token, not a JSON key
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def scrub(text, workdir):
    # Longest first, so the FQDN and the real temp path go before their shorter forms.
    names = [(os.path.realpath(workdir), "<WORKDIR>"), (workdir, "<WORKDIR>"), (str(Path.home()), "<HOME>"),
             (socket.getfqdn(), "<HOST>"), (socket.gethostname(), "<HOST>")]
    for real, fake in names:
        if real:
            text = text.replace(real, fake)
    user = os.environ.get("USER", "")
    if user:
        text = re.sub(rf"\b{re.escape(user)}\b", "<USER>", text)
    text = IP.sub("<IP>", EMAIL.sub("<EMAIL>", text))
    text = re.sub(r'("credential_expires_at":\s*)"[^"]*"', r"\1null", text)  # when the login expires: private
    return UUID.sub("00000000-0000-0000-0000-000000000000", text)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    versions = {c["cli"]: c["version"] for c in w.detect_clis()}
    rc_all = 0
    for cli, version in versions.items():
        power = "read" if cli == "agy" else "answer"  # agy has no answer-only mode
        with tempfile.TemporaryDirectory() as workdir:
            argv, cwd, stdin = w.build_cmd({"cli": cli}, "You are a test.", power, workdir, "Reply with exactly: pong")
            try:
                p = subprocess.run(argv, input=stdin, cwd=cwd, env=w.child_env(), capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired:
                print(f"{cli}: timed out, not saved")
                rc_all = 1
                continue
            rec = {"cli": cli, "version": version, "power": power, "rc": p.returncode,
                   "stdout": scrub(p.stdout, workdir), "stderr": scrub(p.stderr, workdir)}
        text, tokens, _, error = w.parse_output(cli, p.stdout, p.stderr, p.returncode)
        if error or "pong" not in text.lower():
            print(f"{cli}: failed ({error or 'no pong in the answer'}), not saved")
            rc_all = 1
        elif SECRET.search(rec["stdout"] + rec["stderr"]):
            print(f"{cli}: output looks like it holds a secret, not saved")
            rc_all = 1
        else:
            (OUT / f"{cli}.json").write_text(json.dumps(rec, indent=2) + "\n")
            print(f"{cli}: saved ({len(rec['stdout'])} bytes stdout, {tokens} tokens)")
    return rc_all


if __name__ == "__main__":
    sys.exit(main())
