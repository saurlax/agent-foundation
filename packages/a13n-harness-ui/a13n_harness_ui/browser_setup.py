"""Read-only discovery of configured Chromium executables."""

from __future__ import annotations

import asyncio
import importlib.util
import ntpath
import os
import shutil
import sys
from pathlib import Path
from typing import Literal

from a13n_harness_ui.configuration.models import BrowserConfiguration
from a13n_harness_ui.surfaces import SurfaceModel

type BrowserPlatform = Literal["linux", "macos", "windows", "other"]


class BrowserExecutable(SurfaceModel):
    name: str
    path: str
    source: Literal["system", "custom"]


class BrowserRequirement(SurfaceModel):
    name: str
    installed: bool
    detail: str
    required: bool = True


class BrowserInstallStep(SurfaceModel):
    title: str
    command: str


class BrowserSetup(SurfaceModel):
    platform: BrowserPlatform
    distribution: str | None = None
    supported: bool
    ready: bool
    browser: BrowserExecutable | None = None
    requirements: tuple[BrowserRequirement, ...] = ()
    steps: tuple[BrowserInstallStep, ...] = ()
    message: str = ""


def host_platform() -> BrowserPlatform:
    platforms: dict[str, BrowserPlatform] = {"linux": "linux", "darwin": "macos", "win32": "windows"}
    return platforms.get(sys.platform, "other")


def executable_path(path: str) -> str | None:
    candidate = Path(path).expanduser().resolve(strict=False)
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


def system_browsers(platform: BrowserPlatform) -> list[BrowserExecutable]:
    candidates: list[tuple[str, str]] = []
    if platform == "linux":
        for name, command in (
            ("Google Chrome", "google-chrome"),
            ("Google Chrome", "google-chrome-stable"),
            ("Chromium", "chromium"),
            ("Chromium", "chromium-browser"),
            ("Microsoft Edge", "microsoft-edge"),
            ("Microsoft Edge", "microsoft-edge-stable"),
        ):
            if path := shutil.which(command):
                candidates.append((name, path))
    elif platform == "macos":
        for root in (Path("/Applications"), Path.home() / "Applications"):
            for name, path in (
                ("Google Chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
                ("Chromium", "Chromium.app/Contents/MacOS/Chromium"),
                ("Microsoft Edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ):
                candidates.append((name, str(root / path)))
    elif platform == "windows":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if root := os.environ.get(variable):
                for name, path in (
                    ("Google Chrome", r"Google\Chrome\Application\chrome.exe"),
                    ("Chromium", r"Chromium\Application\chrome.exe"),
                    ("Microsoft Edge", r"Microsoft\Edge\Application\msedge.exe"),
                ):
                    candidates.append((name, ntpath.join(root, path)))
    found: dict[str, BrowserExecutable] = {}
    for name, candidate in candidates:
        if path := executable_path(candidate):
            found.setdefault(path, BrowserExecutable(name=name, path=path, source="system"))
    return list(found.values())


def supported_browser(version: str) -> bool:
    return any(name in version.lower() for name in ("chromium", "chrome", "edge", "brave", "vivaldi"))


class BrowserScan(SurfaceModel):
    browsers: tuple[BrowserExecutable, ...] = ()


async def scan_browsers() -> BrowserScan:
    """Enumerate supported installed executables only when explicitly requested."""
    importlib.invalidate_caches()
    platform = host_platform()
    candidates = await asyncio.to_thread(system_browsers, platform)
    browsers = []
    for candidate in candidates:
        if supported_browser(await probe(candidate.path, "--version")):
            browsers.append(candidate)
    return BrowserScan(browsers=tuple(browsers))


async def probe(path: str, argument: str) -> str:
    """Bound help/version probes without launching a desktop or executing a shell."""
    process = None
    try:
        async with asyncio.timeout(5):
            process = await asyncio.create_subprocess_exec(
                path, argument, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            assert process.stdout is not None
            output = bytearray()
            while chunk := await process.stdout.read(min(8192, 65537 - len(output))):
                output.extend(chunk)
                if len(output) > 65536:
                    return ""
            await process.wait()
            return output.decode(errors="replace")
    except (OSError, TimeoutError):
        return ""
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def inspect_browser(configuration: BrowserConfiguration | None = None) -> BrowserSetup:
    configuration = configuration or BrowserConfiguration()
    platform = host_platform()
    supported = platform in {"linux", "macos", "windows"}
    browser = None
    detail = "Select a Chromium browser in General settings, save and restart Harness UI"
    if configuration.executable:
        path = await asyncio.to_thread(executable_path, configuration.executable)
        if path and supported_browser(await probe(path, "--version")):
            browser = BrowserExecutable(name="Configured browser", path=path, source="custom")
            detail = path
        else:
            detail = "Configured browser is missing or not a compatible Chromium executable"
    automation = importlib.util.find_spec("cdp_use") is not None
    requirements = (
        BrowserRequirement(name="Chromium browser", installed=browser is not None, detail=detail),
        BrowserRequirement(
            name="CDP automation",
            installed=automation,
            detail="cdp-use is installed" if automation else "Install a13n-harness-ui[browser]",
        ),
    )
    return BrowserSetup(
        platform=platform,
        supported=supported,
        ready=supported and browser is not None and automation,
        browser=browser,
        requirements=requirements,
        message="Chromium and cdp-use are ready. No VNC or display server is required."
        if supported and browser and automation
        else "Prepare a Chromium executable and cdp-use, then check again.",
        steps=()
        if automation
        else (
            BrowserInstallStep(title="Install browser support", command="uv pip install 'a13n-harness-ui[browser]'"),
        ),
    )
