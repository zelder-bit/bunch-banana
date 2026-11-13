import pytest


@pytest.fixture(autouse=True)
def _fresh_gave_up():
    """autoreply._gave_up is keyed by (agent, message id) and lives for the process. Each test has a new
    database whose ids start again at 1, so an entry left by one test would silence the next."""
    from bunch import autoreply
    autoreply._gave_up.clear()
    yield
