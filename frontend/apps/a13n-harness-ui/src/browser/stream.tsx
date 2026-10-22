import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { HandArrowDown, HandArrowUp } from "@phosphor-icons/react";
import { useTransport } from "../transport/context";
import { result, type Schema } from "../transport/client";
import styles from "./panel.module.css";

export type PageScale = "fit" | "50" | "75" | "100" | "125" | "150" | "200";

type Input = Record<string, string | number>;
export function PageStream({
  threadId,
  session,
  controlToken,
  busy,
  scale = "fit",
  actions,
}: {
  threadId: string;
  session: Schema<"BrowserState">;
  controlToken: string;
  busy: boolean;
  scale?: PageScale;
  actions?: ReactNode;
}) {
  const { client } = useTransport();
  const canvas = useRef<HTMLCanvasElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const [available, setAvailable] = useState({ width: 0, height: 0 });
  const [frameSize, setFrameSize] = useState({ width: 1280, height: 800 });
  const socket = useRef<WebSocket | null>(null);
  const heldKeys = useRef(new Map<string, Input>());
  const [status, setStatus] = useState("Connecting…");
  const [notice, setNotice] = useState("");
  const authority = useRef(controlToken);
  authority.current = controlToken;
  const [hint, setHint] = useState(false);
  const hintTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showHint = useCallback(() => {
    if (hintTimeout.current !== null) clearTimeout(hintTimeout.current);
    setHint(true);
    hintTimeout.current = setTimeout(() => {
      setHint(false);
      hintTimeout.current = null;
    }, 3000);
  }, []);
  useEffect(
    () => () => {
      if (hintTimeout.current !== null) clearTimeout(hintTimeout.current);
    },
    [],
  );
  const [rejected, setRejected] = useState(false);
  const [retry, setRetry] = useState(0);
  const [address, setAddress] = useState(session.url ?? "");
  const allowed = useRef(false);
  allowed.current = !!controlToken && !busy && !rejected;
  useEffect(() => {
    setRejected(false);
    setHint(false);
    if (hintTimeout.current !== null) clearTimeout(hintTimeout.current);
    hintTimeout.current = null;
    heldKeys.current.clear();
  }, [controlToken]);
  useEffect(() => setAddress(session.url ?? ""), [session.url]);
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    // Rounded client dimensions can overflow a fractional CSS layout.
    const measure = ({ width, height }: { width: number; height: number }) =>
      setAvailable((current) =>
        current.width === width && current.height === height
          ? current
          : { width, height },
      );
    measure(element.getBoundingClientRect());
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) measure(entry.contentRect);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const factor =
    scale === "fit"
      ? available.width && available.height
        ? Math.min(
            available.width / frameSize.width,
            available.height / frameSize.height,
          )
        : 1
      : Number(scale) / 100;
  const displayWidth = frameSize.width * factor;
  const displayHeight = frameSize.height * factor;
  const send = (input: Input) => {
    if (!allowed.current) return;
    if (socket.current?.readyState === WebSocket.OPEN)
      socket.current.send(
        JSON.stringify({ control_token: controlToken, input }),
      );
  };
  const releaseKeys = () => {
    for (const input of heldKeys.current.values())
      send({ ...input, type: "keyUp", text: "", modifiers: 0 });
    heldKeys.current.clear();
  };
  useEffect(() => {
    let disposed = false;
    let connection: WebSocket | null = null;
    let pending: Blob | null = null;
    let decoding = false;
    setStatus("Connecting…");
    setHint(false);
    void (async () => {
      try {
        const access = await result(
          client.POST("/api/threads/{thread_id}/browser/access", {
            params: { path: { thread_id: threadId } },
            body: {
              session_id: session.session_id!,
            },
          }),
        );
        if (disposed) return;
        const url = new URL(
          `/api/threads/${encodeURIComponent(threadId)}/browser/connect`,
          location.origin,
        );
        url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
        url.searchParams.set("ticket", access.ticket);
        connection = new WebSocket(url);
        socket.current = connection;
        const current = connection;
        const paint = async () => {
          if (decoding || disposed) return;
          decoding = true;
          try {
            while (!disposed && pending) {
              const frame = pending;
              pending = null;
              let bitmap: ImageBitmap | undefined;
              try {
                bitmap = await createImageBitmap(frame);
                if (!disposed && canvas.current) {
                  const screen = canvas.current;
                  if (
                    screen.width !== bitmap.width ||
                    screen.height !== bitmap.height
                  ) {
                    screen.width = bitmap.width;
                    screen.height = bitmap.height;
                    setFrameSize({
                      width: bitmap.width,
                      height: bitmap.height,
                    });
                  }
                  screen.getContext("2d")?.drawImage(bitmap, 0, 0);
                  setStatus("");
                }
              } catch {
                if (!disposed)
                  setStatus("Frame decoding failed. Reconnect to retry.");
              } finally {
                bitmap?.close();
              }
            }
          } finally {
            decoding = false;
          }
        };
        current.onmessage = (event) => {
          if (typeof event.data === "string") {
            const message = JSON.parse(event.data);
            if (message.type === "input_rejected") {
              setRejected(true);
              showHint();
            }
            if (
              message.type === "clipboard" &&
              allowed.current &&
              message.control_token === authority.current
            ) {
              if (!navigator.clipboard?.writeText) {
                setNotice(
                  "Clipboard access requires a supported browser and HTTPS or localhost.",
                );
                return;
              }
              void navigator.clipboard
                .writeText(message.text)
                .then(() => {
                  if (!disposed) setNotice("");
                })
                .catch(() => {
                  if (!disposed)
                    setNotice(
                      "Allow clipboard access to copy text to this device.",
                    );
                });
            }
            return;
          }
          // Keep one decode in progress and only the newest pending frame.
          pending = event.data as Blob;
          void paint();
        };
        current.onclose = () => {
          if (!disposed) setStatus("Disconnected");
        };
        current.onerror = () => {
          if (!disposed) setStatus("Connection failed");
        };
      } catch (error) {
        if (!disposed)
          setStatus(error instanceof Error ? error.message : String(error));
      }
    })();
    return () => {
      disposed = true;
      pending = null;
      connection?.close();
      if (socket.current === connection) socket.current = null;
    };
  }, [client, threadId, session.session_id, retry, showHint]);
  const coordinates = (event: { clientX: number; clientY: number }) => {
    const screen = canvas.current!;
    const rect = screen.getBoundingClientRect();
    return {
      x: Math.max(
        0,
        Math.min(
          screen.width,
          ((event.clientX - rect.left) * screen.width) / rect.width,
        ),
      ),
      y: Math.max(
        0,
        Math.min(
          screen.height,
          ((event.clientY - rect.top) * screen.height) / rect.height,
        ),
      ),
    };
  };
  const modifiers = (event: {
    altKey: boolean;
    ctrlKey: boolean;
    metaKey: boolean;
    shiftKey: boolean;
  }) =>
    (event.altKey ? 1 : 0) |
    (event.ctrlKey ? 2 : 0) |
    (event.metaKey ? 4 : 0) |
    (event.shiftKey ? 8 : 0);
  const key = (event: React.KeyboardEvent, type: "keyDown" | "keyUp") => {
    if (!allowed.current || event.nativeEvent.isComposing) return;
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "v") {
      // Keep the browser's native paste event and its user-granted clipboard
      // payload instead of requesting background clipboard-read permission.
      return;
    }
    event.preventDefault();
    const identity = event.code || event.key;
    if (type === "keyDown")
      heldKeys.current.set(identity, {
        key: event.key,
        code: event.code,
        key_code: event.keyCode,
      });
    else heldKeys.current.delete(identity);
    send({
      type,
      key: event.key,
      code: event.code,
      key_code: event.keyCode,
      modifiers: modifiers(event),
      text:
        type === "keyDown" &&
        !event.ctrlKey &&
        !event.metaKey &&
        !event.altKey &&
        event.key.length === 1
          ? event.key
          : event.key === "Enter" && type === "keyDown"
            ? "\r"
            : "",
    });
  };
  return (
    <>
      <div className={styles.navigation}>
        <form
          className={styles.addressBar}
          onSubmit={(event) => {
            event.preventDefault();
            send({ type: "navigate", text: address });
          }}
        >
          <button
            type="button"
            title="Back"
            disabled={!controlToken || busy}
            onClick={() => send({ type: "back" })}
          >
            ←
          </button>
          <button
            type="button"
            title="Forward"
            disabled={!controlToken || busy}
            onClick={() => send({ type: "forward" })}
          >
            →
          </button>
          <button
            type="button"
            title="Reload page"
            disabled={!controlToken || busy}
            onClick={() => send({ type: "reload" })}
          >
            ↻
          </button>
          <input
            aria-label="Page address"
            value={address}
            onChange={(event) => setAddress(event.target.value)}
            readOnly={!controlToken || busy}
            spellCheck={false}
          />
        </form>
        <div className={styles.navigationActions}>{actions}</div>
      </div>
      <div
        className={`${styles.pageStream} ${scale === "fit" ? styles.pageStreamFit : ""}`}
        ref={viewport}
        onPointerDown={() => {
          if (!allowed.current) showHint();
        }}
      >
        <div
          className={styles.pageSurface}
          style={{
            width: displayWidth,
            height: displayHeight,
          }}
        >
          <canvas
            ref={canvas}
            tabIndex={0}
            aria-label="Browser page stream"
            className={styles.pageCanvas}
            style={{ width: displayWidth, height: displayHeight }}
            onContextMenu={(event) => event.preventDefault()}
            onPointerMove={(event) => {
              if (allowed.current)
                send({
                  type: "mouseMoved",
                  ...coordinates(event),
                  button:
                    event.buttons & 1
                      ? "left"
                      : event.buttons & 2
                        ? "right"
                        : event.buttons & 4
                          ? "middle"
                          : "none",
                  buttons: event.buttons,
                  modifiers: modifiers(event),
                });
            }}
            onPointerDown={(event) => {
              event.preventDefault();
              event.currentTarget.focus();
              if (allowed.current)
                event.currentTarget.setPointerCapture(event.pointerId);
              send({
                type: "mousePressed",
                ...coordinates(event),
                button: ["left", "middle", "right"][event.button] ?? "none",
                buttons: event.buttons,
                click_count: Math.min(event.detail || 1, 3),
                modifiers: modifiers(event),
              });
            }}
            onPointerUp={(event) =>
              send({
                type: "mouseReleased",
                ...coordinates(event),
                button: ["left", "middle", "right"][event.button] ?? "none",
                buttons: event.buttons,
                modifiers: modifiers(event),
              })
            }
            onWheel={(event) => {
              if (allowed.current)
                send({
                  type: "mouseWheel",
                  ...coordinates(event),
                  delta_x: event.deltaX,
                  delta_y: event.deltaY,
                  modifiers: modifiers(event),
                });
            }}
            onBlur={releaseKeys}
            onKeyDown={(event) => key(event, "keyDown")}
            onKeyUp={(event) => key(event, "keyUp")}
            onPaste={(event) => {
              event.preventDefault();
              if (allowed.current) {
                send({
                  type: "text",
                  text: event.clipboardData.getData("text/plain"),
                });
                setNotice("");
              }
            }}
            onCompositionEnd={(event) =>
              send({ type: "text", text: event.data })
            }
          />
        </div>
        {hint && (!controlToken || busy || rejected) && (
          <div className={styles.controlOverlay}>
            <div className={styles.controlHint} role="status">
              <HandArrowDown size={28} aria-hidden="true" />
              <strong>
                {busy ? "Updating browser…" : "Take control to interact"}
              </strong>
              <span>
                {busy ? (
                  "Please wait until the browser is ready."
                ) : session.control === "human" ? (
                  <>
                    Click <HandArrowUp aria-label="Hand back to Agent" /> to
                    hand back, then <HandArrowDown aria-label="Take control" />{" "}
                    to take control here.
                  </>
                ) : (
                  <>
                    Click the <HandArrowDown aria-label="Take control" /> button
                    next to the address bar to take control.
                  </>
                )}
              </span>
            </div>
          </div>
        )}
        {notice && (
          <p className={styles.inputNotice} role="status">
            {notice}
          </p>
        )}
        {status && (
          <div className={styles.streamStatus}>
            {status}
            {status !== "Connecting…" && (
              <button onClick={() => setRetry((value) => value + 1)}>
                Reconnect
              </button>
            )}
          </div>
        )}
      </div>
    </>
  );
}
