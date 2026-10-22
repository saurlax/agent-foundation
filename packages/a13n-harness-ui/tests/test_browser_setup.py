from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from a13n_harness_ui import browser_setup as module
from a13n_harness_ui.configuration.models import BrowserConfiguration

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
async def test_cdp_requires_only_bound_chromium_and_cdp_use(monkeypatch, platform):
    monkeypatch.setattr(module, "host_platform", lambda: platform)
    monkeypatch.setattr(module, "executable_path", lambda value: value)
    monkeypatch.setattr(module, "probe", AsyncMock(return_value="Google Chrome 156"))
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda value: object() if value == "cdp_use" else None)
    configuration = BrowserConfiguration(executable="/opt/chrome")
    setup = await module.inspect_browser(configuration)
    assert setup.ready and len(setup.requirements) == 2
    assert not any("VNC" in item.name for item in setup.requirements)


async def test_readiness_never_selects_an_unbound_browser(monkeypatch):
    monkeypatch.setattr(module, "system_browsers", lambda _: (_ for _ in ()).throw(AssertionError("Unexpected scan")))
    setup = await module.inspect_browser()
    assert not setup.ready and setup.browser is None


async def test_custom_path_failure_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(module, "executable_path", lambda _: None)
    setup = await module.inspect_browser(BrowserConfiguration(executable="/missing/chrome"))
    assert not setup.ready and setup.browser is None


async def test_scan_filters_non_chromium_without_installing_or_selecting(monkeypatch):
    candidate = module.BrowserExecutable(name="Chrome", path="/opt/chrome", source="system")
    other = module.BrowserExecutable(name="Firefox", path="/opt/firefox", source="system")
    monkeypatch.setattr(module, "system_browsers", lambda _: [candidate, other])
    monkeypatch.setattr(module, "probe", AsyncMock(side_effect=["Google Chrome 156", "Mozilla Firefox"]))
    assert (await module.scan_browsers()).browsers == (candidate,)
