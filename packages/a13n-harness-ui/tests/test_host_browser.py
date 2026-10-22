from __future__ import annotations

import asyncio
import base64
import json
import time
from unittest.mock import AsyncMock

import pytest
from a13n_harness.capabilities.browser.cdp import Page
from a13n_harness_ui.browser import (
    Binding,
    BrowserAccessRequest,
    BrowserInput,
    BrowserViewerInput,
    BrowserViewportRequest,
    HostBrowsers,
    InputBuffer,
    Viewer,
)
from a13n_harness_ui.errors import HarnessUiError

pytestmark = pytest.mark.anyio


@pytest.fixture
def browsers(tmp_path):
    host = HostBrowsers(root=tmp_path, available=True, authorize=AsyncMock())
    host.runtime = AsyncMock()
    for name in ("thread-a", "thread-b"):
        page = Page(name, "session-" + name)
        host.bindings[name] = Binding(status="ready", page=page)
        host.owners[page.target] = name
    host.runtime.pages = {item.page.target: item.page for item in host.bindings.values()}
    host.runtime.screenshot.return_value = b"jpeg"
    host.runtime.input.return_value = None
    return host


async def test_readonly_admission_cannot_inject_input_and_ticket_is_single_use(browsers):
    identity = browsers.state("thread-a").session_id
    access = await browsers.access("thread-a", BrowserAccessRequest(session_id=identity))
    async with browsers.viewer("thread-a", access.ticket) as viewer:
        with pytest.raises(HarnessUiError, match="Take control"):
            await browsers.input(viewer, BrowserInput(type="mousePressed", x=100, y=100))
        browsers.runtime.send.assert_not_awaited()
    with pytest.raises(HarnessUiError, match="expired"):
        async with browsers.viewer("thread-a", access.ticket):
            pass


async def test_tickets_reject_other_thread_and_expiration(browsers):
    identity = browsers.state("thread-a").session_id
    access = await browsers.access("thread-a", BrowserAccessRequest(session_id=identity))
    with pytest.raises(HarnessUiError, match="expired"):
        async with browsers.viewer("thread-b", access.ticket):
            pass
    access = await browsers.access("thread-a", BrowserAccessRequest(session_id=identity))
    claim = browsers.tickets[access.ticket]
    browsers.tickets[access.ticket] = (*claim[:2], time.monotonic() - 1)
    with pytest.raises(HarnessUiError, match="expired"):
        async with browsers.viewer("thread-a", access.ticket):
            pass


async def test_control_changes_preserve_viewers_and_reject_old_input_authority(browsers):
    identity = browsers.state("thread-a").session_id
    access = await browsers.access("thread-a", BrowserAccessRequest(session_id=identity))
    other_access = await browsers.access(
        "thread-b", BrowserAccessRequest(session_id=browsers.state("thread-b").session_id)
    )
    async with (
        browsers.viewer("thread-a", access.ticket) as viewer,
        browsers.viewer("thread-b", other_access.ticket) as other,
    ):
        request = BrowserInput(type="mousePressed", x=123, y=45, button="left")
        control = await browsers.control("thread-a", identity, human=True)
        assert not viewer.closed.is_set() and not other.closed.is_set()
        with pytest.raises(HarnessUiError, match="Take control"):
            await browsers.input(other, request, control_token=control.control_token)
        await browsers.input(viewer, request, control_token=control.control_token)
        browsers.runtime.input.assert_awaited_once_with(browsers.bindings["thread-a"].page, request)
        await browsers.control("thread-a", identity, human=False)
        assert not viewer.closed.is_set() and not other.closed.is_set()
        with pytest.raises(HarnessUiError, match="Take control"):
            await browsers.input(viewer, request, control_token=control.control_token)
        again = await browsers.control("thread-a", identity, human=True)
        with pytest.raises(HarnessUiError, match="Take control"):
            await browsers.input(viewer, request, control_token=control.control_token)
        await browsers.input(viewer, request, control_token=again.control_token)
        assert all(page.requires_observation for page in browsers.runtime.pages.values())
        assert browsers.runtime.screencast.await_count == 2  # One start per page, no restart.
    assert browsers.human_thread == "thread-a" and not browsers.control_token


