from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import monotonic

import httpx
import pytest
from a13n_harness_ui.app import open_harness_ui_app
from a13n_harness_ui.browser import BrowserFrame, BrowserInput, HostBrowsers, browser_url
from a13n_harness_ui.errors import HarnessUiError
from a13n_harness_ui.model_runtime import HarnessUiModelResolver
from a13n_harness_ui.surfaces import NewThreadDefaults
from a13n_harness_ui.webui import create_webui
from pydantic_ai.models.function import FunctionModel

from .test_app import _settings, _write_configuration

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "javascript:alert(1)", "https://user:pass@site.test", "https://site.test/\n"]
)
async def test_browser_rejects_non_web_navigation(url):
    with pytest.raises(HarnessUiError, match="HTTP"):
        browser_url(url)
    assert browser_url("http://127.0.0.1:8000/test") == "http://127.0.0.1:8000/test"


async def test_enable_is_lazy_and_old_bindings_fail_after_stop():
    async def authorize(thread_id):
        assert thread_id == "thread-a"

    browsers = HostBrowsers(available=True, authorize=authorize)
    assert browsers.state("thread-a").status == "disabled"
    enabled = await browsers.enable("thread-a")
    assert browsers.sessions["thread-a"].browser is None
    await browsers.stop("thread-a", enabled.session_id)
    new = await browsers.enable("thread-a")
    assert new.session_id != enabled.session_id
    with pytest.raises(HarnessUiError) as error:
        await browsers.perform("thread-a", enabled.session_id, "click", value="button")
    assert error.value.code == "browser_session_conflict"
    assert browsers.sessions["thread-a"].browser is None
    await browsers.close()


async def test_queued_actions_recheck_authority_without_dispatch():
    allowed = True

    async def authorize(thread_id):
        if not allowed:
            raise HarnessUiError("Permission changed", code="browser_disabled")

    browsers = HostBrowsers(available=True, authorize=authorize)
    state = await browsers.enable("thread-a")
    session = browsers.sessions["thread-a"]
    async with session.lock:
        pending = asyncio.create_task(browsers.perform("thread-a", state.session_id, "click", value="button"))
        await asyncio.sleep(0)
        allowed = False
    with pytest.raises(HarnessUiError, match="Permission changed"):
        await pending
    assert session.browser is None
    await browsers.close()


async def test_http_access_and_root_agent_opt_in(tmp_path, monkeypatch):
    root = _write_configuration(tmp_path)
    observed = []

    async def model(messages, info):
        observed.append({tool.name for tool in info.function_tools})
        yield "Ready."

    async def resolve(self, context, model_id):
        return FunctionModel(stream_function=model)

    monkeypatch.setattr(HarnessUiModelResolver, "__call__", resolve)
    async with open_harness_ui_app(
        _settings(tmp_path / "state"), configuration_path=root, host_mode="webui", share_computer=True
    ) as app:

        @asynccontextmanager
        async def borrowed():
            yield app

        assets = tmp_path / "static"
        assets.mkdir()
        (assets / "browser-viewer.html").write_text("<p>Browser viewer fixture</p>")
        server = create_webui(borrowed, api_key="test-key", static_root=assets)
        async with (
            server.router.lifespan_context(server),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://127.0.0.1") as client,
        ):
            thread = await app.create_thread()
            path = f"/api/threads/{thread.thread_id}/browser"
            assert (await client.post(path + "/enable")).status_code == 401
            client.headers["Authorization"] = "Bearer test-key"
            assert (await client.post(path + "/enable", headers={"Origin": "https://evil.test"})).status_code == 403
            assert (await client.get(path)).json()["enabled"] is False
            receipt = await app.submit_thread(thread_id=thread.thread_id, prompt="Before enablement")
            assert (await app.wait_root_operation(receipt.receipt_id, timeout_seconds=20)).status.value == "completed"
            assert "browser_navigate" not in observed[-1]
            enabled = (await client.post(path + "/enable")).json()
            assert enabled["status"] == "stopped"
            receipt = await app.submit_thread(thread_id=thread.thread_id, prompt="After enablement")
            assert (await app.wait_root_operation(receipt.receipt_id, timeout_seconds=20)).status.value == "completed"
            assert "browser_navigate" in observed[-1]
            assert "browser_screenshot" in observed[-1]
            sandbox = await app.create_thread(defaults=NewThreadDefaults(environment_profile_id="environment-sandbox"))
            denied = await client.post(f"/api/threads/{sandbox.thread_id}/browser/enable")
            assert denied.json()["error"]["code"] == "browser_full_control_required"
            presentation = await client.get(path + "/presentation")
            assert presentation.status_code == 200
            # Opening the viewer grants no third-party App enablement.
            assert (await client.post(f"/api/threads/{thread.thread_id}/apps/open", json={})).json()["error"][
                "code"
            ] == "mcp_apps_disabled"
            await client.post(path + "/stop", params={"session_id": enabled["session_id"]})
            assert (await client.get(path + "/frame", params={"session_id": enabled["session_id"]})).json()["error"][
                "code"
            ] == "browser_session_conflict"


