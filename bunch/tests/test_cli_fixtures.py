"""parse_output against real CLI output recorded by scripts/record_cli_fixtures.py: catches format drift."""
import json
from pathlib import Path

import pytest

from .test_bunch import _load_worker

FILES = sorted((Path(__file__).parent / "fixtures" / "cli").glob("*.json"))


@pytest.mark.skipif(not FILES, reason="no recorded CLI output (run scripts/record_cli_fixtures.py)")
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_recorded_output_still_parses(path):
    f = json.loads(path.read_text())
    text, tokens, _, error = _load_worker().parse_output(f["cli"], f["stdout"], f["stderr"], f["rc"])
    assert error is None and "pong" in text.lower() and tokens > 0
