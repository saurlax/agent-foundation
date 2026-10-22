// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserFields } from "./browser-fields";
const post = vi.hoisted(() => vi.fn());
vi.mock("../transport/context", () => ({
  useTransport: () => ({ client: { POST: post } }),
}));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});
function show(active = true) {
  const changed = vi.fn();
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <BrowserFields
        source={'schema_version: "1"\nbrowser: {executable: /saved/chrome}\n'}
        onChange={changed}
        active={active}
      />
    </QueryClientProvider>,
  );
  return changed;
}
it("does not scan while collapsed", () => {
  show(false);
  expect(post).not.toHaveBeenCalled();
});
it("lists installed Chromium without VNC selection or changing saved bindings", async () => {
  post.mockResolvedValue({
    data: {
      browsers: [{ name: "Chrome", path: "/opt/chrome", source: "system" }],
    },
  });
  const changed = show();
  await screen.findByText("Browsers: 1");
  expect(screen.queryByRole("combobox", { name: "VNC executable" })).toBeNull();
  expect(
    (
      screen.getByRole("textbox", {
        name: "Browser executable path",
      }) as HTMLInputElement
    ).value,
  ).toBe("/saved/chrome");
  expect(changed).not.toHaveBeenCalled();
});
