from __future__ import annotations

import asyncio
import os
import socket
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import uvicorn
from a13n_harness_ui.app import open_harness_ui_app
from a13n_harness_ui.browser import BrowserNavigation
from a13n_harness_ui.webui import create_webui
from playwright.async_api import async_playwright, expect

from .test_app import _settings, _write_configuration

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("A13N_BROWSER_REAL_TEST") != "1", reason="Optional real browser DOM viewer validation"
    ),
]


async def test_dom_viewer_forwards_input_and_blocks_recorded_site_scripts(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"""<title>DOM fixture</title><script>window.siteScriptRan=true;</script><p>Copy this page text.</p><label>Name<input id="name"></label><button onclick="document.querySelector('output').textContent='Hello '+document.querySelector('input').value">Greet</button><output>Ready</output>"""
            )

        def log_message(self, *args):
            pass

    site = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=site.serve_forever, daemon=True)
    worker.start()
    root = _write_configuration(tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    try:
        async with open_harness_ui_app(
            _settings(tmp_path / "state"), configuration_path=root, host_mode="webui", share_computer=True
        ) as app:
            thread = await app.create_thread()
            state = await app.enable_browser(thread.thread_id)
            await app.navigate_browser(
                thread.thread_id,
                BrowserNavigation(session_id=state.session_id, url=f"http://127.0.0.1:{site.server_port}/"),
            )

            @asynccontextmanager
            async def borrowed():
                yield app

            server = uvicorn.Server(
                uvicorn.Config(create_webui(borrowed, api_key="viewer-test"), log_level="warning", access_log=False)
            )
            running = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with async_playwright() as driver:
                    browser = await driver.chromium.launch(channel="chromium", headless=True)
                    try:
                        page = await browser.new_page(viewport={"width": 1400, "height": 900})
                        await page.goto(f"http://127.0.0.1:{port}/threads/{thread.thread_id}#api_key=viewer-test")
                        await page.get_by_role("button", name="Browser", exact=True).click()
                        viewer = page.frame_locator('iframe[title="Browser page"]').frame_locator("iframe")
                        await expect(viewer.get_by_text("Copy this page text.")).to_be_visible()
                        await viewer.get_by_label("Name").fill("Human")
                        await viewer.get_by_role("button", name="Greet", exact=True).click()
                        await expect(viewer.locator("output")).to_have_text("Hello Human", timeout=10000)
                        actual = await app.browsers().perform(thread.thread_id, state.session_id, "observe")
                        assert "Hello Human" in actual
                        # DOM text is real and site JavaScript has no second execution.
                        assert await viewer.locator("p").inner_text() == "Copy this page text."
                        assert (
                            await viewer.locator("body").evaluate(
                                "(node) => node.ownerDocument.defaultView.siteScriptRan"
                            )
                            is None
                        )
                        await page.get_by_role("button", name="Close browser viewer", exact=True).click()
                        assert (await app.browser_state(thread.thread_id)).status == "ready"
                    finally:
                        await browser.close()
            finally:
                server.should_exit = True
                await running
    finally:
        listener.close()
        site.shutdown()
        site.server_close()
        worker.join()
