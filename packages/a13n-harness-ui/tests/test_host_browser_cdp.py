"""Opt-in real Chromium coverage; no Playwright or display/VNC server is used."""

from __future__ import annotations

import asyncio
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import AsyncMock

import pytest
from a13n_harness_ui.browser import BrowserAccessRequest, BrowserInput, BrowserViewportRequest, HostBrowsers
from a13n_harness_ui.configuration.models import BrowserConfiguration
from a13n_harness_ui.errors import HarnessUiError

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(os.environ.get("A13N_BROWSER_REAL_TEST") != "1", reason="Opt-in real CDP browser test"),
]
HTML = b"""<!doctype html><title>CDP fixture</title><input aria-label="Message"><button onclick="document.querySelector('output').textContent='clicked'">Submit</button><button onclick="window.open('/popup')">Popup</button><output></output><canvas width="320" height="180"></canvas><script>const c=document.querySelector('canvas').getContext('2d');let x=0;function animate(){c.fillStyle='navy';c.fillRect(0,0,320,180);c.fillStyle='lime';c.fillRect((x++%280),40,40,40);requestAnimationFrame(animate)}animate()</script>"""


@pytest.fixture(autouse=True)
def isolated_user_home():
    # This explicitly opted-in native integration keeps the OS user identity
    # (including platform browser services) and isolates the browser profile.
    yield


@pytest.fixture
async def website(request):
    body = getattr(request, "param", HTML)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        await asyncio.to_thread(thread.join)


async def test_real_cdp_stream_input_popup_and_independent_lifecycle(website):
    executable = os.environ["A13N_BROWSER_EXECUTABLE"]
    # Keep native browser test profiles separate from pytest artifact paths.
    profile = TemporaryDirectory(prefix="cdp-")
    browsers = HostBrowsers(
        root=Path(profile.name),
        available=True,
        authorize=AsyncMock(),
        configuration=BrowserConfiguration(executable=executable),
    )
    try:
        state = await browsers.enable("thread-a")
        backend = browsers.backend("thread-a", state.session_id)
        await backend.act("navigate", value=website)
        runtime = browsers.runtime
        assert runtime
        first = browsers.bindings["thread-a"].page
        assert first
        observation = await backend.observe()
        textbox = next(element for element in observation.elements if element.description == "Message")
        await backend.act("fill", target=textbox.target, value="native CDP", revision=observation.revision)
        assert await runtime.evaluate(first, "document.querySelector('input').value") == "native CDP"
        access = await browsers.access("thread-a", BrowserAccessRequest(session_id=state.session_id))
        async with browsers.viewer("thread-a", access.ticket) as viewer:
            frames = []
            async with asyncio.timeout(5):
                while len(frames) < 8:
                    frames.append(await viewer.frames.get())
            assert len({frame["data"] for frame in frames}) > 2
            with pytest.raises(HarnessUiError):
                await browsers.input(viewer, BrowserInput(type="text", text="readonly"))
        other = await browsers.enable("thread-b")
        await browsers.backend("thread-b", other.session_id).act("navigate", value=website)
        second = browsers.bindings["thread-b"].page
        assert second and second is not first
        assert browsers.runtime is runtime
        control = await browsers.control("thread-a", state.session_id, human=True)
        await browsers.resize(
            "thread-a",
            BrowserViewportRequest(
                session_id=state.session_id, control_token=control.control_token, preset="tablet", aspect_ratio="1:1"
            ),
        )
        assert await runtime.evaluate(first, "[innerWidth,innerHeight]") == [768, 768]
        access = await browsers.access("thread-a", BrowserAccessRequest(session_id=state.session_id))
        async with browsers.viewer("thread-a", access.ticket) as viewer:
            point = await runtime.evaluate(
                first,
                "(()=>{const r=document.querySelector('button').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()",
            )
            for kind in ("mousePressed", "mouseReleased"):
                await browsers.input(
                    viewer,
                    BrowserInput(type=kind, **point, button="left", buttons=1 if kind == "mousePressed" else 0),
                    control_token=control.control_token,
                )
            assert await runtime.evaluate(first, "document.querySelector('output').textContent") == "clicked"
            point = await runtime.evaluate(
                first,
                "(()=>{const r=document.querySelectorAll('button')[1].getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()",
            )
            for kind in ("mousePressed", "mouseReleased"):
                await browsers.input(
                    viewer,
                    BrowserInput(type=kind, **point, button="left", buttons=1 if kind == "mousePressed" else 0),
                    control_token=control.control_token,
                )
            async with asyncio.timeout(5):
                while browsers.bindings["thread-a"].page is first:
                    await asyncio.sleep(0.025)
            popup = browsers.bindings["thread-a"].page
            assert popup and browsers.owners[popup.target] == "thread-a"
            async with asyncio.timeout(5):
                while await runtime.evaluate(popup, "[innerWidth,innerHeight]") != [768, 768]:
                    await asyncio.sleep(0.025)
            async with asyncio.timeout(5):
                while (await viewer.frames.get()).get("target") != popup.target:
                    pass
        await browsers.control("thread-a", state.session_id, human=False)
        reset = await browsers.reset("thread-a", state.session_id)
        assert reset.session_id != state.session_id and browsers.runtime is runtime
        assert reset.aspect_ratio == "1:1"
        assert await runtime.evaluate(browsers.bindings["thread-a"].page, "[innerWidth,innerHeight]") == [768, 768]
        assert browsers.bindings["thread-b"].page is second
        assert await runtime.evaluate(second, "document.title") == "CDP fixture"
    finally:
        await browsers.close()
        profile.cleanup()


