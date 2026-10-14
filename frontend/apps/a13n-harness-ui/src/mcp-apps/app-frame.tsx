import {
  AppBridge,
  PostMessageTransport,
  type McpUiHostCapabilities,
  type McpUiHostContext,
  type McpUiResourceCsp,
} from "@modelcontextprotocol/ext-apps/app-bridge";
import type { CallToolResult } from "@modelcontextprotocol/client";
import { useEffect, useRef, useState } from "react";

export type AppHandlers = Partial<
  Pick<
    AppBridge,
    | "oncalltool"
    | "onreadresource"
    | "onmessage"
    | "onopenlink"
    | "onupdatemodelcontext"
  >
>;

interface Props {
  trustedBrowserViewer?: boolean;
  layout?: "inline" | "panel";
  title: string;
  sandboxUrl: string;
  html: string;
  csp?: McpUiResourceCsp;
  arguments: Record<string, unknown>;
  result: CallToolResult;
  capabilities: McpUiHostCapabilities;
  handlers: AppHandlers;
}

/** One public SDK bridge per View; the iframe never receives an MCP client or credentials. */
export function AppFrame(props: Props) {
  const container = useRef<HTMLDivElement>(null);
  const handlers = useRef(props.handlers);
  handlers.current = props.handlers;
  const [error, setError] = useState<string>();

  useEffect(() => {
    const node = container.current;
    if (!node) return;
    let disposed = false;
    setError(undefined);
    const fail = (cause: unknown) => {
      if (!disposed)
        setError(cause instanceof Error ? cause.message : String(cause));
    };
    const url = new URL(props.sandboxUrl);
    if (url.origin === window.location.origin) {
      fail(new Error("MCP Apps require a separate sandbox origin."));
      return;
    }
    url.searchParams.set("host", window.location.origin);
    url.searchParams.set("csp", JSON.stringify(props.csp ?? {}));
    const frame = document.createElement("iframe");
    frame.title = props.title;
    frame.sandbox.add("allow-scripts", "allow-same-origin");
    frame.referrerPolicy = "no-referrer";
    frame.style.cssText =
      props.layout === "panel"
        ? "width:100%;height:100%;border:0;display:block"
        : "width:100%;height:320px;max-height:720px;border:0;display:block";
    let initialized = false;
    const hostContext = (): McpUiHostContext => ({
      theme: document.documentElement.classList.contains("dark")
        ? "dark"
        : "light",
      locale: document.documentElement.lang || navigator.language,
      displayMode: "inline",
      availableDisplayModes: ["inline"],
      containerDimensions: props.layout === "panel" ? {} : { maxHeight: 720 },
    });
    const bridge = new AppBridge(
      null,
      { name: "Harness UI", version: "1.0.0" },
      props.capabilities,
      { hostContext: hostContext() },
    );
    const unsupported = () =>
      Promise.reject(new Error("This operation is unavailable in this View."));
    bridge.oncalltool = (params, extra) =>
      handlers.current.oncalltool?.(params, extra) ?? unsupported();
    bridge.onreadresource = (params, extra) =>
      handlers.current.onreadresource?.(params, extra) ?? unsupported();
    bridge.onmessage = (params, extra) =>
      handlers.current.onmessage?.(params, extra) ?? unsupported();
    bridge.onopenlink = (params, extra) =>
      handlers.current.onopenlink?.(params, extra) ?? unsupported();
    bridge.onupdatemodelcontext = (params, extra) =>
      handlers.current.onupdatemodelcontext?.(params, extra) ?? unsupported();
    bridge.onrequestdisplaymode = async () => ({ mode: "inline" });
    const updateHostContext = () => {
      if (initialized) bridge.setHostContext(hostContext());
    };
    const observer = new MutationObserver(updateHostContext);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "lang"],
    });
    window.addEventListener("languagechange", updateHostContext);
    const startup = setTimeout(
      () =>
        fail(
          new Error(
            "The App did not initialize. Check that the sandbox origin is reachable from this browser.",
          ),
        ),
      15000,
    );
    frame.onerror = () =>
      fail(new Error("The App sandbox could not be loaded."));
    bridge.onsizechange = ({ height }) => {
      if (
        props.layout !== "panel" &&
        height !== undefined &&
        Number.isFinite(height)
      )
        frame.style.height = `${Math.max(80, Math.min(720, height))}px`;
    };
    bridge.onsandboxready = () => {
      if (props.trustedBrowserViewer) return;
      void bridge
        .sendSandboxResourceReady({ html: props.html, csp: props.csp })
        .catch(fail);
    };
    bridge.oninitialized = () => {
      initialized = true;
      updateHostContext();
      clearTimeout(startup);
      if (!disposed) setError(undefined);
      void (async () => {
        await bridge.sendToolInput({ arguments: props.arguments });
        await bridge.sendToolResult(props.result);
      })().catch(fail);
    };
    bridge.onerror = fail;
    node.append(frame);
    const target = frame.contentWindow;
    if (target) {
      void bridge
        .connect(new PostMessageTransport(target, target))
        .then(() => {
          if (!disposed) frame.src = url.href;
        })
        .catch(fail);
    }
    return () => {
      disposed = true;
      clearTimeout(startup);
      observer.disconnect();
      window.removeEventListener("languagechange", updateHostContext);
      void bridge.close();
      frame.remove();
    };
  }, [
    props.title,
    props.sandboxUrl,
    props.html,
    props.csp,
    props.arguments,
    props.result,
    props.capabilities,
    props.layout,
    props.trustedBrowserViewer,
  ]);

  return (
    <div
      style={
        props.layout === "panel"
          ? { height: "100%", display: "flex", flexDirection: "column" }
          : undefined
      }
    >
      {error && (
        <p role="alert">
          {error}{" "}
          {props.layout === "panel"
            ? "Close and reopen the viewer to reconnect. Browser actions are not replayed."
            : "The original tool result remains in the conversation. Close and reopen the View to retry; the tool will not be called again."}
        </p>
      )}
      <div
        ref={container}
        style={props.layout === "panel" ? { flex: 1, minHeight: 0 } : undefined}
      />
    </div>
  );
}
