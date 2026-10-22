"""Chromium page operations over cdp-use; the embedding Host owns the process."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx2
from a13n_logging import get_logger
from pydantic import BaseModel, ConfigDict, Field

from a13n_harness.capabilities.browser import BrowserAction, BrowserElement, BrowserError, BrowserObservation

_LOGGER = get_logger(__name__)


def validate_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise BrowserError("Use an HTTP(S) URL without embedded credentials.")
    return value


async def websocket_url(cdp_url: str) -> str:
    """Resolve a trusted Host endpoint; never expose it as an Agent tool input."""
    parsed = urlsplit(cdp_url)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname or parsed.fragment:
        raise BrowserError("Use an HTTP(S) CDP base URL or a WS(S) debugger URL.")
    if parsed.scheme in {"ws", "wss"}:
        return cdp_url
    try:
        async with httpx2.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(parsed._replace(path=parsed.path.rstrip("/") + "/json/version").geturl())
            response.raise_for_status()
            endpoint = response.json()["webSocketDebuggerUrl"]
        result = urlsplit(endpoint)
        if result.scheme not in {"ws", "wss"} or not result.hostname or result.fragment:
            raise ValueError("Invalid debugger URL")
        return endpoint
    except (httpx2.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise BrowserError("Cannot discover the browser CDP endpoint.") from exc


class Input(BaseModel):
    """Validated page input; the embedding Host must authorize every call."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    type: Literal[
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
        "mouseWheel",
        "keyDown",
        "keyUp",
        "text",
        "navigate",
        "back",
        "forward",
        "reload",
    ]
    x: float = Field(default=0, ge=0, le=4096)
    y: float = Field(default=0, ge=0, le=4096)
    button: Literal["none", "left", "middle", "right"] = "none"
    buttons: int = Field(default=0, ge=0, le=31)
    delta_x: float = Field(default=0, ge=-10000, le=10000)
    delta_y: float = Field(default=0, ge=-10000, le=10000)
    key: str = Field(default="", max_length=80)
    code: str = Field(default="", max_length=80)
    text: str = Field(default="", max_length=65536)
    modifiers: int = Field(default=0, ge=0, le=15)
    key_code: int = Field(default=0, ge=0, le=255)
    click_count: int = Field(default=1, ge=1, le=3)


@dataclass
class Page:
    target: str
    session: str
    url: str = "about:blank"
    revision: str = ""
    elements: dict[str, int] = field(default_factory=dict)
    requires_observation: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loaded: asyncio.Event = field(default_factory=asyncio.Event)

    def invalidate(self) -> None:
        self.revision = ""
        self.elements.clear()