async def test_takeover_settles_operations_without_waiting_for_the_entire_agent_task(browsers):
    finished = asyncio.Event()
    release = asyncio.Event()

    async def agent():
        backend = browsers.backend("thread-b", browsers.state("thread-b").session_id)
        async with backend.operation():
            await release.wait()
        finished.set()
        await asyncio.Event().wait()

    agent_task = asyncio.create_task(agent())
    await asyncio.sleep(0)
    control = asyncio.create_task(browsers.control("thread-a", browsers.state("thread-a").session_id, human=True))
    await asyncio.sleep(0)
    assert not control.done()
    release.set()
    await asyncio.wait_for(control, 1)
    assert finished.is_set() and not agent_task.done()
    agent_task.cancel()
    await asyncio.gather(agent_task, return_exceptions=True)


async def test_stop_retires_only_its_thread_and_preserves_shared_runtime(browsers):
    first, other = browsers.state("thread-a"), browsers.state("thread-b")
    runtime = browsers.runtime
    page = browsers.bindings["thread-a"].page
    stopped = await browsers.stop("thread-a", first.session_id)
    assert stopped.status == "stopped" and stopped.session_id != first.session_id
    assert browsers.runtime is runtime
    assert browsers.state("thread-b") == other
    runtime.close_page.assert_awaited_once_with(page)
    with pytest.raises(HarnessUiError, match="session changed"):
        browsers.checked("thread-a", first.session_id)


async def test_page_operations_in_distinct_threads_can_overlap(browsers):
    async with browsers.backend("thread-a", browsers.state("thread-a").session_id).operation():
        async with asyncio.timeout(1):
            async with browsers.backend("thread-b", browsers.state("thread-b").session_id).operation():
                assert len(browsers.operations) == 2


async def test_handback_waiter_wakes_on_local_stop_without_replaying(browsers):
    identity = browsers.state("thread-b").session_id
    await browsers.control("thread-a", browsers.state("thread-a").session_id, human=True)
    pending = asyncio.create_task(browsers.backend("thread-b", identity).wait_for_handback())
    await asyncio.sleep(0)
    await browsers.stop("thread-b", identity)
    with pytest.raises(HarnessUiError, match="session changed"):
        await asyncio.wait_for(pending, 1)
    assert browsers.human_thread == "thread-a"


def test_frame_delivery_retains_only_the_latest_repaint():
    viewer = Viewer("thread-a", "binding")
    for sequence in range(100):
        viewer.publish({"sequence": sequence})
    assert viewer.frames.qsize() == 1
    assert viewer.frames.get_nowait() == {"sequence": 99}


async def test_websocket_streams_successive_frames_without_client_acknowledgements(browsers, tmp_path, monkeypatch):
    from websockets.asyncio.client import connect

    from .test_interactive_protocol import listener_with_app

    async with listener_with_app(tmp_path) as (http, ws, app):
        monkeypatch.setattr(app, "browsers", lambda: browsers)
        identity = browsers.state("thread-a").session_id
        access = await browsers.access("thread-a", BrowserAccessRequest(session_id=identity))
        async with connect(
            ws + f"/api/threads/thread-a/browser/connect?ticket={access.ticket}", origin=http, proxy=None
        ) as client:
            async with asyncio.timeout(2):
                assert await client.recv() == b"jpeg"
                page = browsers.bindings["thread-a"].page
                browsers.frame(page, {"data": base64.b64encode(b"next").decode(), "metadata": {}})
                assert await client.recv() == b"next"
                control = await browsers.control("thread-a", identity, human=True)
                await client.send(
                    json.dumps({"control_token": control.control_token, "input": {"type": "text", "text": "allowed"}})
                )
                async with asyncio.timeout(2):
                    while not browsers.runtime.input.await_count:
                        await asyncio.sleep(0)
                await browsers.control("thread-a", identity, human=False)
                await client.send(
                    json.dumps({"control_token": control.control_token, "input": {"type": "text", "text": "blocked"}})
                )
                assert json.loads(await client.recv())["type"] == "input_rejected"
                browsers.frame(page, {"data": base64.b64encode(b"after-handback").decode(), "metadata": {}})
                assert await client.recv() == b"after-handback"
                assert browsers.runtime.input.await_count == 1


