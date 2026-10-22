"""Managed Chromium with CDP page streaming and authenticated input."""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from a13n_harness.capabilities.browser import (
    BrowserAction,
    BrowserError,
    BrowserHumanControl,
    BrowserObservation,
)
from a13n_harness.capabilities.browser.cdp import Chromium, Page, validate_url
from a13n_harness.capabilities.browser.cdp import Input as BrowserInput
from pydantic import Field

from a13n_harness_ui.browser_setup import BrowserSetup, inspect_browser
from a13n_harness_ui.configuration.models import BrowserConfiguration
from a13n_harness_ui.errors import HarnessUiError
from a13n_harness_ui.surfaces import SurfaceModel

type BrowserRatio = Literal["native", "16:9", "4:3", "1:1", "9:16"]

type BrowserPreset = Literal["desktop", "laptop", "wide", "tablet", "tablet-landscape", "phone", "phone-landscape"]


class BrowserViewport(SurfaceModel):
    preset: BrowserPreset
    label: str
    width: int
    height: int


VIEWPORTS = (
    BrowserViewport(preset="desktop", label="Desktop", width=1280, height=800),
    BrowserViewport(preset="laptop", label="Laptop", width=1366, height=768),
    BrowserViewport(preset="wide", label="Desktop HD", width=1920, height=1080),
    BrowserViewport(preset="tablet", label="Tablet", width=768, height=1024),
    BrowserViewport(preset="tablet-landscape", label="Tablet landscape", width=1024, height=768),
    BrowserViewport(preset="phone", label="Phone", width=390, height=844),
    BrowserViewport(preset="phone-landscape", label="Phone landscape", width=844, height=390),
)
VIEWPORT_BY_PRESET = {viewport.preset: viewport for viewport in VIEWPORTS}


def viewport_size(preset: BrowserPreset, aspect_ratio: BrowserRatio) -> tuple[int, int]:
    size = VIEWPORT_BY_PRESET[preset]
    if aspect_ratio == "native":
        return size.width, size.height
    horizontal, vertical = map(int, aspect_ratio.split(":"))
    width, height = size.width, round(size.width * vertical / horizontal)
    # Keep captured pixels and page coordinates at the same scale.
    scale = min(1, 1920 / max(width, height))
    return round(width * scale), round(height * scale)


class BrowserState(SurfaceModel):
    session_id: str = ""
    status: Literal["disabled", "enabled", "ready", "stopped", "failed"] = "disabled"
    control: Literal["agent", "human"] = "agent"
    url: str = ""
    error: str | None = None
    viewport: BrowserPreset = "desktop"
    aspect_ratio: BrowserRatio = "native"
    viewports: tuple[BrowserViewport, ...] = VIEWPORTS
    resize_available: bool = True


class BrowserCommand(SurfaceModel):
    session_id: str = Field(min_length=1, max_length=80)


class BrowserAccessRequest(BrowserCommand):
    pass


class BrowserViewportRequest(BrowserCommand):
    control_token: str = Field(default="", max_length=80, repr=False)
    preset: BrowserPreset
    aspect_ratio: BrowserRatio = "native"


class BrowserAccess(SurfaceModel):
    ticket: str


class BrowserViewerInput(SurfaceModel):
    control_token: str = Field(default="", max_length=80, repr=False)
    input: BrowserInput


class InputBuffer:
    """Preserve input barriers while replacing obsolete consecutive motion."""

    def __init__(self) -> None:
        self.items: deque[BrowserViewerInput] = deque()
        self.changed = asyncio.Condition()

    async def put(self, request: BrowserViewerInput) -> None:
        async with self.changed:
            if self.items:
                previous = self.items[-1]
                left, right = previous.input, request.input
                same_state = (
                    previous.control_token == request.control_token
                    and left.type == right.type
                    and left.button == right.button
                    and left.buttons == right.buttons
                    and left.modifiers == right.modifiers
                )
                if same_state and right.type == "mouseMoved":
                    self.items[-1] = request
                    return
                if same_state and right.type == "mouseWheel":
                    dx, dy = left.delta_x + right.delta_x, left.delta_y + right.delta_y
                    if abs(dx) <= 10000 and abs(dy) <= 10000:
                        self.items[-1] = request.model_copy(
                            update={"input": right.model_copy(update={"delta_x": dx, "delta_y": dy})}
                        )
                        return
            await self.changed.wait_for(lambda: len(self.items) < 64)
            self.items.append(request)
            self.changed.notify_all()

    async def get(self) -> BrowserViewerInput:
        async with self.changed:
            await self.changed.wait_for(lambda: bool(self.items))
            request = self.items.popleft()
            self.changed.notify_all()
            return request


