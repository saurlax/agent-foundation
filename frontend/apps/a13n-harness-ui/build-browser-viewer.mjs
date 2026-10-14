import { build } from "esbuild";
import { mkdir, readFile, writeFile } from "node:fs/promises";

const result = await build({
  entryPoints: ["src/browser/viewer.ts"],
  bundle: true,
  format: "iife",
  target: "es2022",
  minify: true,
  write: false,
});
const template = await readFile("src/browser/viewer.html", "utf8");
const script = result.outputFiles[0].text.replaceAll("</script", "<\\/script");
await mkdir("public", { recursive: true });
await writeFile(
  "public/browser-viewer.html",
  template.replace(
    "<!-- VIEWER_SCRIPT -->",
    () => `<script>${script}</script>`,
  ),
);

const recorder = await build({
  entryPoints: ["src/browser/recorder.ts"],
  bundle: true,
  format: "iife",
  target: "es2022",
  minify: true,
  write: false,
});
await writeFile("public/browser-recorder.js", recorder.outputFiles[0].text);
