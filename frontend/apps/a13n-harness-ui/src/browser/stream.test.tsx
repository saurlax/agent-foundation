// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { PageStream, type PageScale } from "./stream";
const { post, transport } = vi.hoisted(() => {
  const post = vi.fn();
  return { post, transport: { client: { POST: post } } };
});
vi.mock("../transport/context", () => ({ useTransport: () => transport }));
class Socket {
  static OPEN = 1;
  static current: Socket | null = null;
  readyState = 1;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn();
  constructor() {
    Socket.current = this;
  }
}
const draw = vi.fn();
beforeEach(() => {
  Socket.current = null;
  post.mockResolvedValue({
    data: { ticket: "fixture-ticket" },
  });
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("PointerEvent", MouseEvent);
  vi.stubGlobal(
    "createImageBitmap",
    vi.fn(async () => ({ width: 1280, height: 800, close: vi.fn() })),
  );
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    drawImage: draw,
  } as unknown as CanvasRenderingContext2D);
  vi.spyOn(
    HTMLCanvasElement.prototype,
    "getBoundingClientRect",
  ).mockReturnValue({ left: 10, top: 20, width: 640, height: 400 } as DOMRect);
  HTMLCanvasElement.prototype.setPointerCapture = vi.fn();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  vi.useRealTimers();
});
async function connect(controlToken = "", scale: PageScale = "fit") {
  render(
    <PageStream
      threadId="thread-a"
      session={{
        session_id: "binding-a",
        status: "ready",
        control: controlToken ? "human" : "agent",
        url: "https://example.com",
      }}
      controlToken={controlToken}
      busy={false}
      scale={scale}
    />,
  );
  await waitFor(() => expect(Socket.current?.onmessage).toBeTypeOf("function"));
  return Socket.current!;
}
async function show(controlToken = "", scale: PageScale = "fit") {
  const socket = await connect(controlToken, scale);
  await act(async () => {
    socket.onmessage?.({ data: new Blob(["jpeg"]) });
  });
  await waitFor(() => expect(draw).toHaveBeenCalled());
  return socket;
}
it("shows a temporary hint only when the read-only page is clicked and never sends input", async () => {
  const socket = await show();
  vi.useFakeTimers();
  const canvas = screen.getByLabelText("Browser page stream");
  fireEvent.pointerEnter(canvas);
  fireEvent.pointerMove(canvas, { clientX: 330, clientY: 220 });
  fireEvent.focus(canvas);
  expect(screen.queryByText("Take control to interact")).toBeNull();
  fireEvent.pointerDown(canvas, { clientX: 330, clientY: 220, button: 0 });
  expect(screen.getByText("Take control to interact")).toBeTruthy();
  act(() => vi.advanceTimersByTime(2000));
  fireEvent.pointerDown(canvas, { button: 0 });
  act(() => vi.advanceTimersByTime(2000));
  expect(screen.getByText("Take control to interact")).toBeTruthy();
  act(() => vi.advanceTimersByTime(1000));
  expect(screen.queryByText("Take control to interact")).toBeNull();
  expect(socket.send).not.toHaveBeenCalled();
});

it("maps displayed pointer coordinates to captured page coordinates only with control", async () => {
  const socket = await show("controller-a");
  fireEvent.pointerDown(screen.getByLabelText("Browser page stream"), {
    clientX: 330,
    clientY: 220,
    button: 0,
    buttons: 1,
  });
  const input = socket.send.mock.calls
    .map(([value]) => JSON.parse(value))
    .find((value) => value.input.type === "mousePressed");
  expect(input).toMatchObject({
    control_token: "controller-a",
    input: { x: 640, y: 400, button: "left", buttons: 1 },
  });
  expect(post).toHaveBeenCalledWith(
    expect.stringContaining("/access"),
    expect.objectContaining({
      body: { session_id: "binding-a" },
    }),
  );
});