INPUT_HTML = b"""<!doctype html><title>Input fixture</title>
<style>input,textarea{display:block;margin:20px;width:500px}#drag{width:100px;height:80px;background:skyblue}#drop{margin-top:20px;width:400px;height:100px;background:lightgray}</style>
<input id="source" value="Hello CDP sample"><textarea id="target"></textarea>
<input id="password" type="password" value="fixture-password"><input id="range" type="range" value="50">
<div id="drag" draggable="true">Drag me</div><div id="drop" ondragover="event.preventDefault()" ondrop="event.preventDefault();this.dataset.result='dropped'">Drop here</div>
"""


@pytest.mark.parametrize("website", [INPUT_HTML], indirect=True)
async def test_real_drag_shortcuts_and_explicit_text_clipboard(website):
    with TemporaryDirectory(prefix="cdp-input-") as profile:
        browsers = HostBrowsers(
            root=Path(profile),
            available=True,
            authorize=AsyncMock(),
            configuration=BrowserConfiguration(executable=os.environ["A13N_BROWSER_EXECUTABLE"]),
        )
        try:
            state = await browsers.enable("thread-input")
            await browsers.backend("thread-input", state.session_id).act("navigate", value=website)
            runtime = browsers.runtime
            page = browsers.bindings["thread-input"].page
            assert runtime and page
            control = await browsers.control("thread-input", state.session_id, human=True)
            access = await browsers.access("thread-input", BrowserAccessRequest(session_id=state.session_id))
            async with browsers.viewer("thread-input", access.ticket) as viewer:

                async def send(**params):
                    return await browsers.input(viewer, BrowserInput(**params), control_token=control.control_token)

                async def point(selector):
                    return await runtime.evaluate(
                        page,
                        f"(()=>{{const r=document.querySelector('{selector}').getBoundingClientRect();return {{x:r.x+r.width/2,y:r.y+r.height/2}}}})()",
                    )

                async def click(selector):
                    position = await point(selector)
                    await send(type="mousePressed", **position, button="left", buttons=1)
                    await send(type="mouseReleased", **position, button="left")

                async def key(name, modifiers=4, code=None, number=None, text=""):
                    values = {
                        "key": name,
                        "code": code or "Key" + name.upper(),
                        "key_code": number or ord(name.upper()),
                        "modifiers": modifiers,
                    }
                    copied = await send(type="keyDown", **values, text=text)
                    await send(type="keyUp", **values)
                    return copied

                await click("#source")
                for modifiers in (2, 4):
                    await key("a", modifiers)
                    assert await runtime.evaluate(page, "[source.selectionStart,source.selectionEnd]") == [0, 16]
                assert await key("c") == "Hello CDP sample"
                assert await key("Insert", 2, "Insert", 45) == "Hello CDP sample"
                assert await key("x") == "Hello CDP sample"
                assert await runtime.evaluate(page, "source.value") == ""
                await key("z")
                assert await runtime.evaluate(page, "source.value") == "Hello CDP sample"
                await key("z", 12)
                assert await runtime.evaluate(page, "source.value") == ""
                await key("z", 2)
                await key("y", 2)
                assert await runtime.evaluate(page, "source.value") == ""
                await click("#target")
                await send(type="text", text="local paste sample")
                await key("a")
                assert await key("c") == "local paste sample"
                await key("ArrowRight", 0, "ArrowRight", 39)
                await key("Enter", 0, "Enter", 13, "\r")
                await key("Backspace", 0, "Backspace", 8)
                assert await runtime.evaluate(page, "target.value") == "local paste sample"
                await key("Tab", 0, "Tab", 9)
                assert await runtime.evaluate(page, "document.activeElement.id") == "password"
                await key("a")
                assert await key("c") is None
                start = await point("#range")
                end = {"x": start["x"] + 180, "y": start["y"]}
                await send(type="mousePressed", **start, button="left", buttons=1)
                await send(type="mouseMoved", **end, button="left", buttons=1)
                await send(type="mouseReleased", **end, button="left")
                assert int(await runtime.evaluate(page, "document.querySelector('#range').value")) > 80
                start, end = await point("#drag"), await point("#drop")
                await send(type="mousePressed", **start, button="left", buttons=1)
                for step in (0.1, 0.3, 0.6, 1):
                    await send(
                        type="mouseMoved",
                        x=start["x"] + (end["x"] - start["x"]) * step,
                        y=start["y"] + (end["y"] - start["y"]) * step,
                        button="left",
                        buttons=1,
                    )
                await send(type="mouseReleased", **end, button="left")
                assert await runtime.evaluate(page, "document.querySelector('#drop').dataset.result") == "dropped"
        finally:
            await browsers.close()


