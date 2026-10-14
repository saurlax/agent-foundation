import { record } from "rrweb";

declare global {
  interface Window {
    __a13nBrowserEvent: (value: unknown) => Promise<void>;
    __a13nBrowserRecorder: {
      restart: () => void;
      node: (id: number, epoch: string) => Element | null;
    };
  }
}

let epoch = "";
let stop: (() => void) | undefined;
function restart() {
  stop?.();
  epoch = Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  stop = record({
    emit: (event) => {
      void window.__a13nBrowserEvent({ epoch, event });
    },
    inlineStylesheet: true,
    inlineImages: true,
    maskInputOptions: { password: true },
    blockSelector: "iframe, canvas, video, audio, input[type=file]",
    sampling: {
      mousemove: false,
      mouseInteraction: false,
      scroll: 100,
      input: "last",
    },
    recordCanvas: false,
    recordCrossOriginIframes: false,
  });
}
window.__a13nBrowserRecorder = {
  restart,
  node: (id, expectedEpoch) => {
    if (expectedEpoch !== epoch) return null;
    const node = record.mirror.getNode(id);
    if (!(node instanceof Element) || !node.isConnected) return null;
    return node;
  },
};
if (document.readyState === "loading")
  document.addEventListener("DOMContentLoaded", restart, { once: true });
else restart();