it("drops intermediate frames while a decode is in progress", async () => {
  let finish!: (bitmap: ImageBitmap) => void;
  vi.mocked(createImageBitmap).mockImplementationOnce(
    () =>
      new Promise<ImageBitmap>((resolve) => {
        finish = resolve;
      }),
  );
  const socket = await connect();
  const first = new Blob(["first"]);
  const latest = new Blob(["latest"]);
  act(() => {
    socket.onmessage?.({ data: first });
    for (let index = 0; index < 100; index++)
      socket.onmessage?.({ data: new Blob([String(index)]) });
    socket.onmessage?.({ data: latest });
  });
  expect(createImageBitmap).toHaveBeenCalledTimes(1);
  const bitmap = {
    width: 1280,
    height: 800,
    close: vi.fn(),
  } as unknown as ImageBitmap;
  await act(async () => {
    finish(bitmap);
  });
  await waitFor(() => expect(draw).toHaveBeenCalledTimes(2));
  expect(
    vi.mocked(createImageBitmap).mock.calls.map(([frame]) => frame),
  ).toEqual([first, latest]);
  expect(bitmap.close).toHaveBeenCalledOnce();
  expect(socket.send).not.toHaveBeenCalled();
});

it("releases an in-progress bitmap and discards pending frames on disconnect", async () => {
  let finish!: (bitmap: ImageBitmap) => void;
  vi.mocked(createImageBitmap).mockImplementationOnce(
    () =>
      new Promise<ImageBitmap>((resolve) => {
        finish = resolve;
      }),
  );
  const socket = await connect();
  act(() => {
    socket.onmessage?.({ data: new Blob(["decoding"]) });
    socket.onmessage?.({ data: new Blob(["pending"]) });
  });
  cleanup();
  const bitmap = {
    width: 1280,
    height: 800,
    close: vi.fn(),
  } as unknown as ImageBitmap;
  await act(async () => {
    finish(bitmap);
  });
  expect(bitmap.close).toHaveBeenCalledOnce();
  expect(createImageBitmap).toHaveBeenCalledTimes(1);
  expect(draw).not.toHaveBeenCalled();
  expect(socket.close).toHaveBeenCalledOnce();
});

it.each(["50", "200"] as const)(
  "keeps page coordinates correct at %s%% display scale",
  async (scale) => {
    vi.mocked(
      HTMLCanvasElement.prototype.getBoundingClientRect,
    ).mockImplementation(function (this: HTMLCanvasElement) {
      return {
        left: 10,
        top: 20,
        width: parseFloat(this.style.width),
        height: parseFloat(this.style.height),
      } as DOMRect;
    });
    const socket = await show("controller-a", scale);
    const canvas = screen.getByLabelText("Browser page stream");
    const rect = canvas.getBoundingClientRect();
    expect(rect.width).toBe((1280 * Number(scale)) / 100);
    fireEvent.pointerDown(canvas, {
      clientX: rect.left + rect.width / 2,
      clientY: rect.top + rect.height / 2,
      button: 0,
      buttons: 1,
    });
    const click = socket.send.mock.calls
      .map(([value]) => JSON.parse(value))
      .find((value) => value.input.type === "mousePressed");
    expect(click.input).toMatchObject({ x: 640, y: 400 });
  },
);

it("keeps streaming across takeover and handback while using only current input authority", async () => {
  const props = {
    threadId: "thread-a",
    session: {
      session_id: "binding-a",
      status: "ready" as const,
      control: "agent" as const,
    },
    controlToken: "",
    busy: false,
  };
  const view = render(<PageStream {...props} />);
  await waitFor(() => expect(Socket.current?.onmessage).toBeTypeOf("function"));
  const socket = Socket.current!;
  view.rerender(
    <PageStream
      {...props}
      session={{ ...props.session, control: "human" }}
      controlToken="controller-a"
    />,
  );
  fireEvent.pointerDown(screen.getByLabelText("Browser page stream"), {
    button: 0,
  });
  expect(JSON.parse(socket.send.mock.calls[0][0]).control_token).toBe(
    "controller-a",
  );
  view.rerender(<PageStream {...props} />);
  fireEvent.pointerDown(screen.getByLabelText("Browser page stream"), {
    button: 0,
  });
  expect(socket.send).toHaveBeenCalledOnce();
  await act(async () => {
    socket.onmessage?.({ data: new Blob(["jpeg"]) });
  });
  await waitFor(() => expect(draw).toHaveBeenCalled());
  expect(post).toHaveBeenCalledOnce();
  expect(Socket.current).toBe(socket);
  expect(socket.close).not.toHaveBeenCalled();
});

