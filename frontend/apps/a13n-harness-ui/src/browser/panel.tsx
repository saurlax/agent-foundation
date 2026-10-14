import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Globe, LockSimple, Stop } from "@phosphor-icons/react";
import { Button } from "a13n-ui";
import { AppFrame } from "../mcp-apps/app-frame";
import { result, type Schema } from "../transport/client";
import { useTransport } from "../transport/context";
import styles from "./panel.module.css";

const args = {};
const emptyResult = { content: [] };
const capabilities = { serverResources: {}, serverTools: {} };

export function BrowserPanel({
  threadId,
  fullControl,
}: {
  threadId?: string;
  fullControl: boolean;
}) {
  const { client } = useTransport();
  const [busy, setBusy] = useState(false);
  const [address, setAddress] = useState<string>();
  const [error, setError] = useState<string>();
  const [frame, setFrame] = useState<Schema<"BrowserDOM">>();
  const [now, setNow] = useState(Date.now());
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      mounted.current = false;
      clearInterval(timer);
    };
  }, []);
  const state = useQuery({
    queryKey: ["browser", threadId],
    enabled: !!threadId && fullControl,
    queryFn: () =>
      result(
        client.GET("/api/threads/{thread_id}/browser", {
          params: { path: { thread_id: threadId! } },
        }),
      ),
    refetchInterval: 2000,
    retry: false,
  });
  const session = state.data;
  const presentation = useQuery({
    queryKey: ["browser-presentation", threadId, session?.session_id],
    enabled: !!threadId && session?.status === "ready",
    queryFn: () =>
      result(
        client.GET("/api/threads/{thread_id}/browser/presentation", {
          params: { path: { thread_id: threadId! } },
        }),
      ),
    retry: false,
  });
  const handlers = useMemo(
    () => ({
      onreadresource: async ({ uri }: { uri: string }) => {
        const resource = new URL(uri);
        if (
          resource.protocol !== "ui:" ||
          resource.hostname !== "browser" ||
          resource.pathname !== "/dom" ||
          [...resource.searchParams.keys()].some(
            (key) => !["epoch", "after"].includes(key),
          ) ||
          !mounted.current ||
          !threadId ||
          !session?.session_id
        )
          throw new Error(
            "This resource is unavailable in the browser viewer.",
          );
        try {
          const value = await result(
            client.GET("/api/threads/{thread_id}/browser/dom", {
              params: {
                path: { thread_id: threadId },
                query: {
                  session_id: session.session_id,
                  epoch: resource.searchParams.get("epoch") ?? "",
                  after: Number(resource.searchParams.get("after") ?? 0),
                },
              },
            }),
          );
          if (!mounted.current) throw new Error("Viewer closed.");
          setFrame(value);
          setError(undefined);
          return {
            contents: [
              {
                uri,
                mimeType: "application/json",
                text: JSON.stringify(value),
              },
            ],
          };
        } catch (cause) {
          if (mounted.current)
            setError(
              cause instanceof Error
                ? cause.message
                : "Browser DOM unavailable.",
            );
          throw cause;
        }
      },
      oncalltool: async ({
        name,
        arguments: value,
      }: {
        name: string;
        arguments?: Record<string, unknown>;
      }) => {
        if (
          name !== "browser_input" ||
          !mounted.current ||
          !threadId ||
          !session?.session_id
        )
          throw new Error(
            "This operation is unavailable in the browser viewer.",
          );
        // Session and Thread scope come from the trusted panel, never the App payload.
        const body = {
          ...value,
          session_id: session.session_id,
        } as Schema<"BrowserInput">;
        const response = await result(
          client.POST("/api/threads/{thread_id}/browser/input", {
            params: { path: { thread_id: threadId } },
            body,
          }),
        );
        return {
          content: [{ type: "text" as const, text: JSON.stringify(response) }],
        };
      },
    }),
    [client, threadId, session?.session_id],
  );
  async function start() {
    if (!threadId) return;
    setBusy(true);
    setError(undefined);
    try {
      if (session?.status === "disconnected" && session.session_id) {
        await result(
          client.POST("/api/threads/{thread_id}/browser/stop", {
            params: {
              path: { thread_id: threadId },
              query: { session_id: session.session_id },
            },
          }),
        );
        setFrame(undefined);
      }
      const enabled =
        session?.enabled && session.status !== "disconnected"
          ? session
          : await result(
              client.POST("/api/threads/{thread_id}/browser/enable", {
                params: { path: { thread_id: threadId } },
              }),
            );
      await result(
        client.POST("/api/threads/{thread_id}/browser/start", {
          params: {
            path: { thread_id: threadId },
            query: { session_id: enabled.session_id! },
          },
        }),
      );
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Browser could not start.",
      );
    } finally {
      await state.refetch();
      setBusy(false);
    }
  }
  async function stop() {
    if (!threadId || !session?.session_id) return;
    setBusy(true);
    setError(undefined);
    try {
      await result(
        client.POST("/api/threads/{thread_id}/browser/stop", {
          params: {
            path: { thread_id: threadId },
            query: { session_id: session.session_id },
          },
        }),
      );
      setFrame(undefined);
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Browser could not stop. Check its state before retrying.",
      );
    } finally {
      await state.refetch();
      setBusy(false);
    }
  }
  async function navigate() {
    if (!threadId || !session?.session_id || !address) return;
    setBusy(true);
    setError(undefined);
    try {
      await result(
        client.POST("/api/threads/{thread_id}/browser/navigate", {
          params: { path: { thread_id: threadId } },
          body: { session_id: session.session_id, url: address },
        }),
      );
      setAddress(undefined);
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Navigation failed. Check the current page before retrying.",
      );
    } finally {
      setBusy(false);
      await state.refetch();
    }
  }
  const age = frame
    ? Math.max(0, Math.floor((now - Date.parse(frame.observed_at)) / 1000))
    : undefined;
  const live =
    session?.status === "ready" && age !== undefined && age < 20 && !error;
  const message = error ?? state.error?.message ?? presentation.error?.message;
  return (
    <section className={styles.panel} aria-label="Browser session">
      <div className={styles.bar}>
        <LockSimple size={14} aria-hidden="true" />
        <form
          className={styles.addressForm}
          onSubmit={(event) => {
            event.preventDefault();
            void navigate();
          }}
        >
          <input
            aria-label="Browser address"
            className={styles.address}
            value={address ?? frame?.state.url ?? session?.url ?? ""}
            placeholder="Enter a URL"
            readOnly={busy || session?.status !== "ready"}
            onChange={(event) => setAddress(event.target.value)}
          />
        </form>
        <span
          className={`${styles.state} ${live ? styles.live : ""}`}
          role="status"
        >
          <span className={styles.dot} />
          {live
            ? "Live"
            : frame
              ? "Stale"
              : session?.status === "ready"
                ? "Connecting"
                : "Stopped"}
        </span>
      </div>
      {message && (
        <p className={styles.error} role="alert">
          {message}
        </p>
      )}
      {!threadId || !fullControl ? (
        <div className={styles.empty}>
          <Globe />
          <h3>{threadId ? "Full Control required" : "Open a conversation"}</h3>
          <p>
            {threadId
              ? "The browser runs on the server’s computer. Select Full Control to enable it."
              : "Each conversation uses its own browser session."}
          </p>
        </div>
      ) : session?.status === "ready" && presentation.data ? (
        <div className={styles.viewer}>
          <AppFrame
            trustedBrowserViewer
            layout="panel"
            title="Browser page"
            sandboxUrl={presentation.data.sandbox_url}
            html=""
            arguments={args}
            result={emptyResult}
            capabilities={capabilities}
            handlers={handlers}
          />
        </div>
      ) : (
        <div className={styles.empty}>
          <Globe />
          <h3>
            {session?.status === "ready"
              ? "Connecting to browser"
              : "A browser beside your conversation"}
          </h3>
          <p>
            You and the Agent can inspect and operate the same page. This
            browser uses a separate, temporary profile on the server’s computer.
          </p>
          <Button
            disabled={busy || state.isPending || session?.status === "ready"}
            onClick={() => void start()}
          >
            {busy
              ? "Starting…"
              : session?.enabled
                ? "Start browser"
                : "Enable browser"}
          </Button>
        </div>
      )}
      <footer
        className={styles.footer}
        title="DOM view: canvas, media, cross-origin frames, password and file inputs are not mirrored. External assets may differ."
      >
        <span>
          Shared page · {frame ? `${age}s since sync` : "Host browser"}
        </span>
        {session?.enabled && (
          <Button
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() => void stop()}
          >
            <Stop size={13} />
            Stop browser
          </Button>
        )}
      </footer>
    </section>
  );
}