class BrowserControl(SurfaceModel):
    state: BrowserState
    control_token: str = Field(repr=False)


class HostBrowserError(HarnessUiError, BrowserError):
    pass


class HumanBrowserControl(HarnessUiError, BrowserHumanControl):
    pass


def failure(message: str, code: str = "operation_failed") -> HarnessUiError:
    if code == "human_control":
        return HumanBrowserControl(message, code="browser_human_control")
    return HostBrowserError(message, code="browser_" + code)


def browser_url(value: str) -> str:
    try:
        return validate_url(value)
    except BrowserError as exc:
        raise failure(str(exc), "url_invalid") from exc


async def require_browser(configuration: BrowserConfiguration) -> BrowserSetup:
    setup = await inspect_browser(configuration)
    if not setup.ready or setup.browser is None:
        raise failure(setup.message, "runtime_not_ready")
    return setup


@dataclass
class Binding:
    identity: str = field(default_factory=lambda: "browser_" + secrets.token_hex(12))
    status: Literal["enabled", "ready", "stopped", "failed"] = "enabled"
    page: Page | None = None
    viewport: BrowserPreset = "desktop"
    aspect_ratio: BrowserRatio = "native"
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None


@dataclass(eq=False)
class Viewer:
    thread: str
    binding: str
    control_token: str = field(default="", repr=False)
    frames: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def publish(self, frame: dict) -> None:
        if self.frames.full():
            self.frames.get_nowait()
        self.frames.put_nowait(frame)


