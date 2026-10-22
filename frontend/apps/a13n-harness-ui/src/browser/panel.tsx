import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Menu,
  MenuTrigger,
  MenuPopup,
  MenuSub,
  MenuSubTrigger,
  MenuSubPopup,
  MenuRadioGroup,
  MenuRadioItem,
} from "a13n-ui";
import {
  CornersOut,
  Devices,
  DotsThree,
  HandArrowDown,
  HandArrowUp,
  LockSimple,
  MagnifyingGlassPlus,
} from "@phosphor-icons/react";
import { useTransport } from "../transport/context";
import { result, type Schema } from "../transport/client";
import { PageStream, type PageScale } from "./stream";
import { BrowserSetup } from "./setup";
import styles from "./panel.module.css";

export function BrowserPanel({
  threadId,
  fullControl,
}: {
  threadId?: string;
  fullControl: boolean;
}) {
  const { client } = useTransport();
  const queries = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [scale, setScale] = useState<PageScale>("fit");
  const [error, setError] = useState("");
  const [authority, setAuthority] = useState<{
    threadId: string;
    binding: string;
    token: string;
  } | null>(null);
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
  const controlToken =
    authority &&
    authority.threadId === threadId &&
    authority.binding === session?.session_id
      ? authority.token
      : "";
  const preparation = useQuery({
    queryKey: ["browser-setup", threadId],
    enabled:
      !!threadId &&
      fullControl &&
      state.isSuccess &&
      session?.status !== "ready",
    queryFn: () =>
      result(
        client.GET("/api/threads/{thread_id}/browser/setup", {
          params: { path: { thread_id: threadId! } },
        }),
      ),
    refetchOnWindowFocus: false,
    retry: false,
  });
  const refresh = () => queries.invalidateQueries({ queryKey: ["browser"] });
  const command = async (
    kind: "enable" | "start" | "take-control" | "handback",
  ) => {
    if (!threadId || busy) return;
    setBusy(true);
    setError("");
    try {
      const path = {
        params: { path: { thread_id: threadId } },
        body: { session_id: session?.session_id ?? "" },
      };
      if (kind === "enable")
        await result(
          client.POST("/api/threads/{thread_id}/browser/enable", {
            params: path.params,
          }),
        );
      if (kind === "start")
        await result(
          client.POST("/api/threads/{thread_id}/browser/start", path),
        );
      if (kind === "take-control") {
        const value = await result(
          client.POST("/api/threads/{thread_id}/browser/take-control", path),
        );
        queries.setQueryData(["browser", threadId], value.state);
        setAuthority({
          threadId,
          binding: value.state.session_id!,
          token: value.control_token,
        });
      }
      if (kind === "handback") {
        const value = await result(
          client.POST("/api/threads/{thread_id}/browser/handback", path),
        );
        queries.setQueryData(["browser", threadId], value.state);
        setAuthority(null);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      if (kind === "enable" || kind === "start") await preparation.refetch();
    } finally {
      await refresh();
      setBusy(false);
    }
  };
  const changeViewport = async (
    preset: Schema<"BrowserViewportRequest">["preset"],
    aspect_ratio: NonNullable<
      Schema<"BrowserViewportRequest">["aspect_ratio"]
    > = "native",
  ) => {
    if (!threadId || !session?.session_id || !controlToken || busy) return;
    setBusy(true);
    setError("");
    try {
      const value = await result(
        client.POST("/api/threads/{thread_id}/browser/viewport", {
          params: { path: { thread_id: threadId } },
          body: {
            session_id: session.session_id,
            control_token: controlToken,
            preset,
            aspect_ratio,
          },
        }),
      );
      queries.setQueryData(["browser", threadId], value);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      await refresh();
      setBusy(false);
    }
  };
  useEffect(() => {
    if (session?.control !== "human") setAuthority(null);
  }, [session?.control]);
  useEffect(() => {
    setAuthority(null);
  }, [threadId, session?.session_id]);
  if (!threadId || !fullControl)
    return (
      <div className={styles.empty}>
        <LockSimple size={28} />
        <strong>Browse on the Host</strong>
        <p>Open a Full Control conversation to use the shared Host browser.</p>
      </div>
    );
  const ready = session?.status === "ready";
  const stopped = session?.status === "stopped" || session?.status === "failed";
  const human = session?.control === "human";
  const canResize = !busy && !!controlToken && !!session?.resize_available;
  const viewport = session?.viewports?.find(
    (item) => item.preset === session.viewport,
  );
  const ratio = session?.aspect_ratio ?? "native";
  const actions = ready ? (
    <>
      <Button
        size="icon-sm"
        aria-label={human ? "Hand back to Agent" : "Take control"}
        variant={human ? "default" : "outline"}
        title={
          human
            ? "Hand back to Agent · Resume browser tools in all conversations"
            : "Take control · Pause browser tools in all conversations"
        }
        disabled={busy}
        onClick={() => void command(human ? "handback" : "take-control")}
      >
        {human ? <HandArrowUp /> : <HandArrowDown />}
      </Button>
      <Menu>
        <MenuTrigger
          render={<Button size="icon-sm" variant="ghost" />}
          aria-label="Browser options"
          title="Browser options"
        >
          <DotsThree />
        </MenuTrigger>
        <MenuPopup align="end" className={styles.optionsMenu}>
          <MenuSub>
            <MenuSubTrigger>
              <MagnifyingGlassPlus />
              Zoom
              <span className={styles.menuValue}>
                {scale === "fit" ? "Fit" : `${scale}%`}
              </span>
            </MenuSubTrigger>
            <MenuSubPopup>
              <MenuRadioGroup
                value={scale}
                onValueChange={(value) => setScale(value as PageScale)}
              >
                <MenuRadioItem value="fit">Fit to panel</MenuRadioItem>
                {(["50", "75", "100", "125", "150", "200"] as const).map(
                  (value) => (
                    <MenuRadioItem key={value} value={value}>
                      {value}%
                    </MenuRadioItem>
                  ),
                )}
              </MenuRadioGroup>
            </MenuSubPopup>
          </MenuSub>
          <MenuSub>
            <MenuSubTrigger
              disabled={!canResize}
              title={
                canResize
                  ? "Resize the page"
                  : "Take control to resize the page"
              }
            >
              <Devices />
              Viewport
              <span className={styles.menuValue}>{viewport?.label}</span>
            </MenuSubTrigger>
            <MenuSubPopup>
              <MenuRadioGroup
                value={session.viewport}
                onValueChange={(value) =>
                  void changeViewport(
                    value as Schema<"BrowserViewportRequest">["preset"],
                  )
                }
              >
                {(session.viewports ?? []).map((item) => (
                  <MenuRadioItem key={item.preset} value={item.preset}>
                    {item.label} · {item.width} × {item.height}
                  </MenuRadioItem>
                ))}
              </MenuRadioGroup>
            </MenuSubPopup>
          </MenuSub>
          <MenuSub>
            <MenuSubTrigger
              disabled={!canResize}
              title={
                canResize
                  ? "Change the page aspect ratio"
                  : "Take control to change the page aspect ratio"
              }
            >
              <CornersOut />
              Aspect ratio
              <span className={styles.menuValue}>
                {ratio === "native" ? "Default" : ratio}
              </span>
            </MenuSubTrigger>
            <MenuSubPopup>
              <MenuRadioGroup
                value={ratio}
                onValueChange={(value) =>
                  void changeViewport(
                    session.viewport ?? "desktop",
                    value as NonNullable<
                      Schema<"BrowserViewportRequest">["aspect_ratio"]
                    >,
                  )
                }
              >
                <MenuRadioItem value="native">Device default</MenuRadioItem>
                {["16:9", "4:3", "1:1", "9:16"].map((value) => (
                  <MenuRadioItem key={value} value={value}>
                    {value}
                  </MenuRadioItem>
                ))}
              </MenuRadioGroup>
            </MenuSubPopup>
          </MenuSub>
        </MenuPopup>
      </Menu>
    </>
  ) : null;
  return (
    <section className={styles.panel} aria-label="Host browser">
      {(error || state.error || session?.error) && (
        <p className={styles.notice} role="alert">
          {error || state.error?.message || session?.error}
        </p>
      )}
      {ready ? (
        <PageStream
          key={session.session_id}
          threadId={threadId}
          session={session}
          controlToken={human ? controlToken : ""}
          busy={busy}
          scale={scale}
          actions={actions}
        />
      ) : !preparation.data?.ready ? (
        <BrowserSetup
          setup={preparation.data}
          loading={preparation.isFetching || state.isLoading}
          error={preparation.error}
          check={() => void preparation.refetch()}
          busy={busy}
        />
      ) : (
        <div className={styles.empty}>
          <LockSimple size={28} />
          <strong>
            {stopped ? "Browser stopped" : "Your browser on the Host"}
          </strong>
          <p>
            {stopped
              ? "Reopen this conversation’s page with your shared browser profile."
              : "One shared browser profile for all conversations and projects. Each conversation uses its own page. Taking control pauses browser tools in every conversation."}
          </p>
          <Button
            disabled={busy || state.isLoading || preparation.isFetching}
            onClick={() =>
              void command(session?.session_id ? "start" : "enable")
            }
          >
            {busy
              ? "Preparing…"
              : session?.session_id
                ? "Start browser"
                : "Enable browser"}
          </Button>
        </div>
      )}
    </section>
  );
}
