import { App, applyDocumentTheme } from "@modelcontextprotocol/ext-apps";
import { Replayer, ReplayerEvents, type eventWithTime } from "rrweb";
import type { Schema } from "../transport/client";

const app = new App({ name: "Browser DOM viewer", version: "1.0.0" });
const viewport = document.querySelector<HTMLElement>("#viewport")!;
const status = document.querySelector<HTMLElement>("#status")!;
let closed = false;
let epoch = "";
let cursor = 0;
let replayer: Replayer | undefined;
let inputLane = Promise.resolve();
let inputListeners: AbortController | undefined;
function supported(event: eventWithTime) {
  return event.type !== 3 || event.data.source !== 9;
}
function resize() {
  viewport.style.width = "100%";
  viewport.style.height = "100%";
  viewport.style.transform = "none";
  if (replayer) {
    replayer.wrapper.style.width = "100%";
    replayer.wrapper.style.height = "100%";
    replayer.iframe.style.width = "100%";
    replayer.iframe.style.height = "100%";
  }
}

new ResizeObserver(resize).observe(document.body);
app.onhostcontextchanged = (context) => {
  if (context.theme) applyDocumentTheme(context.theme);
};
app.onteardown = async () => {
  closed = true;
  inputListeners?.abort();
  replayer?.destroy();
  return {};
};
function forward(
  target: Element,
  action: Schema<"BrowserInput">["action"],
  extra: Record<string, unknown> = {},
) {
  const nodeId = replayer?.getMirror().getId(target);
  if (!epoch || nodeId === undefined || nodeId < 1) {
    status.hidden = false;
    status.textContent =
      "This element is unavailable. Wait for the current page DOM.";
    return;
  }
  const arguments_ = { epoch, node_id: nodeId, action, ...extra };
  inputLane = inputLane.then(async () => {
    if (closed) return;
    try {
      const response = await app.callServerTool({
        name: "browser_input",
        arguments: arguments_,
      });
      if (response.isError) throw new Error("Browser input was rejected.");
    } catch (cause) {
      status.hidden = false;
      status.textContent =
        cause instanceof Error
          ? cause.message
          : "Page changed; wait for the current DOM.";
    }
  });
}
function attachInput() {
  const document_ = replayer?.iframe.contentDocument;
  if (!document_) return;
  inputListeners?.abort();
  inputListeners = new AbortController();
  const options = { capture: true, signal: inputListeners.signal };
  // The child inherits the viewer's exact script hash policy. It can run these
  // trusted input listeners; site handlers/scripts and form submission are blocked.
  document_
    .querySelectorAll<HTMLInputElement>(
      'input[type="password"],input[type="file"]',
    )
    .forEach((input) => {
      input.disabled = true;
    });
  document_.addEventListener(
    "click",
    (event) => {
      event.preventDefault();
      const target = event.target;
      if (
        !(target instanceof document_.defaultView!.Element) ||
        document_.getSelection()?.isCollapsed === false
      )
        return;
      // Text selection stays local. Input fields receive focus locally and remotely.
      forward(target, "click");
    },
    options,
  );
  document_.addEventListener(
    "input",
    (event) => {
      const target = event.target as HTMLInputElement;
      if (!target || target.type === "password" || target.type === "file")
        return;
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA")
        forward(target, "fill", { text: target.value });
    },
    options,
  );
  document_.addEventListener(
    "change",
    (event) => {
      const target = event.target as HTMLSelectElement;
      if (target?.tagName === "SELECT")
        forward(target, "select", { text: target.value });
    },
    options,
  );
  document_.addEventListener(
    "keydown",
    (event) => {
      // Preserve normal typing and copy/select shortcuts in the local DOM.
      if (
        event.ctrlKey ||
        event.metaKey ||
        !["Enter", "Escape", "Tab"].includes(event.key)
      )
        return;
      if (event.key === "Enter" || event.key === "Escape")
        event.preventDefault();
      if (event.target instanceof document_.defaultView!.Element)
        forward(event.target, "press", { key: event.key });
    },
    options,
  );
  document_.addEventListener(
    "submit",
    (event) => event.preventDefault(),
    options,
  );
  let scrollTimer: ReturnType<typeof setTimeout>;
  document_.addEventListener(
    "scroll",
    (event) => {
      if (!(event as Event).isTrusted) return;
      clearTimeout(scrollTimer);
      scrollTimer = setTimeout(() => {
        const target =
          event.target === document_
            ? document_.documentElement
            : (event.target as Element);
        if (target)
          forward(target, "scroll", {
            x: Math.round(target.scrollLeft),
            y: Math.round(target.scrollTop),
          });
      }, 100);
    },
    options,
  );
}
async function receive() {
  while (!closed) {
    try {
      const uri = `ui://browser/dom?epoch=${encodeURIComponent(epoch)}&after=${cursor}`;
      const resource = await app.readServerResource({ uri });
      if (closed) return;
      const content = resource.contents.find((item) => "text" in item);
      if (!content || !("text" in content)) throw new Error("No DOM returned.");
      const batch = JSON.parse(content.text) as Schema<"BrowserDOM">;
      if (batch.epoch !== epoch) {
        replayer?.destroy();
        viewport.replaceChildren();
        const events = (batch.events as eventWithTime[]).filter(supported);
        if (!events.some((event) => event.type === 2))
          throw new Error("Waiting for the page DOM…");
        epoch = batch.epoch;
        replayer = new Replayer([], {
          root: viewport,
          liveMode: true,
          mouseTail: false,
          useVirtualDom: false,
          // rrweb uses this flag to permit parent-installed input listeners.
          // The viewer response's script hash policy still blocks all site code;
          // canvas recording and canvas mutation events are disabled.
          UNSAFE_replayCanvas: true,
          showWarning: false,
          showDebug: false,
        });
        replayer.on(ReplayerEvents.FullsnapshotRebuilded, attachInput);
        replayer.on(ReplayerEvents.Resize, resize);
        replayer.enableInteract();
        // Start directly in live mode. Past initial events are cast immediately;
        // later incremental events continue through the same live state machine.
        replayer.startLive(Date.now());
        for (const event of events) replayer.addEvent(event);
      } else {
        for (const event of batch.events)
          if (supported(event as eventWithTime))
            replayer?.addEvent(event as eventWithTime);
      }
      cursor = batch.cursor;
      status.hidden = true;
      resize();
    } catch (cause) {
      if (closed) return;
      status.hidden = false;
      status.textContent =
        cause instanceof Error
          ? `DOM unavailable · ${cause.message}`
          : "DOM unavailable";
      // Reads may reconnect; mutations are never retried.
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }
}
void app.connect().then(() => {
  const context = app.getHostContext();
  if (context?.theme) applyDocumentTheme(context.theme);
  void receive();
});
