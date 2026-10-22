# Browser Use Capability

## Design Position

`BrowserUseCapability` supplies native browser tools over an embedding Host's explicitly supplied browser backend. Harness owns tool definitions, instructions, managed permission metadata, observation rendering and compact element references. The Host owns browser process creation, profiles, endpoint selection, access and human takeover. Harness provides the optional CDP connection and page-operation adapter; the Host bounds that connection lifetime. The Capability neither launches browsers nor implements a remote desktop or a provider platform.

## CDP Adapter

Install the optional `a13n-harness[browser]` extra to use `a13n_harness.capabilities.browser.cdp`. `CdpBrowserBackend.connect(cdp_url)` is an async context manager accepting a trusted HTTP(S) CDP base URL or WS(S) browser debugger URL. HTTP(S) discovery reads `/json/version`; the endpoint remains Host-only connection input and is never an Agent tool argument or continuation state. The adapter supports Chromium-compatible browsers through cdp-use and does not launch or install a browser.

The context creates one dedicated page and yields a `BrowserBackend` usable by `BrowserUseCapability`. All operations target that explicit page and use its operation lock. On exit, including failure or cancellation, the context closes its created page and disconnects its CDP client. It does not close the supplied browser process or other pages. The Host keeps this context alive for every Run that borrows its backend. The standalone backend has no human-control owner; applications that provide takeover wrap the same operations with their current authority checks.

`Chromium.connect(cdp_url)` exposes the shared connection for Hosts such as Harness UI that manage several page targets. Harness owns page observation, element actions, keyboard and pointer dispatch, navigation, screenshots, viewport emulation and CDP screencast acknowledgements. The Host owns page-to-Thread assignment, popup adoption, authenticated viewers and takeover admission. Validated keyboard input maps Ctrl/Cmd editing shortcuts to Chromium commands. Copy and cut may return the selected plain text to the embedding Host without reading the OS clipboard; password selections are excluded. The Host owns viewer authorization and any local clipboard transfer. Human page-input navigation returns after dispatch rather than holding the input queue until DOMContentLoaded; Agent navigation retains its document-ready wait. While a page is streamed, the adapter emulates focused/visible state for that target so another active target cannot suppress hover rendering; it restores normal focus behavior when the stream stops or startup fails. This does not switch the browser’s foreground target. Disconnecting the adapter never implicitly sends `Browser.close`; only the browser process owner invokes that explicit operation.

## Observation and Actions

Navigation, observation, screenshots, click, ordinary text fill, supported keys and condition-based text waiting use the existing native FunctionToolset and Harness approval path. Website content is untrusted external data. Mutations have no automatic replay guarantee; read-only observations and bounded output use canonical tool metadata.

Each Run has fresh Capability reference state. An observation returns a bounded page description, backend observation revision and element identities. Harness exposes compact references such as `e1`; those references resolve only through that Run's latest observation. Navigation clears references. The backend validates the revision and actual element identity before dispatch, rejecting stale or detached targets instead of inferring a replacement from an index or label.

The backend rechecks current Host authority for every call. During human control all model-facing browser operations are unavailable, including page observations and screenshots. A browser tool requested during human control waits for explicit handback without collecting page content or polling the model. On release it returns an instruction to observe afresh; the pending operation is not replayed. Other explicit browser failures become tool results without being treated as successful actions. After handback, the Host requires a fresh observation before another mutation and invalidates prior element identities.

A restrictive `RunConfiguration.allowed_hosts` is rejected before browser tools become available. The browser owns opaque subresource, redirect and background traffic and cannot enforce that per-request policy. Hosts use Environment or deployment network isolation for the browser; an unrestricted Run does not itself grant access to a backend.

The Capability borrows its backend and never closes the Host browser when a Run ends. [Harness UI Host Browser](../a13n-harness-ui/11-browser.md) owns the built-in Chromium runtime and page-stream contract.