@pytest.mark.parametrize(
    "website",
    [
        b"<!doctype html><style>body{margin:0}div{width:300px;height:150px;background:red}div:hover{background:lime}</style><div>Hover</div>"
    ],
    indirect=True,
)
async def test_streamed_page_keeps_hover_rendering_when_another_target_opens(website):
    import base64
    from io import BytesIO

    from PIL import Image

    with TemporaryDirectory(prefix="cdp-hover-") as profile:
        browsers = HostBrowsers(
            root=Path(profile),
            available=True,
            authorize=AsyncMock(),
            configuration=BrowserConfiguration(executable=os.environ["A13N_BROWSER_EXECUTABLE"]),
        )
        try:
            state = await browsers.enable("thread-hover")
            await browsers.backend("thread-hover", state.session_id).act("navigate", value=website)
            runtime = browsers.runtime
            page = browsers.bindings["thread-hover"].page
            assert runtime and page
            access = await browsers.access("thread-hover", BrowserAccessRequest(session_id=state.session_id))
            async with browsers.viewer("thread-hover", access.ticket) as viewer:
                await runtime.create()
                assert await runtime.evaluate(page, "document.visibilityState") == "visible"
                control = await browsers.control("thread-hover", state.session_id, human=True)
                await browsers.input(
                    viewer, BrowserInput(type="mouseMoved", x=50, y=50), control_token=control.control_token
                )
                async with asyncio.timeout(2):
                    while True:
                        frame = await viewer.frames.get()
                        with Image.open(BytesIO(base64.b64decode(frame["data"]))) as picture:
                            red, green, _ = picture.convert("RGB").getpixel((20, 20))
                            if green > red:
                                break
                await browsers.control("thread-hover", state.session_id, human=False)
            assert await runtime.evaluate(page, "document.visibilityState") == "hidden"
        finally:
            await browsers.close()