class Chromium:
    """One browser connection, explicit page sessions and bounded protocol calls."""

    @classmethod
    async def connect(cls, endpoint: str) -> Chromium:
        from cdp_use import CDPClient

        client = CDPClient(await websocket_url(endpoint))
        try:
            await client.start()
            browser = cls(client)
            await browser.send("Target.setDiscoverTargets", {"discover": True})
            return browser
        except BaseException:
            await client.stop()
            raise

    def __init__(self, client: Any) -> None:
        self.client = client
        self.pages: dict[str, Page] = {}
        self.tasks: set[asyncio.Task] = set()
        self.frame: Callable[[Page, dict], None] = lambda page, frame: None
        self.opened: Callable[[dict], None] = lambda target: None
        self.closed: Callable[[str], None] = lambda target: None
        client.register.Page.screencastFrame(self._frame)
        client.register.Page.frameNavigated(self._navigated)
        client.register.Page.domContentEventFired(self._loaded)
        client.register.Page.navigatedWithinDocument(self._within_document)
        client.register.Target.targetCreated(lambda event, _: self.opened(event["targetInfo"]))
        client.register.Target.targetDestroyed(self._destroyed)

    def background(self, operation) -> None:
        task = asyncio.create_task(operation)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

        def completed(done: asyncio.Task) -> None:
            if not done.cancelled() and (error := done.exception()) is not None:
                _LOGGER.warning("Background CDP operation failed: %s", error)

        task.add_done_callback(completed)

    async def send(self, method: str, params: dict | None = None, page: Page | None = None) -> dict:
        try:
            async with asyncio.timeout(15):
                return await self.client.send_raw(method, params or {}, page.session if page else None)
        except (TimeoutError, RuntimeError, ConnectionError) as exc:
            raise BrowserError(
                "Browser operation failed; its outcome may be uncertain. Observe before retrying."
            ) from exc
        finally:
            # cdp-use keeps request futures until responses arrive. Timed-out
            # requests must not accumulate when a renderer stops responding.
            for identity, future in tuple(self.client.pending_requests.items()):
                if future.cancelled():
                    self.client.pending_requests.pop(identity, None)

    def _page(self, session: str | None) -> Page | None:
        return next((page for page in self.pages.values() if page.session == session), None)

    def _frame(self, event: dict, session: str | None) -> None:
        page = self._page(session)
        if page is not None:
            # Event handlers run on the CDP receiver. Never await a CDP reply
            # inside them: that reply needs the same receiver to make progress.
            self.background(self.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]}, page))
            self.frame(page, event)

    def _destroyed(self, event: dict, session: str | None) -> None:
        target = event["targetId"]
        self.pages.pop(target, None)
        self.closed(target)

    def _loaded(self, event: dict, session: str | None) -> None:
        if page := self._page(session):
            page.loaded.set()

    async def navigate(self, page: Page, url: str, *, wait: bool = True) -> None:
        page.loaded.clear()
        page.invalidate()
        result = await self.send("Page.navigate", {"url": url}, page)
        if result.get("errorText"):
            raise BrowserError("Navigation failed: " + result["errorText"])
        page.url = url
        if wait and "loaderId" in result:
            try:
                async with asyncio.timeout(25):
                    await page.loaded.wait()
            except TimeoutError as exc:
                raise BrowserError("Navigation did not settle. Observe before retrying.") from exc

    def _navigated(self, event: dict, session: str | None) -> None:
        page = self._page(session)
        frame = event["frame"]
        if page is not None and not frame.get("parentId"):
            page.url = frame["url"]
            page.invalidate()

    def _within_document(self, event: dict, session: str | None) -> None:
        if page := self._page(session):
            page.url = event["url"]
            page.invalidate()

    async def attach(self, target: str, url: str = "about:blank") -> Page:
        if target in self.pages:
            return self.pages[target]
        attached = await self.send("Target.attachToTarget", {"targetId": target, "flatten": True})
        page = Page(target, attached["sessionId"], url)
        self.pages[target] = page
        await self.send("Page.enable", page=page)
        await self.send("Runtime.enable", page=page)
        return page

    async def create(self) -> Page:
        target = await self.send("Target.createTarget", {"url": "about:blank"})
        return await self.attach(target["targetId"])

    async def resize(self, page: Page, width: int, height: int) -> None:
        await self.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": False,
                "screenWidth": width,
                "screenHeight": height,
            },
            page,
        )

    async def screencast(self, page: Page, enabled: bool) -> None:
        if enabled:
            # Background targets can accept pointer events but stop painting.
            # Emulate focus per streamed target instead of stealing global focus.
            await self.send("Emulation.setFocusEmulationEnabled", {"enabled": True}, page)
        try:
            await self.send(
                "Page.startScreencast" if enabled else "Page.stopScreencast",
                {
                    "format": "jpeg",
                    "quality": 60,
                    "maxWidth": 1920,
                    "maxHeight": 1920,
                    "everyNthFrame": 1,
                }
                if enabled
                else {},
                page,
            )
        except BaseException:
            if enabled:
                try:
                    await self.send("Emulation.setFocusEmulationEnabled", {"enabled": False}, page)
                except BrowserError:
                    pass
            raise
        finally:
            if not enabled:
                await self.send("Emulation.setFocusEmulationEnabled", {"enabled": False}, page)

    async def evaluate(self, page: Page, expression: str) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            page,
        )
        if "exceptionDetails" in result:
            raise BrowserError("The page script failed. Observe before retrying.")
        return result.get("result", {}).get("value")

    async def observe(self, page: Page) -> BrowserObservation:
        page.invalidate()
        nodes = (await self.send("Accessibility.getFullAXTree", page=page))["nodes"]
        lines = []
        for node in nodes[:1200]:
            if node.get("ignored"):
                continue
            role = node.get("role", {}).get("value", "")
            name = str(node.get("name", {}).get("value", ""))[:300]
            lines.append(f"{role}: {name}")
            if role in {"button", "link", "textbox", "combobox", "checkbox", "radio", "menuitem", "slider", "tab"}:
                if backend := node.get("backendDOMNodeId"):
                    page.elements[secrets.token_hex(8)] = backend
        page.revision = secrets.token_hex(12)
        page.requires_observation = False
        description = {node.get("backendDOMNodeId"): str(node.get("name", {}).get("value", ""))[:240] for node in nodes}
        return BrowserObservation(
            f"URL: {page.url}\n" + "\n".join(lines)[:24000],
            page.revision,
            tuple(BrowserElement(key, description.get(value, "")) for key, value in page.elements.items()),
        )

    async def element(self, page: Page, target: str, revision: str, *, fill: str | None = None) -> None:
        backend = page.elements.get(target)
        if not revision or revision != page.revision or backend is None:
            raise BrowserError("Element reference expired. Observe again.")
        node = await self.send("DOM.resolveNode", {"backendNodeId": backend}, page)
        identity = node["object"]["objectId"]
        try:
            await self.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend}, page)
            checked = await self.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": identity,
                    "returnByValue": True,
                    "functionDeclaration": """function() {
                    if (!this.isConnected || this.disabled) return null;
                    const r=this.getBoundingClientRect();
                    if (!r.width || !r.height) return null;
                    const x=r.left+r.width/2,y=r.top+r.height/2;
                    const hit=document.elementFromPoint(x,y);
                    if (!hit || !(this===hit || this.contains(hit))) return null;
                    return {x,y,type:this.type};
                }""",
                },
                page,
            )
            point = checked.get("result", {}).get("value")
            if not point:
                raise BrowserError("Element is detached, hidden, disabled or covered. Observe again.")
            if fill is not None:
                if point.get("type") in {"password", "file"}:
                    raise BrowserError("Use human takeover for password or file input.")
                await self.send(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": identity,
                        "functionDeclaration": "function(){this.focus();this.select?.();}",
                    },
                    page,
                )
                await self.send("Input.insertText", {"text": fill}, page)
            else:
                for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                    await self.send(
                        "Input.dispatchMouseEvent",
                        {
                            "type": kind,
                            "x": point["x"],
                            "y": point["y"],
                            "button": "left" if kind != "mouseMoved" else "none",
                            **({"clickCount": 1} if kind != "mouseMoved" else {}),
                        },
                        page,
                    )
        finally:
            await self.send("Runtime.releaseObject", {"objectId": identity}, page)

    async def screenshot(self, page: Page) -> bytes:
        result = await self.send("Page.captureScreenshot", {"format": "jpeg", "quality": 75}, page)
        return base64.b64decode(result["data"], validate=True)

    async def close_page(self, page: Page) -> None:
        await self.send("Target.closeTarget", {"targetId": page.target})
        self.pages.pop(page.target, None)

    async def close_browser(self) -> None:
        """Explicit process-owner operation; disconnecting never calls this."""
        await self.send("Browser.close")

    async def release_keys(self, page: Page) -> None:
        for key, code, number in (
            ("Shift", "ShiftLeft", 16),
            ("Control", "ControlLeft", 17),
            ("Alt", "AltLeft", 18),
            ("Meta", "MetaLeft", 91),
        ):
            await self.send(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": number, "modifiers": 0},
                page,
            )

    async def input(self, page: Page, request: Input) -> str | None:
        if request.type.startswith("mouse"):
            params = {
                "type": request.type,
                "x": request.x,
                "y": request.y,
                "modifiers": request.modifiers,
                "button": request.button,
                "buttons": request.buttons,
            }
            if request.type == "mouseWheel":
                params.update(deltaX=request.delta_x, deltaY=request.delta_y)
            elif request.type != "mouseMoved":
                params["clickCount"] = request.click_count
            await self.send("Input.dispatchMouseEvent", params, page)
        elif request.type in {"keyDown", "keyUp"}:
            command = None
            if request.type == "keyDown" and request.modifiers & 6 and not request.modifiers & 1:
                command = {
                    "a": "selectAll",
                    "c": "copy",
                    "insert": "copy",
                    "x": "cut",
                    "v": "paste",
                    "z": "redo" if request.modifiers & 8 else "undo",
                    "y": "redo",
                }.get(request.key.lower())
            copied = None
            if command in {"copy", "cut"}:
                # Transfer only this explicit selection, never the Host OS clipboard.
                # Password fields follow the browser's non-copyable behavior.
                copied = await self.evaluate(
                    page,
                    """(() => {
                    let element = document.activeElement;
                    while (element?.shadowRoot?.activeElement) element = element.shadowRoot.activeElement;
                    if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
                        if (element.type === 'password' || element.selectionStart == null) return null;
                        return element.selectionStart === element.selectionEnd ? null :
                            element.value.slice(element.selectionStart, element.selectionEnd);
                    }
                    return window.getSelection()?.toString() || null;
                })()""",
                )
            await self.send(
                "Input.dispatchKeyEvent",
                {
                    "type": request.type,
                    "key": request.key,
                    "code": request.code,
                    "text": request.text,
                    "modifiers": request.modifiers,
                    "windowsVirtualKeyCode": request.key_code,
                    **({"commands": [command]} if command else {}),
                },
                page,
            )
            return copied
        elif request.type == "text":
            await self.send("Input.insertText", {"text": request.text}, page)
        elif request.type == "navigate":
            await self.navigate(page, validate_url(request.text), wait=False)
        elif request.type == "reload":
            await self.send("Page.reload", page=page)
        else:
            history = await self.send("Page.getNavigationHistory", page=page)
            index = history["currentIndex"] + (-1 if request.type == "back" else 1)
            if 0 <= index < len(history["entries"]):
                await self.send("Page.navigateToHistoryEntry", {"entryId": history["entries"][index]["id"]}, page)
        return None

    async def act(
        self, page: Page, action: BrowserAction, *, target: str = "", value: str = "", revision: str = ""
    ) -> str:
        if page.requires_observation:
            raise BrowserError("Observe after handback before continuing.")
        if action == "navigate":
            await self.navigate(page, validate_url(value))
        elif action in {"click", "fill"}:
            await self.element(page, target, revision, fill=value if action == "fill" else None)
        elif action == "press":
            codes = {
                "Enter": 13,
                "Tab": 9,
                "Escape": 27,
                "Backspace": 8,
                "ArrowDown": 40,
                "ArrowUp": 38,
                "Space": 32,
            }
            if value not in codes:
                raise BrowserError("Unsupported browser key.")
            for kind in ("keyDown", "keyUp"):
                await self.send(
                    "Input.dispatchKeyEvent",
                    {
                        "type": kind,
                        "key": " " if value == "Space" else value,
                        "windowsVirtualKeyCode": codes[value],
                        "text": "\r" if value == "Enter" and kind == "keyDown" else "",
                    },
                    page,
                )
        elif action == "wait":
            try:
                async with asyncio.timeout(10):
                    while not await self.evaluate(page, f"document.body?.innerText.includes({json.dumps(value)})"):
                        await asyncio.sleep(0.1)
            except TimeoutError as exc:
                raise BrowserError("Timed out waiting for page text. Observe before continuing.") from exc
        return f"Browser {action} completed. Observe to verify the result."

    async def close(self) -> None:
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        self.pages.clear()
        await self.client.stop()


