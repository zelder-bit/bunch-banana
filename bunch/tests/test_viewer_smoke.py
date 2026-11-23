"""Browser smoke test for the viewer: a real Chromium against a real hub on a temporary database.
Skipped when the playwright package or its browser is not installed (CI installs it)."""
import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from bunch.app import create_app
from bunch.config import Settings

from .test_bunch import ADMIN, FakeLLM, adm, h

sync_api = pytest.importorskip("playwright.sync_api")

# Lets the test cut the viewer's socket and keep it from reconnecting until released.
INIT = """
window.__socks = []; window.__hold = false;
const Real = window.WebSocket;
window.WebSocket = function (url, ...a) {
  const s = new Real(window.__hold ? 'ws://127.0.0.1:1/' : url, ...a);
  window.__socks.push(s); return s;
};
"""


@pytest.fixture
def hub(tmp_path):
    s = Settings(db_path=str(tmp_path / "hub.db"), admin_token=ADMIN, litellm_url="http://x")
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(s, FakeLLM()), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "the hub did not start within 10 seconds"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(10)


@pytest.fixture
def page(hub):
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except sync_api.Error as e:
            if os.environ.get("CI"):  # a missing browser must fail CI, not pass it silently
                raise
            pytest.skip(f"playwright browser not installed: {str(e).splitlines()[0]}")
        pg = browser.new_page()
        pg.add_init_script(INIT)
        yield pg
        browser.close()


def test_viewer_live_and_reconnect(hub, page):
    key = httpx.post(hub + "/api/admin/agents", json={"name": "alice"}, headers=adm()).json()["key"]

    def agent_says(text):
        r = httpx.post(hub + "/api/v1/post", json={"room": "live", "text": text}, headers=h(key))
        assert r.status_code == 200, r.text

    agent_says("first")  # creates the room
    page.goto(hub)
    page.click("#modeToggle")
    page.fill("#tokenIn", ADMIN)
    page.press("#tokenIn", "Enter")
    page.click('#rooms [data-room="live"]')
    page.wait_for_selector("#feed .msg >> text=first")
    page.wait_for_selector("#liveText >> text=live")

    page.fill("#text", "hello from the viewer")
    page.press("#text", "Enter")
    page.wait_for_selector("#feed .msg:not(.pending) >> text=hello from the viewer")

    agent_says("pushed to an open page")  # no reload
    page.wait_for_selector("#feed .msg >> text=pushed to an open page", timeout=5000)

    page.evaluate("window.__hold = true; window.__socks.forEach(s => s.close())")
    page.wait_for_selector("#liveText >> text=reconnecting")
    agent_says("posted while offline")
    assert page.locator("#feed .msg", has_text="posted while offline").count() == 0
    page.evaluate("window.__hold = false")
    page.wait_for_selector("#feed .msg >> text=posted while offline", timeout=30000)
    page.wait_for_selector("#liveText >> text=live")


def test_unread_survives_reload_and_reconnect(hub, page):
    key = httpx.post(hub + "/api/admin/agents", json={"name": "alice"}, headers=adm()).json()["key"]

    def agent_says(room, text):
        r = httpx.post(hub + "/api/v1/post", json={"room": room, "text": text}, headers=h(key))
        assert r.status_code == 200, r.text

    def badge(n):
        page.wait_for_selector(f'#rooms [data-room="other"] .badge:text-is("{n}")', timeout=30000)

    agent_says("live", "first")
    agent_says("other", "before")
    page.goto(hub)
    page.click("#modeToggle")
    page.fill("#tokenIn", ADMIN)
    page.press("#tokenIn", "Enter")
    page.click('#rooms [data-room="live"]')
    page.wait_for_selector("#liveText >> text=live")
    badge(1)

    agent_says("other", "while live is open")
    badge(2)
    page.reload()  # the hub keeps the counts, not the page
    page.wait_for_selector("#liveText >> text=live")
    badge(2)

    page.evaluate("window.__hold = true; window.__socks.forEach(s => s.close())")
    page.wait_for_selector("#liveText >> text=reconnecting")
    agent_says("other", "posted while offline")
    page.evaluate("window.__hold = false")
    badge(3)  # a room that isn't open catches up too, not only the open one

    page.click('#rooms [data-room="other"]')
    page.wait_for_selector("#feed .msg >> text=posted while offline")
    page.click('#rooms [data-room="live"]')
    page.wait_for_selector("#feed .msg >> text=first")
    page.reload()  # opening the room reported it as read
    page.wait_for_selector("#liveText >> text=live")
    page.wait_for_selector('#rooms [data-room="other"]')
    assert page.locator('#rooms [data-room="other"] .badge').count() == 0