class HostBrowsers:
    def __init__(
        self,
        *,
        root: Path,
        available: bool,
        authorize: Callable[[str], Awaitable[None]],
        configuration: BrowserConfiguration | None = None,
    ) -> None:
        self.configuration = configuration or BrowserConfiguration()
        self.root = Path(self.configuration.profile_root) if self.configuration.profile_root else root / "profile"
        self.available, self.authorize = available, authorize
        self.bindings: dict[str, Binding] = {}
        self.owners: dict[str, str] = {}
        self.runtime: Chromium | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.monitor: asyncio.Task | None = None
        self.lifecycle = asyncio.Lock()
        self.control_lock = asyncio.Lock()
        self.operations: set[asyncio.Future[None]] = set()
        self.released = asyncio.Event()
        self.released.set()
        self.human_thread = ""
        self.control_token = ""
        self.tickets: dict[str, tuple[str, str, float]] = {}
        self.viewers: set[Viewer] = set()

    def state(self, thread_id: str) -> BrowserState:
        item = self.bindings.get(thread_id)
        if item is None:
            return BrowserState()
        return BrowserState(
            session_id=item.identity,
            status=item.status,
            control="human" if self.human_thread else "agent",
            url=item.page.url if item.page else "",
            error=item.error,
            viewport=item.viewport,
            aspect_ratio=item.aspect_ratio,
        )

    def binding(self, thread_id: str) -> str | None:
        item = self.bindings.get(thread_id)
        return item.identity if item and item.status not in {"stopped", "failed"} else None

    def checked(self, thread_id: str, identity: str) -> Binding:
        item = self.bindings.get(thread_id)
        if item is None or item.identity != identity:
            raise failure("Browser session changed. Refresh before acting.", "session_conflict")
        return item

    async def setup(self, thread_id: str) -> BrowserSetup:
        await self.authorize(thread_id)
        result = await inspect_browser(self.configuration)
        await self.authorize(thread_id)
        return result

    async def enable(self, thread_id: str) -> BrowserState:
        await self.authorize(thread_id)
        await require_browser(self.configuration)
        self.bindings.setdefault(thread_id, Binding())
        return self.state(thread_id)

    def revoke_viewers(self, thread_id: str | None = None) -> None:
        for viewer in self.viewers:
            if thread_id is None or viewer.thread == thread_id:
                viewer.closed.set()
        self.tickets = {
            key: value for key, value in self.tickets.items() if thread_id is not None and value[0] != thread_id
        }

    def failed(self, reason: str) -> None:
        for item in self.bindings.values():
            item.closed.set()
            item.status, item.error = "failed", reason
        self.human_thread = self.control_token = ""
        self.released.set()
        self.revoke_viewers()

    async def launch(self) -> None:
        if self.runtime is not None and self.process is not None and self.process.returncode is None:
            return
        await self.close_runtime()
        preparation = await require_browser(self.configuration)
        assert preparation.browser is not None
        await asyncio.to_thread(self.root.mkdir, mode=0o700, parents=True, exist_ok=True)
        endpoint = self.root / "DevToolsActivePort"
        endpoint.unlink(missing_ok=True)
        arguments = [
            preparation.browser.path,
            f"--user-data-dir={self.root}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--headless=new",
            "--mute-audio",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0:
            arguments.extend(["--no-sandbox", "--disable-dev-shm-usage"])
        self.process = await asyncio.create_subprocess_exec(
            *arguments, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        try:
            async with asyncio.timeout(20):
                while True:
                    if self.process.returncode is not None:
                        raise RuntimeError(
                            "Chromium exited during startup; check executable, system libraries and profile ownership"
                        )
                    if endpoint.exists():
                        parts = (await asyncio.to_thread(endpoint.read_text)).splitlines()
                        if len(parts) == 2 and parts[0].isdecimal() and parts[1].startswith("/devtools/browser/"):
                            break
                    await asyncio.sleep(0.05)
            self.runtime = await Chromium.connect(f"ws://127.0.0.1:{parts[0]}{parts[1]}")
            self.runtime.frame = self.frame
            self.runtime.opened = self.opened
            self.runtime.closed = self.page_closed
            process = self.process

            async def watch() -> None:
                await process.wait()
                if self.process is process:
                    self.failed("Chromium exited. Start the browser to continue.")

            self.monitor = asyncio.create_task(watch())
        except BaseException:
            await self.close_runtime()
            raise

    def frame(self, page: Page, frame: dict) -> None:
        owner = self.owners.get(page.target)
        item = self.bindings.get(owner or "")
        if item and item.page is page:
            for viewer in self.viewers:
                if viewer.thread == owner and not viewer.closed.is_set():
                    viewer.publish({**frame, "target": page.target})

    def opened(self, info: dict) -> None:
        if (
            info.get("type") != "page"
            or not (owner := self.owners.get(info.get("openerId", "")))
            or self.runtime is None
        ):
            return
        identity = self.bindings[owner].identity

        async def adopt() -> None:
            if self.runtime is None:
                return
            page = await self.runtime.attach(info["targetId"], info.get("url", "about:blank"))
            item = self.checked(owner, identity)
            if item.status != "ready":
                return
            previous = item.page
            self.owners[page.target] = owner
            item.page = page
            await self.runtime.resize(page, *viewport_size(item.viewport, item.aspect_ratio))
            if any(viewer.thread == owner and not viewer.closed.is_set() for viewer in self.viewers):
                if previous:
                    await self.runtime.screencast(previous, False)
                await self.runtime.screencast(page, True)

        self.runtime.background(adopt())

    def page_closed(self, target: str) -> None:
        owner = self.owners.pop(target, None)
        if self.runtime is not None:
            self.runtime.pages.pop(target, None)
        if owner and (item := self.bindings.get(owner)) and item.page and item.page.target == target:
            item.closed.set()
            self.bindings[owner] = Binding(status="stopped", viewport=item.viewport, aspect_ratio=item.aspect_ratio)
            self.revoke_viewers(owner)
            if self.human_thread == owner:
                self.human_thread = self.control_token = ""
                if self.runtime:
                    for page in self.runtime.pages.values():
                        page.invalidate()
                        page.requires_observation = True
                self.released.set()

    async def start(self, thread_id: str, binding: str, *, explicit: bool = False) -> BrowserState:
        await self.authorize(thread_id)
        async with self.lifecycle:
            item = self.checked(thread_id, binding)
            if item.status in {"stopped", "failed"}:
                if not explicit:
                    raise failure("Browser stopped. Start it explicitly.", "session_conflict")
                item.closed.set()
                item = self.bindings[thread_id] = Binding(viewport=item.viewport, aspect_ratio=item.aspect_ratio)
            if self.human_thread and self.human_thread != thread_id:
                raise failure("Hand back browser control before opening another page.", "human_control")
            try:
                await self.launch()
                assert self.runtime is not None
                if item.page is None:
                    page = await self.runtime.create()
                    self.owners[page.target] = thread_id
                    item.page = page
                    await self.runtime.resize(page, *viewport_size(item.viewport, item.aspect_ratio))
                item.status = "ready"
            except Exception as exc:
                item.status, item.error = "failed", str(exc)
                raise failure(
                    "Chromium startup failed. Check the executable and shared profile.", "runtime_unavailable"
                ) from exc
            return self.state(thread_id)

    async def stop(self, thread_id: str, binding: str) -> BrowserState:
        await self.authorize(thread_id)
        async with self.lifecycle:
            item = self.checked(thread_id, binding)
            item.closed.set()
            self.revoke_viewers(thread_id)
            if self.human_thread == thread_id:
                await self.release()
            self.bindings[thread_id] = Binding(status="stopped", viewport=item.viewport, aspect_ratio=item.aspect_ratio)
            if self.runtime:
                for target, owner in tuple(self.owners.items()):
                    if owner == thread_id:
                        self.owners.pop(target, None)
                        page = self.runtime.pages.get(target)
                        if page:
                            await self.runtime.close_page(page)
            return self.state(thread_id)

    async def reset(self, thread_id: str, binding: str) -> BrowserState:
        if self.human_thread and self.human_thread != thread_id:
            raise failure("Hand back control before resetting another page.", "human_control")
        stopped = await self.stop(thread_id, binding)
        return await self.start(thread_id, stopped.session_id, explicit=True)

    async def resize(self, thread_id: str, request: BrowserViewportRequest) -> BrowserState:
        await self.authorize(thread_id)
        self.require_control(thread_id, request.session_id, request.control_token)
        item = self.checked(thread_id, request.session_id)
        assert self.runtime and item.page
        async with item.page.lock:
            await self.runtime.resize(item.page, *viewport_size(request.preset, request.aspect_ratio))
            item.page.invalidate()
            item.page.requires_observation = True
            item.viewport, item.aspect_ratio = request.preset, request.aspect_ratio
        return self.state(thread_id)

    async def release_keys(self, thread: str) -> None:
        item = self.bindings.get(thread)
        if self.runtime and item and item.page:
            async with item.page.lock:
                await self.runtime.release_keys(item.page)

    async def release(self) -> None:
        previous = self.human_thread
        self.control_token = ""
        try:
            if previous:
                await self.release_keys(previous)
        finally:
            self.human_thread = ""
            if self.runtime:
                for page in self.runtime.pages.values():
                    page.invalidate()
                    page.requires_observation = True
            self.released.set()

    async def control(self, thread_id: str, binding: str, *, human: bool) -> BrowserControl:
        await self.authorize(thread_id)
        async with self.control_lock:
            item = self.checked(thread_id, binding)
            if human:
                if self.human_thread:
                    raise failure("Browser is already under human control.", "control_conflict")
                if item.status != "ready":
                    raise failure("Start the browser before taking control.", "not_ready")
                self.human_thread = thread_id
                self.released.clear()
                # Existing per-target operations settle; newly admitted ones see
                # the barrier. Ordinary Agent work has no browser-wide queue.
                await asyncio.gather(*tuple(self.operations), return_exceptions=True)
                self.control_token = secrets.token_urlsafe(24)
                if self.runtime:
                    for page in self.runtime.pages.values():
                        page.invalidate()
                        page.requires_observation = True
            else:
                if self.human_thread and self.human_thread != thread_id:
                    raise failure("Hand back control in its owning Thread.", "control_conflict")
                await self.release()
            return BrowserControl(state=self.state(thread_id), control_token=self.control_token)

    def require_control(self, thread: str, binding: str, token: str) -> None:
        self.checked(thread, binding)
        if not token or self.human_thread != thread or not secrets.compare_digest(token, self.control_token):
            raise failure("Take control before operating this page.", "control_required")

    async def access(self, thread_id: str, request: BrowserAccessRequest) -> BrowserAccess:
        await self.authorize(thread_id)
        item = self.checked(thread_id, request.session_id)
        if item.status != "ready":
            raise failure("Start the browser before viewing.", "not_ready")
        now = time.monotonic()
        self.tickets = {key: value for key, value in self.tickets.items() if value[2] > now}
        if len(self.tickets) >= 128:
            raise failure("Too many pending viewers.", "limit")
        ticket = secrets.token_urlsafe(24)
        self.tickets[ticket] = (thread_id, request.session_id, now + 30)
        return BrowserAccess(ticket=ticket)

    @asynccontextmanager
    async def viewer(self, thread: str, ticket: str) -> AsyncIterator[Viewer]:
        claim = self.tickets.pop(ticket, None)
        if claim is None or claim[0] != thread or claim[2] <= time.monotonic():
            raise failure("Viewer access expired.", "access_expired")
        await self.authorize(thread)
        item = self.checked(thread, claim[1])
        if item.page is None or self.runtime is None or item.status != "ready":
            raise failure("Browser is not running.", "not_ready")
        if len(self.viewers) >= 8:
            raise failure("Too many viewers.", "limit")
        active = any(v.thread == thread and not v.closed.is_set() for v in self.viewers)
        viewer = Viewer(thread, item.identity)
        self.viewers.add(viewer)
        try:
            if not active:
                await self.runtime.screencast(item.page, True)
            # A static page may not emit a new screencast frame for a second
            # viewer. Seed it without waiting for another repaint.
            viewer.publish(
                {
                    "data": base64.b64encode(await self.runtime.screenshot(item.page)).decode(),
                    "metadata": {"timestamp": time.time()},
                }
            )
            yield viewer
        finally:
            viewer.closed.set()
            self.viewers.discard(viewer)
            if viewer.control_token and secrets.compare_digest(viewer.control_token, self.control_token):
                # Losing the controller revokes input but keeps the Agent pause.
                self.control_token = ""
                try:
                    await self.release_keys(thread)
                except BrowserError:
                    pass
            if (
                self.runtime
                and item.page
                and not any(v.thread == thread and not v.closed.is_set() for v in self.viewers)
            ):
                try:
                    await self.runtime.screencast(item.page, False)
                except BrowserError:
                    pass

    async def input(self, viewer: Viewer, request: BrowserInput, *, control_token: str = "") -> str | None:
        await self.authorize(viewer.thread)
        item = self.checked(viewer.thread, viewer.binding)
        if viewer.closed.is_set():
            raise failure("Viewer access has been revoked.", "control_required")
        self.require_control(viewer.thread, viewer.binding, control_token)
        if self.runtime is None or item.page is None:
            raise failure("Browser is not running.", "not_ready")
        async with item.page.lock:
            # Handback can revoke input while this request waits for the page.
            if viewer.closed.is_set():
                raise failure("Viewer access has been revoked.", "control_required")
            self.require_control(viewer.thread, viewer.binding, control_token)
            if any(
                other is not viewer
                and not other.closed.is_set()
                and secrets.compare_digest(other.control_token, control_token)
                for other in self.viewers
            ):
                raise failure("Another controller is connected.", "control_conflict")
            viewer.control_token = control_token
            return await self.runtime.input(item.page, request)

    def backend(self, thread_id: str, binding: str) -> HostBrowserBackend:
        return HostBrowserBackend(self, thread_id, binding)

    async def close_runtime(self) -> None:
        monitor, self.monitor = self.monitor, None
        if monitor:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        runtime, self.runtime = self.runtime, None
        if runtime:
            try:
                await runtime.close_browser()
            except BrowserError:
                pass
            await runtime.close()
        process, self.process = self.process, None
        if process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), 5)
            except ProcessLookupError:
                pass
            except TimeoutError:
                process.kill()
                await process.wait()
        self.owners.clear()

    async def close(self) -> None:
        self.failed("Browser stopped.")
        await self.close_runtime()
        self.bindings.clear()


