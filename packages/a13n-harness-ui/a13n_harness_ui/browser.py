"""Optional process-local Host browsers, independent of Runs and viewer lifetimes."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from anyio import CancelScope, fail_after
from pydantic import Field, JsonValue

from a13n_harness_ui.errors import HarnessUiError
from a13n_harness_ui.surfaces import SurfaceModel

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page


class BrowserState(SurfaceModel):
    enabled: bool = False
    session_id: str | None = None
    status: Literal["disabled", "stopped", "ready", "disconnected", "failed"] = "disabled"
    url: str = ""
    title: str = ""
    error: str | None = None


class BrowserFrame(SurfaceModel):
    state: BrowserState
    captured_at: datetime
    image: str = Field(max_length=4 * 1024 * 1024)


class BrowserPresentation(SurfaceModel):
    state: BrowserState
    sandbox_url: str


class BrowserDOM(SurfaceModel):
    state: BrowserState
    epoch: str
    cursor: int
    observed_at: datetime
    events: tuple[JsonValue, ...]


class BrowserInput(SurfaceModel):
    session_id: str = Field(min_length=1, max_length=80)
    epoch: str = Field(min_length=1, max_length=80)
    node_id: int = Field(ge=1)
    action: Literal["click", "fill", "press", "scroll", "select"]
    text: str = Field(default="", max_length=16_384)
    key: Literal[
        "Enter",
        "Tab",
        "Escape",
        "Backspace",
        "ArrowDown",
        "ArrowUp",
        "ArrowLeft",
        "ArrowRight",
        "PageDown",
        "PageUp",
        "Space",
    ] = "Enter"
    x: int = Field(default=0, ge=0, le=100_000)
    y: int = Field(default=0, ge=0, le=100_000)


class BrowserNavigation(SurfaceModel):
    session_id: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=8192)


def browser_url(value: str) -> str:
    """Navigation is HTTP(S), including local development servers, without credentials."""
    parsed = urlsplit(value)
    if (
        len(value) > 8192
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(ord(character) < 32 for character in value)
    ):
        raise HarnessUiError("Enter an HTTP or HTTPS URL without embedded credentials.", code="browser_url_invalid")
    return value


class BrowserSession:
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.session_id = f"browser_{uuid4().hex}"
        self.state = BrowserState(enabled=True, session_id=self.session_id, status="stopped")
        self.lock = asyncio.Lock()
        self.resources = AsyncExitStack()
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.retired = False
        self.dom_epoch = ""
        self.dom_events: list[tuple[int, JsonValue]] = []
        self.dom_cursor = 0
        self.dom_bytes = 0
        self.dom_overflow = False
        self.changed = asyncio.Event()

    async def start(self) -> None:
        if self.browser is not None:
            self.require_page()
            return
        try:
            # Neither package import nor browser preparation occurs for ordinary Shell use.
            from playwright.async_api import async_playwright

            driver = await self.resources.enter_async_context(async_playwright())
            self.browser = await driver.chromium.launch(channel="chromium", headless=True, timeout=30_000)
            self.resources.push_async_callback(self.browser.close)
            self.context = await self.browser.new_context(
                viewport={"width": 1280, "height": 800}, accept_downloads=False, service_workers="block"
            )
            self.page = await self.context.new_page()
            recorder = Path(__file__).with_name("static") / "browser-recorder.js"
            if not recorder.is_file():
                raise HarnessUiError(
                    "Browser recorder assets are missing. Rebuild the WebUI assets.", code="browser_assets_missing"
                )
            await self.page.expose_binding("__a13nBrowserEvent", self.record_dom)
            script = "if (window === window.top) {" + recorder.read_text(encoding="utf-8") + "}"
            await self.page.add_init_script(script)
            await self.page.evaluate(script)
            self.page.set_default_timeout(10_000)
            self.browser.on("disconnected", self.disconnected)
            self.state = self.state.model_copy(update={"status": "ready", "error": None})
        except ImportError as exc:
            self.state = self.state.model_copy(
                update={"status": "failed", "error": "Browser support is not installed."}
            )
            raise HarnessUiError(
                "Install a13n-harness-ui[browser], then run python -m playwright install chromium.",
                code="browser_runtime_missing",
            ) from exc
        except BaseException as exc:
            await self.close()
            self.retired = False
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, HarnessUiError):
                self.state = self.state.model_copy(update={"status": "failed", "error": str(exc)})
                raise
            self.state = self.state.model_copy(update={"status": "failed", "error": "Chromium could not start."})
            raise HarnessUiError(
                "Chromium could not start. Run python -m playwright install chromium and check OS dependencies.",
                code="browser_start_failed",
            ) from exc

    def disconnected(self, *_: object) -> None:
        self.state = self.state.model_copy(update={"status": "disconnected", "error": "Browser connection was lost."})
        self.signal()

    def signal(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def record_dom(self, source: object, payload: object) -> None:
        if (
            self.retired
            or not isinstance(source, dict)
            or source.get("frame") != (self.page.main_frame if self.page else None)
        ):
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("epoch"), str) or len(payload["epoch"]) > 80:
            return
        event = payload.get("event")
        if not isinstance(event, dict) or event.get("type") not in {2, 3, 4}:
            return
        epoch = payload["epoch"]
        if epoch != self.dom_epoch:
            self.dom_epoch, self.dom_events, self.dom_bytes, self.dom_overflow = epoch, [], 0, False
        if self.dom_overflow:
            return
        size = len(json.dumps(event).encode())
        if size > 2 * 1024 * 1024 or self.dom_bytes + size > 4 * 1024 * 1024 or len(self.dom_events) >= 512:
            self.dom_overflow = True
            self.signal()
            return
        self.dom_cursor += 1
        self.dom_events.append((self.dom_cursor, event))
        self.dom_bytes += size
        self.signal()

    def require_page(self) -> Page:
        if (
            self.retired
            or self.page is None
            or self.page.is_closed()
            or self.browser is None
            or not self.browser.is_connected()
        ):
            raise HarnessUiError(
                "Browser session ended. Start a new session explicitly; actions are not replayed.",
                code="browser_disconnected",
            )
        return self.page

    async def update_state(self) -> BrowserState:
        page = self.require_page()
        self.state = self.state.model_copy(update={"url": page.url[:8192], "title": (await page.title())[:512]})
        return self.state

    async def close(self) -> None:
        self.retired = True
        self.signal()
        # A canceled request must still join browser cleanup, including the bundled driver.
        with CancelScope(shield=True):
            with fail_after(15):
                await self.resources.aclose()
        self.browser, self.context, self.page = None, None, None
        self.state = self.state.model_copy(update={"status": "stopped"})


class HostBrowsers:
    """One disposable profile per root Thread, with a finite browser budget."""

    def __init__(self, *, available: bool, authorize: Callable[[str], Awaitable[None]]) -> None:
        self.available = available
        self.authorize = authorize
        self.sessions: dict[str, BrowserSession] = {}
        self.lifecycle = asyncio.Lock()

    def state(self, thread_id: str) -> BrowserState:
        session = self.sessions.get(thread_id)
        return session.state if session is not None else BrowserState()

    async def enable(self, thread_id: str) -> BrowserState:
        await self.authorize(thread_id)
        async with self.lifecycle:
            if thread_id not in self.sessions:
                if len(self.sessions) >= 16:
                    raise HarnessUiError("Stop an unused browser before enabling another.", code="browser_limit")
                self.sessions[thread_id] = BrowserSession(thread_id)
            return self.state(thread_id)

    def binding(self, thread_id: str) -> str | None:
        session = self.sessions.get(thread_id)
        return session.session_id if session is not None and not session.retired else None

    def session(self, thread_id: str, session_id: str) -> BrowserSession:
        session = self.sessions.get(thread_id)
        if session is None or session.session_id != session_id or session.retired:
            raise HarnessUiError(
                "Browser session changed. Observe the new session before acting.", code="browser_session_conflict"
            )
        return session

    async def start(self, thread_id: str, session_id: str) -> BrowserState:
        async with self.lifecycle:
            await self.authorize(thread_id)
            session = self.session(thread_id, session_id)
            if session.browser is None and sum(item.browser is not None for item in self.sessions.values()) >= 4:
                raise HarnessUiError(
                    "Four browsers are already running. Stop one before starting another.", code="browser_limit"
                )
            async with session.lock:
                await session.start()
                return session.state

    async def stop(self, thread_id: str, session_id: str) -> BrowserState:
        async with self.lifecycle:
            session = self.sessions.get(thread_id)
            if session is None or session.session_id != session_id:
                raise HarnessUiError("Browser session changed; refresh its state.", code="browser_session_conflict")
            # Retire before waiting so queued actions cannot acquire new authority.
            session.retired = True
            async with session.lock:
                await session.close()
            self.sessions.pop(thread_id, None)
            return BrowserState()

    async def perform(
        self,
        thread_id: str,
        session_id: str,
        action: Literal["navigate", "observe", "click", "fill", "press", "screenshot"],
        *,
        value: str = "",
        text: str = "",
    ) -> BrowserState | BrowserFrame | str:
        if action == "navigate":
            browser_url(value)
            # Only a fresh, explicitly enabled binding can lazily start its first browser.
            await self.start(thread_id, session_id)
        session = self.session(thread_id, session_id)
        async with session.lock:
            await self.authorize(thread_id)
            session = self.session(thread_id, session_id)
            page = session.require_page()
            try:
                if action == "navigate":
                    await page.goto(value, wait_until="domcontentloaded", timeout=20_000)
                elif action == "click":
                    await page.locator(value).click()
                elif action == "fill":
                    await page.locator(value).fill(text)
                elif action == "press":
                    await page.keyboard.press(value)
                state = await session.update_state()
                if action == "observe":
                    return state.model_dump_json() + "\n" + (await page.locator("body").aria_snapshot())[:32_768]
                if action == "screenshot":
                    picture = await page.screenshot(type="jpeg", quality=65, timeout=5000)
                    if len(picture) > 3 * 1024 * 1024:
                        raise HarnessUiError("Browser picture exceeded the size limit.", code="browser_frame_too_large")
                    return BrowserFrame(
                        state=state, captured_at=datetime.now(UTC), image=base64.b64encode(picture).decode()
                    )
                return state
            except HarnessUiError:
                raise
            except Exception as exc:
                if not session.browser or not session.browser.is_connected() or page.is_closed():
                    session.disconnected()
                raise HarnessUiError(
                    "Browser operation failed; its effect may be uncertain. Observe the page before any retry.",
                    code="browser_operation_failed",
                ) from exc

    async def dom(self, thread_id: str, session_id: str, *, epoch: str = "", after: int = 0) -> BrowserDOM:
        await self.authorize(thread_id)
        session = self.session(thread_id, session_id)
        changed = session.changed
        if session.dom_epoch == epoch and session.dom_cursor <= after and not session.dom_overflow:
            try:
                await asyncio.wait_for(changed.wait(), timeout=10)
            except TimeoutError:
                pass
        async with session.lock:
            await self.authorize(thread_id)
            session = self.session(thread_id, session_id)
            page = session.require_page()
            if session.dom_overflow:
                await page.evaluate("window.__a13nBrowserRecorder.restart()")
            state = await session.update_state()
            events = tuple(
                event for cursor, event in session.dom_events if epoch != session.dom_epoch or cursor > after
            )
            return BrowserDOM(
                state=state,
                epoch=session.dom_epoch,
                cursor=session.dom_cursor,
                observed_at=datetime.now(UTC),
                events=events,
            )

    async def input(self, thread_id: str, request: BrowserInput) -> BrowserState:
        session = self.session(thread_id, request.session_id)
        async with session.lock:
            await self.authorize(thread_id)
            session = self.session(thread_id, request.session_id)
            page = session.require_page()
            handle = await page.evaluate_handle(
                "([id, epoch]) => window.__a13nBrowserRecorder?.node(id, epoch)", [request.node_id, request.epoch]
            )
            try:
                element = handle.as_element()
                if element is None:
                    raise HarnessUiError(
                        "The page changed. Wait for the current DOM before acting.", code="browser_dom_conflict"
                    )
                if await element.get_attribute("type") in {"password", "file"}:
                    raise HarnessUiError(
                        "Password and file inputs are unavailable in the DOM viewer.", code="browser_input_unavailable"
                    )
                if request.action == "click":
                    await element.click(timeout=10_000)
                elif request.action == "fill":
                    await element.fill(request.text, timeout=10_000)
                elif request.action == "press":
                    await element.press(request.key, timeout=10_000)
                elif request.action == "select":
                    await element.select_option(request.text, timeout=10_000)
                else:
                    await element.evaluate(
                        "(node, position) => node.scrollTo(position[0], position[1])", [request.x, request.y]
                    )
                return await session.update_state()
            except HarnessUiError:
                raise
            except Exception as exc:
                raise HarnessUiError(
                    "Browser input failed; observe the current page before retrying.", code="browser_operation_failed"
                ) from exc
            finally:
                await handle.dispose()

    async def close(self) -> None:
        async with self.lifecycle:
            for session in self.sessions.values():
                session.retired = True
                async with session.lock:
                    await session.close()
            self.sessions.clear()
