// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserPanel } from "./panel";

const client = vi.hoisted(() => ({ GET: vi.fn(), POST: vi.fn() }));
vi.mock("../transport/context", () => ({ useTransport: () => ({ client }) }));
vi.mock("../mcp-apps/app-frame", () => ({
  AppFrame: ({
    handlers,
  }: {
    handlers: { oncalltool: (request: object) => Promise<unknown> };
  }) => (
    <button
      onClick={() =>
        void handlers
          .oncalltool({
            name: "browser_input",
            arguments: {
              session_id: "other-session",
              epoch: "document-a",
              node_id: 8,
              action: "fill",
              text: "Human",
            },
          })
          .catch(() => {})
      }
    >
      Type in mirrored page
    </button>
  ),
}));

const state = {
  enabled: true,
  status: "ready",
  session_id: "browser-a",
  url: "http://localhost:3000",
  title: "Fixture",
  error: null,
};
beforeEach(() => {
  client.GET.mockImplementation(async (path: string) => ({
    data: path.endsWith("presentation")
      ? {
          state,
          sandbox_url: "http://localhost:9001/sandbox.html",
          html: "<p>Viewer</p>",
        }
      : state,
  }));
  client.POST.mockResolvedValue({ data: state });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response("<p>Viewer</p>")),
  );
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});
function show() {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <BrowserPanel threadId="thread-a" fullControl />
    </QueryClientProvider>,
  );
}

it("binds human input to the trusted conversation and session", async () => {
  show();
  fireEvent.click(
    await screen.findByRole("button", { name: "Type in mirrored page" }),
  );
  await waitFor(() =>
    expect(client.POST).toHaveBeenCalledWith(
      "/api/threads/{thread_id}/browser/input",
      {
        params: { path: { thread_id: "thread-a" } },
        body: {
          session_id: "browser-a",
          epoch: "document-a",
          node_id: 8,
          action: "fill",
          text: "Human",
        },
      },
    ),
  );
});

it("does not retry an uncertain human input or stop the browser when the viewer closes", async () => {
  client.POST.mockRejectedValueOnce(new Error("Connection lost"));
  const view = show();
  fireEvent.click(
    await screen.findByRole("button", { name: "Type in mirrored page" }),
  );
  await waitFor(() => expect(client.POST).toHaveBeenCalledTimes(1));
  view.unmount();
  expect(client.POST).toHaveBeenCalledTimes(1);
});

it("navigation uses an explicit session-bound request", async () => {
  show();
  await screen.findByRole("button", { name: "Type in mirrored page" });
  const address = screen.getByRole("textbox", { name: "Browser address" });
  fireEvent.change(address, {
    target: { value: "http://localhost:3000/next" },
  });
  fireEvent.submit(address.closest("form")!);
  await waitFor(() =>
    expect(client.POST).toHaveBeenCalledWith(
      "/api/threads/{thread_id}/browser/navigate",
      {
        params: { path: { thread_id: "thread-a" } },
        body: { session_id: "browser-a", url: "http://localhost:3000/next" },
      },
    ),
  );
});
