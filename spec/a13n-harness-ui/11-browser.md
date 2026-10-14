# Built-in Host Browser

## Design Position

Harness UI provides an optional browser on the WebUI server's computer for active root Full Control conversations. Agent tools and the Browser panel operate the same live page. The panel mirrors DOM changes rather than repeatedly painting screenshots: users can select and copy text, navigate, click, type, select options and use supported keys while the Agent continues. Actual page scripts execute only in the Host browser.

This is a Harness UI Host facility, outside Agent Environment isolation. It requires WebUI computer sharing and explicit conversation enablement. CLI, Service, Console, child Threads and memory organization do not acquire this facility. It introduces no public Harness browser abstraction or external MCP server configuration.

## Session and Authority

The App owns one process-local browser binding per enabled root Thread and a fresh `browser_` identity for each binding. Enablement requires an idle, active Full Control root. Enablement creates no browser process; explicit Start or the admitted Agent's first navigation starts Chromium. A Run captures the current binding at admission. Enablement afterward cannot add tools to that Run, and an old binding cannot operate a replacement session.

The browser uses a disposable context with a fixed 1280 by 800 viewport. No personal profile, account cookies or browser history is imported. The Host bounds enablement to sixteen conversations and running browsers to four. The profile is discarded on explicit Stop, archival or Host shutdown. There is no idle TTL or durable browser recovery. Closing, resizing or switching the viewer does not stop Chromium or pause the Agent. Normal and graceful Host restart require explicit new enablement; saved conversation history does not revive a browser or replay its actions.

Every browser read and input checks the current root, archival state and Full Control selection. Checks finish before browser I/O, with no retained database transaction. Stop retires the binding before waiting for in-flight work; queued calls then fail. Agent navigation, click, fill and key tools use normal Harness tool permission handling. Human panel input is an authenticated, explicit UI action, not a fabricated Agent Run or model request. Human and Agent input share one serialized page-operation lane; neither acquires exclusive control.

## DOM Projection and Input

The Host records the main document's initial snapshot and subsequent DOM, style, input and scroll changes through rrweb. Each document recording has an independent epoch. Reads return the current browser state, document epoch, event cursor, observation time and a bounded event batch. Long reads wait up to ten seconds outside the page-operation lane, then recheck authority and return either changes or a current-state heartbeat. Event retention is bounded to 512 events and four MiB, with a two MiB individual event limit. A new full snapshot replaces an exhausted incremental history; a viewer does not infer continuity across different epochs.

The built-in viewer uses the public MCP Apps bridge for DOM resource reads and narrowly scoped input requests. The trusted panel supplies the Thread and browser binding; App arguments cannot choose another conversation. This live projection does not manufacture or replace a retained tool-result App original. Ordinary [MCP Apps](09-mcp-apps.md) retain their existing inline display, enablement and operation contracts; general third-party panel display modes are not introduced.

Input identifies the exact document epoch and rrweb node identity. The Host resolves that identity in the actual current browser document and rejects disconnected, removed or stale nodes before dispatch. Input supports click, ordinary text fill, option selection, supported keys and scroll. The original browser produces resulting mutations, which are projected back to every viewer. Local text selection and copying do not dispatch browser mutations.

The DOM viewer supports ordinary main-document content, not full visual fidelity for every website. Cross-origin frames, canvas, media and file inputs are blocked from recording. Password values are masked and password/file input through the viewer is unavailable. Inlined styles and images are retained where supported; external resources without an inlined representation may differ or be blocked. Agent screenshots remain available for visual observation of the actual browser, without becoming the viewer transport.

## Browser Isolation and Failures

The distribution-owned viewer runs directly on the separate-origin credential-free sandbox listener, allowing its trusted SDK code to access its child document. Third-party Apps retain the opaque inner sandbox. rrweb rebuilds website DOM in a further iframe with script-capable same-origin access for trusted input listeners. The HTTP CSP permits only the exact bundled viewer script hash, excludes inline handlers and external script sources, and is inherited by the rebuilt document. Website scripts and handlers cannot execute; forms and popups remain disabled. Canvas recording and mutation events are disabled independently. The viewer receives no Host key, MCP credentials or unrestricted browser client. It grants no external network origins. A non-loopback deployment requires a browser-visible separate sandbox URL under the existing sandbox configuration. Opening the built-in viewer does not enable third-party MCP Apps.

Browser startup, DOM synchronization and input completion are separate facts. The panel exposes the page URL, session state, connection and observation freshness. A failed read preserves the previous DOM as stale; reconnecting reads never repeats an input. A navigation or input failure may have an uncertain effect: the caller observes the current page before any deliberate retry. Neither transport disconnection nor a lost response proves rollback or exactly-once execution.

## Runtime and Distribution

Playwright is an optional `browser` extra of the Python Harness UI distribution. Operators explicitly prepare Chromium and required OS dependencies; ordinary package installation, Files and Shell use neither prepare nor launch a browser. Playwright's Python package owns its driver runtime, so installed use requires no separately installed Node.js. Frontend builds bundle the DOM recorder and viewer alongside WebUI assets; wheel and sdist rebuilding preserve the existing prebuilt-asset contract.

## Invariants

1. User input and Agent tools address one actual Host page with current Thread authority.
2. Viewer closure does not terminate or acquire exclusive control of the browser.
3. No browser profile or pending input is restored from conversation history or Host restart.
4. Old session bindings and document epochs cannot silently target a replacement page.
5. Website JavaScript executes only in the original browser, never in the rebuilt DOM viewer.
6. Browser preparation is optional and does not change file/Shell-only installed operation.