class CdpBrowserBackend:
    """One explicit page, usable by BrowserUseCapability without Harness UI."""

    def __init__(self, browser: Chromium, page: Page) -> None:
        self.browser, self.page = browser, page

    @classmethod
    @asynccontextmanager
    async def connect(cls, cdp_url: str) -> AsyncIterator[CdpBrowserBackend]:
        """Connect and open one page; exit closes that page and disconnects only."""
        browser = await Chromium.connect(cdp_url)
        page = None
        try:
            page = await browser.create()
            yield cls(browser, page)
        finally:
            try:
                if page is not None and page.target in browser.pages:
                    await browser.close_page(page)
            except BrowserError:
                pass
            finally:
                await browser.close()

    async def observe(self) -> BrowserObservation:
        async with self.page.lock:
            return await self.browser.observe(self.page)

    async def screenshot(self) -> bytes:
        async with self.page.lock:
            return await self.browser.screenshot(self.page)

    async def act(self, action: BrowserAction, *, target: str = "", value: str = "", revision: str = "") -> str:
        async with self.page.lock:
            return await self.browser.act(self.page, action, target=target, value=value, revision=revision)

    async def wait_for_handback(self) -> None:
        # Standalone connections have no human-control owner. Hosts that provide
        # takeover wrap the same CDP operations with their authority boundary.
        return None
