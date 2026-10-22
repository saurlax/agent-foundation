from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from a13n_harness.capabilities import BrowserUseCapability
from a13n_harness.capabilities.browser import BrowserError
from a13n_harness.capabilities.browser.cdp import CdpBrowserBackend, Chromium, Input, Page, websocket_url
from pydantic import ValidationError

pytestmark = pytest.mark.anyio


@pytest.fixture
def browser():
    client = Mock()
    client.pending_requests = {}
    client.send_raw = AsyncMock(return_value={})
    client.stop = AsyncMock()
    return Chromium(client)


async def test_standalone_backend_supplies_capability_tools_without_ui(browser):
    page = Page("page-one", "session-one")
    browser.pages[page.target] = page

    async def send(method, params, session):
        assert session == page.session
        if method == "Accessibility.getFullAXTree":
            return {"nodes": [{"role": {"value": "button"}, "name": {"value": "Continue"}, "backendDOMNodeId": 11}]}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "node-one"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": {"x": 20, "y": 30, "type": "button"}}}
        return {}

    browser.client.send_raw.side_effect = send
    tools = BrowserUseCapability(CdpBrowserBackend(browser, page)).get_toolset().tools
    observation = await tools["browser_observe"].function_schema.call({}, None)
    assert "[e1] Continue" in observation
    assert "completed" in await tools["browser_click"].function_schema.call({"ref": "e1"}, None)
    mutations = [
        call.args for call in browser.client.send_raw.await_args_list if call.args[0] == "Input.dispatchMouseEvent"
    ]
    assert [args[1]["type"] for args in mutations] == ["mouseMoved", "mousePressed", "mouseReleased"]
    browser.client.send_raw.assert_any_await("Runtime.releaseObject", {"objectId": "node-one"}, page.session)


async def test_direct_connection_closes_only_its_page_on_exception(monkeypatch):
    browser = AsyncMock()
    page = Page("owned", "session")
    browser.pages = {page.target: page, "existing": Page("existing", "other-session")}
    browser.create.return_value = page
    connect = AsyncMock(return_value=browser)
    monkeypatch.setattr(Chromium, "connect", connect)
    with pytest.raises(ValueError, match="run failed"):
        async with CdpBrowserBackend.connect("ws://localhost:9222/devtools/browser/test") as backend:
            assert backend.page is page and backend.browser is browser
            raise ValueError("run failed")
    browser.close_page.assert_awaited_once_with(page)
    browser.close.assert_awaited_once()
    browser.close_browser.assert_not_awaited()


async def test_cdp_discovery_accepts_http_base_urls_and_preserves_query(monkeypatch):
    original = httpx2.AsyncClient
    urls = []

    def response(request):
        urls.append(str(request.url))
        return httpx2.Response(200, json={"webSocketDebuggerUrl": "wss://browser.example/devtools/browser/test"})

    monkeypatch.setattr(
        httpx2, "AsyncClient", lambda **kwargs: original(transport=httpx2.MockTransport(response), **kwargs)
    )
    assert (
        await websocket_url("https://browser.example/cdp?token=example")
        == "wss://browser.example/devtools/browser/test"
    )
    assert urls == ["https://browser.example/cdp/json/version?token=example"]
    assert (
        await websocket_url("ws://localhost:9222/devtools/browser/test") == "ws://localhost:9222/devtools/browser/test"
    )


async def test_discovery_failure_is_a_browser_error(monkeypatch):
    original = httpx2.AsyncClient
    monkeypatch.setattr(
        httpx2,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={})), **kwargs
        ),
    )
    with pytest.raises(BrowserError, match="Cannot discover"):
        await websocket_url("http://localhost:9222")
    with pytest.raises(BrowserError, match="CDP base URL"):
        await websocket_url("file:///tmp/browser")


async def test_shared_actions_enforce_handback_and_url_safety(browser):
    page = Page("one", "session", requires_observation=True)
    with pytest.raises(BrowserError, match="Observe after handback"):
        await browser.act(page, "press", value="Enter")
    page.requires_observation = False
    with pytest.raises(BrowserError, match="HTTP"):
        await browser.input(page, Input(type="navigate", text="file:///etc/passwd"))
    with pytest.raises(BrowserError, match="Unsupported"):
        await browser.act(page, "press", value="Unknown")
    browser.client.send_raw.assert_not_awaited()
    with pytest.raises(ValidationError):
        Input.model_validate({"type": "Runtime.evaluate", "expression": "arbitrary()"})


async def test_screencast_ack_is_independent_of_viewer_consumption(browser):
    page = Page("one", "session")
    browser.pages[page.target] = page
    delivered = []
    browser.frame = lambda current, frame: delivered.append((current, frame))
    frame = {"sessionId": 42, "data": "jpeg"}
    browser._frame(frame, page.session)
    await asyncio.sleep(0)
    assert delivered == [(page, frame)]
    browser.client.send_raw.assert_awaited_once_with("Page.screencastFrameAck", {"sessionId": 42}, page.session)
    await browser.close()


async def test_failed_stream_start_restores_focus_emulation(browser):
    page = Page("one", "session")

    async def send(method, params, session):
        if method == "Page.startScreencast":
            raise RuntimeError("capture unavailable")
        return {}

    browser.client.send_raw.side_effect = send
    with pytest.raises(BrowserError):
        await browser.screencast(page, True)
    browser.client.send_raw.assert_any_await("Emulation.setFocusEmulationEnabled", {"enabled": False}, page.session)


async def test_human_navigation_does_not_block_input_until_document_loads(browser):
    page = Page("one", "session")
    browser.client.send_raw.return_value = {"loaderId": "still-loading"}
    await asyncio.wait_for(browser.input(page, Input(type="navigate", text="https://example.com/")), 1)
    assert not page.loaded.is_set()
    agent_navigation = asyncio.create_task(browser.navigate(page, "https://example.com/agent"))
    await asyncio.sleep(0)
    assert not agent_navigation.done()
    page.loaded.set()
    await asyncio.wait_for(agent_navigation, 1)