@pytest.mark.parametrize(
    ("preset", "ratio", "expected"),
    [
        ("desktop", "16:9", (1280, 720)),
        ("phone", "native", (390, 844)),
        ("wide", "9:16", (1080, 1920)),
        ("tablet", "1:1", (768, 768)),
    ],
)
async def test_aspect_ratio_changes_require_control_and_keep_page_coordinates_unscaled(
    browsers, preset, ratio, expected
):
    identity = browsers.state("thread-a").session_id
    with pytest.raises(HarnessUiError):
        await browsers.resize(
            "thread-a", BrowserViewportRequest(session_id=identity, preset=preset, aspect_ratio=ratio)
        )
    control = await browsers.control("thread-a", identity, human=True)
    page = browsers.bindings["thread-a"].page
    result = await browsers.resize(
        "thread-a",
        BrowserViewportRequest(
            session_id=identity, control_token=control.control_token, preset=preset, aspect_ratio=ratio
        ),
    )
    browsers.runtime.resize.assert_awaited_once_with(page, *expected)
    assert result.viewport == preset and result.aspect_ratio == ratio
    assert page.requires_observation
    stopped = await browsers.stop("thread-a", identity)
    assert stopped.viewport == preset and stopped.aspect_ratio == ratio


async def test_input_motion_is_coalesced_without_reordering_buttons_keys_or_authority():
    queue = InputBuffer()

    def request(kind, token="controller", **kwargs):
        return BrowserViewerInput(control_token=token, input=BrowserInput(type=kind, **kwargs))

    await queue.put(request("mousePressed", button="left", buttons=1))
    for x in range(100):
        await queue.put(request("mouseMoved", button="left", buttons=1, x=x))
    await queue.put(request("mouseReleased", button="left"))
    await queue.put(request("keyDown", key="a"))
    await queue.put(request("mouseMoved", x=110))
    await queue.put(request("mouseMoved", "new-controller", x=120))
    values = [(await queue.get()) for _ in range(6)]
    assert [value.input.type for value in values] == [
        "mousePressed",
        "mouseMoved",
        "mouseReleased",
        "keyDown",
        "mouseMoved",
        "mouseMoved",
    ]
    assert values[1].input.x == 99 and values[1].input.buttons == 1
    assert values[-1].control_token == "new-controller"
    await queue.put(request("mouseWheel", delta_y=80))
    await queue.put(request("mouseWheel", delta_y=120))
    await queue.put(request("mouseWheel", modifiers=2, delta_y=100))
    assert (await queue.get()).input.delta_y == 200
    assert (await queue.get()).input.modifiers == 2


async def test_input_barrier_queue_is_bounded_and_backpressures_without_dropping_keys():
    queue = InputBuffer()
    for _ in range(64):
        await queue.put(BrowserViewerInput(input=BrowserInput(type="keyDown", key="a")))
    pending = asyncio.create_task(queue.put(BrowserViewerInput(input=BrowserInput(type="keyUp", key="a"))))
    await asyncio.sleep(0)
    assert not pending.done()
    assert (await queue.get()).input.type == "keyDown"
    await asyncio.wait_for(pending, 1)
    assert [(await queue.get()).input.type for _ in range(64)] == ["keyDown"] * 63 + ["keyUp"]
