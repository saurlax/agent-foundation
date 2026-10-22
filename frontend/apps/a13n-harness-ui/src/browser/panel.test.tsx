// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TransportContext } from "../transport/context";
import type { Transport } from "../transport/client";
import { BrowserPanel } from "./panel";
vi.mock("./stream", () => ({
  PageStream: ({
    threadId,
    controlToken,
    scale,
    actions,
  }: {
    threadId: string;
    controlToken: string;
    scale: string;
    actions?: React.ReactNode;
  }) => (
    <>
      {actions}
      <div data-testid="stream" data-scale={scale}>
        {threadId}:{controlToken || "readonly"}
      </div>
    </>
  ),
}));
afterEach(cleanup);
function show() {
  let state = {
    session_id: "binding-a",
    status: "ready",
    control: "agent",
    url: "https://example.com",
    viewport: "desktop",
    aspect_ratio: "native",
    resize_available: true,
    viewports: [
      { preset: "desktop", label: "Desktop", width: 1280, height: 800 },
      { preset: "phone", label: "Phone", width: 390, height: 844 },
    ],
  };
  const client = {
    GET: vi.fn(async () => ({ data: state })),
    POST: vi.fn(
      async (
        url: string,
        options: { body?: { preset: string; aspect_ratio: string } },
      ) => {
        if (url.endsWith("take-control")) {
          state = { ...state, control: "human" };
          return { data: { state, control_token: "controller-a" } };
        }
        if (url.endsWith("handback")) {
          state = { ...state, control: "agent" };
          return { data: { state, control_token: "" } };
        }
        if (url.endsWith("viewport"))
          state = {
            ...state,
            viewport: options.body!.preset,
            aspect_ratio: options.body!.aspect_ratio,
          };
        return { data: state };
      },
    ),
  };
  const queries = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const view = (threadId: string) => (
    <QueryClientProvider client={queries}>
      <TransportContext value={{ client } as unknown as Transport}>
        <BrowserPanel threadId={threadId} fullControl />
      </TransportContext>
    </QueryClientProvider>
  );
  const result = render(view("thread-a"));
  return { client, switchThread: () => result.rerender(view("thread-b")) };
}
it("uses one page stream for read-only and explicit control, then clears authority on handback", async () => {
  show();
  expect((await screen.findByTestId("stream")).textContent).toBe(
    "thread-a:readonly",
  );
  fireEvent.click(screen.getByRole("button", { name: "Take control" }));
  await screen.findByRole("button", { name: "Hand back to Agent" });
  expect(screen.getByTestId("stream").textContent).toBe(
    "thread-a:controller-a",
  );
  fireEvent.click(screen.getByRole("button", { name: "Hand back to Agent" }));
  await screen.findByRole("button", { name: "Take control" });
  expect(screen.getByTestId("stream").textContent).toBe("thread-a:readonly");
});
it("never passes the previous conversation's control token to another stream", async () => {
  const view = show();
  await screen.findByTestId("stream");
  fireEvent.click(screen.getByRole("button", { name: "Take control" }));
  await screen.findByRole("button", { name: "Hand back to Agent" });
  view.switchThread();
  await screen.findByText("thread-b:readonly");
});

it("keeps local zoom available without control and hides standalone lifecycle buttons", async () => {
  const { client } = show();
  const user = userEvent.setup();
  await screen.findByTestId("stream");
  expect(screen.queryByRole("button", { name: "Reset browser" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Stop browser" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Browser options" }));
  expect(
    (await screen.findByRole("menuitem", { name: /Viewport/ })).getAttribute(
      "aria-disabled",
    ),
  ).toBe("true");
  expect(
    (
      await screen.findByRole("menuitem", { name: /Aspect ratio/ })
    ).getAttribute("aria-disabled"),
  ).toBe("true");
  await user.click(await screen.findByRole("menuitem", { name: /Zoom/ }));
  fireEvent.click(await screen.findByRole("menuitemradio", { name: "150%" }));
  expect(screen.getByTestId("stream").getAttribute("data-scale")).toBe("150");
  expect(client.POST).not.toHaveBeenCalled();
});

it("moves viewport and aspect-ratio changes into the menu and binds them to current control", async () => {
  const { client } = show();
  const user = userEvent.setup();
  await screen.findByTestId("stream");
  await user.click(screen.getByRole("button", { name: "Take control" }));
  await screen.findByRole("button", { name: "Hand back to Agent" });
  await user.click(screen.getByRole("button", { name: "Browser options" }));
  await user.click(await screen.findByRole("menuitem", { name: /Viewport/ }));
  fireEvent.click(
    await screen.findByRole("menuitemradio", { name: "Phone · 390 × 844" }),
  );
  expect(client.POST).toHaveBeenCalledWith(
    expect.stringContaining("/viewport"),
    expect.objectContaining({
      body: {
        session_id: "binding-a",
        control_token: "controller-a",
        preset: "phone",
        aspect_ratio: "native",
      },
    }),
  );
  await user.keyboard("{Escape}{Escape}");
  await waitFor(() => {
    expect(
      screen
        .getByRole("button", { name: "Browser options" })
        .getAttribute("aria-expanded"),
    ).toBe("false");
    expect(
      screen
        .getByRole("button", { name: "Hand back to Agent" })
        .hasAttribute("disabled"),
    ).toBe(false);
  });
  await user.click(screen.getByRole("button", { name: "Browser options" }));
  await user.click(
    await screen.findByRole("menuitem", { name: /Aspect ratio/ }),
  );
  fireEvent.click(await screen.findByRole("menuitemradio", { name: "1:1" }));
  expect(client.POST).toHaveBeenLastCalledWith(
    expect.stringContaining("/viewport"),
    expect.objectContaining({
      body: {
        session_id: "binding-a",
        control_token: "controller-a",
        preset: "phone",
        aspect_ratio: "1:1",
      },
    }),
  );
});