class HostBrowserBackend:
    def __init__(self, browsers: HostBrowsers, thread_id: str, binding: str) -> None:
        self.browsers, self.thread_id, self.binding = browsers, thread_id, binding

    async def wait_for_handback(self) -> None:
        item = self.browsers.checked(self.thread_id, self.binding)
        waits = [asyncio.create_task(self.browsers.released.wait()), asyncio.create_task(item.closed.wait())]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
        self.browsers.checked(self.thread_id, self.binding)

    @asynccontextmanager
    async def operation(self) -> AsyncIterator[tuple[Chromium, Page]]:
        await self.browsers.authorize(self.thread_id)
        item = self.browsers.checked(self.thread_id, self.binding)
        if self.browsers.human_thread:
            raise failure("The user controls the browser. Ask for handback.", "human_control")
        if item.status != "ready" or item.page is None or self.browsers.runtime is None:
            raise failure("Browser is not running.", "not_ready")
        completion = asyncio.get_running_loop().create_future()
        self.browsers.operations.add(completion)
        try:
            async with item.page.lock:
                if self.browsers.human_thread:
                    raise failure("The user controls the browser. Ask for handback.", "human_control")
                yield self.browsers.runtime, item.page
                self.browsers.checked(self.thread_id, self.binding)
        finally:
            self.browsers.operations.discard(completion)
            completion.set_result(None)

    async def observe(self) -> BrowserObservation:
        async with self.operation() as (runtime, page):
            return await runtime.observe(page)

    async def screenshot(self) -> bytes:
        async with self.operation() as (runtime, page):
            return await runtime.screenshot(page)

    async def act(self, action: BrowserAction, *, target: str = "", value: str = "", revision: str = "") -> str:
        if action == "navigate":
            browser_url(value)
            await self.browsers.start(self.thread_id, self.binding)
        async with self.operation() as (runtime, page):
            return await runtime.act(page, action, target=target, value=value, revision=revision)