it("keeps the pressed mouse button on drag moves and releases it at the end", async () => {
  const socket = await show("controller-a");
  const canvas = screen.getByLabelText("Browser page stream");
  fireEvent.pointerDown(canvas, {
    button: 0,
    buttons: 1,
    clientX: 40,
    clientY: 50,
  });
  fireEvent.pointerMove(canvas, { buttons: 1, clientX: 240, clientY: 150 });
  fireEvent.pointerUp(canvas, {
    button: 0,
    buttons: 0,
    clientX: 240,
    clientY: 150,
  });
  expect(
    socket.send.mock.calls.map(([value]) => JSON.parse(value).input),
  ).toMatchObject([
    { type: "mousePressed", button: "left", buttons: 1 },
    { type: "mouseMoved", button: "left", buttons: 1, x: 460, y: 260 },
    { type: "mouseReleased", button: "left", buttons: 0 },
  ]);
});

it("copies text only for current control and accepts explicit paste events only while controlling", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  const props = {
    threadId: "thread-a",
    session: {
      session_id: "binding-a",
      status: "ready" as const,
      control: "human" as const,
    },
    controlToken: "controller-a",
    busy: false,
  };
  const view = render(<PageStream {...props} />);
  await waitFor(() => expect(Socket.current?.onmessage).toBeTypeOf("function"));
  const socket = Socket.current!;
  act(() =>
    socket.onmessage?.({
      data: JSON.stringify({
        type: "clipboard",
        text: "sample selection",
        control_token: "controller-a",
      }),
    }),
  );
  await waitFor(() =>
    expect(writeText).toHaveBeenCalledWith("sample selection"),
  );
  act(() =>
    socket.onmessage?.({
      data: JSON.stringify({
        type: "clipboard",
        text: "stale",
        control_token: "old-controller",
      }),
    }),
  );
  expect(writeText).toHaveBeenCalledOnce();
  writeText.mockRejectedValueOnce(new Error("permission denied"));
  act(() =>
    socket.onmessage?.({
      data: JSON.stringify({
        type: "clipboard",
        text: "sample selection",
        control_token: "controller-a",
      }),
    }),
  );
  expect(
    await screen.findByText(
      "Allow clipboard access to copy text to this device.",
    ),
  ).toBeTruthy();
  expect(screen.queryByText("Disconnected")).toBeNull();
  const canvas = screen.getByLabelText("Browser page stream");
  // The shortcut must remain unprevented so the browser delivers its paste event.
  expect(
    fireEvent.keyDown(canvas, {
      key: "v",
      code: "KeyV",
      ctrlKey: true,
      keyCode: 86,
    }),
  ).toBe(true);
  expect(socket.send).not.toHaveBeenCalled();
  fireEvent.paste(canvas, { clipboardData: { getData: () => "local sample" } });
  expect(JSON.parse(socket.send.mock.calls[0][0]).input).toEqual({
    type: "text",
    text: "local sample",
  });
  view.rerender(
    <PageStream
      {...props}
      controlToken=""
      session={{ ...props.session, control: "agent" }}
    />,
  );
  act(() =>
    socket.onmessage?.({
      data: JSON.stringify({
        type: "clipboard",
        text: "after handback",
        control_token: "controller-a",
      }),
    }),
  );
  fireEvent.paste(canvas, {
    clipboardData: { getData: () => "blocked sample" },
  });
  expect(socket.send).toHaveBeenCalledOnce();
  expect(writeText).toHaveBeenCalledTimes(2);
  expect(socket.close).not.toHaveBeenCalled();
});