@pytest.mark.skipif(os.environ.get("A13N_BROWSER_REAL_TEST") != "1", reason="Optional real Chromium validation")
async def test_real_browser_input_observation_and_fresh_frame(record_property):
    class PageHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<title>Browser fixture</title><label>Name <input id=\"name\"></label><button onclick=\"document.querySelector('output').textContent='Hello '+document.querySelector('input').value\">Greet</button><output></output>"
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()

    async def authorize(thread_id):
        assert thread_id == "thread-real"

    browsers = HostBrowsers(available=True, authorize=authorize)
    try:
        state = await browsers.enable("thread-real")
        started = monotonic()
        await browsers.perform(
            "thread-real", state.session_id, "navigate", value=f"http://127.0.0.1:{server.server_port}"
        )
        record_property("startup_seconds", monotonic() - started)
        await browsers.perform("thread-real", state.session_id, "fill", value="#name", text="Harness")
        await browsers.perform("thread-real", state.session_id, "click", value="button")
        observed = await browsers.perform("thread-real", state.session_id, "observe")
        assert "Hello Harness" in observed
        started = monotonic()
        frame = await browsers.perform("thread-real", state.session_id, "screenshot")
        record_property("frame_seconds", monotonic() - started)
        assert isinstance(frame, BrowserFrame)
        assert frame.state.title == "Browser fixture"
        assert frame.image.startswith("/9j/")
        dom = await browsers.dom("thread-real", state.session_id)
        assert any(isinstance(event, dict) and event["type"] == 2 for event in dom.events)

        def find_node(value, element_id):
            if isinstance(value, dict):
                if value.get("attributes", {}).get("id") == element_id:
                    return value["id"]
                for child in value.values():
                    if (found := find_node(child, element_id)) is not None:
                        return found
            if isinstance(value, (tuple, list)):
                for child in value:
                    if (found := find_node(child, element_id)) is not None:
                        return found
            return None

        input_id = find_node(dom.events, "name")
        assert input_id is not None
        await browsers.input(
            "thread-real",
            BrowserInput(session_id=state.session_id, epoch=dom.epoch, node_id=input_id, action="fill", text="Human"),
        )
        # Human and Agent inputs address the same live page and serialize through one lane.
        await browsers.perform("thread-real", state.session_id, "click", value="button")
        assert "Hello Human" in await browsers.perform("thread-real", state.session_id, "observe")
        changed = await browsers.dom("thread-real", state.session_id, epoch=dom.epoch, after=dom.cursor)
        assert changed.cursor > dom.cursor
        with pytest.raises(HarnessUiError) as conflict:
            await browsers.input(
                "thread-real",
                BrowserInput(session_id=state.session_id, epoch="old-document", node_id=input_id, action="click"),
            )
        assert conflict.value.code == "browser_dom_conflict"
        import psutil

        record_property(
            "browser_tree_rss_mib",
            sum(process.memory_info().rss for process in psutil.Process().children(recursive=True)) / (1024 * 1024),
        )
        await browsers.stop("thread-real", state.session_id)
        with pytest.raises(HarnessUiError):
            await browsers.perform("thread-real", state.session_id, "click", value="button")
    finally:
        await browsers.close()
        server.shutdown()
        server.server_close()
        worker.join()


async def test_trusted_dom_viewer_uses_exact_script_hash_without_weakening_apps():
    from a13n_harness_ui.mcp_apps.sandbox import create_sandbox

    server = create_sandbox(browser_viewer="<main>Viewer</main><script>window.viewer=true;</script>")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://127.0.0.1:9001") as client:
        response = await client.get("/browser-viewer.html", params={"host": "http://127.0.0.1:9000"})
        assert response.status_code == 200
        policy = response.headers["content-security-policy"]
        script_policy = next(part for part in policy.split(";") if "script-src" in part)
        assert "'sha256-" in script_policy
        assert "unsafe-inline" not in script_policy
        assert "unsafe-eval" not in script_policy
        assert "connect-src 'none'" in policy
        assert (await client.get("/browser-viewer.html", params={"host": "javascript:alert(1)"})).status_code == 400
        ordinary = await client.get("/sandbox.html", params={"host": "http://127.0.0.1:9000"})
        assert 'view.setAttribute("sandbox", "allow-scripts");' in ordinary.text
