// Playwright driver for the web dashboard.
//
// The dashboard's "Coding Mode" is where the Python agent integration lives;
// that's the screen most PRs to web/ will care about. This driver navigates
// there, optionally waits for the agent's tool registry to load (via the
// proxy at /api/tools), and writes a screenshot to /tmp/shots/.
//
// Usage:
//   node screenshot.mjs [overview|coding]   # default: coding
//
// Assumes:
//   - web dev server on http://127.0.0.1:3000  (npm run dev)
//   - optionally agent server on :8765 for the "Tool Schema Registry (35)"
//     count to populate; without it, Coding Mode still renders but shows
//     "0 real tools" and proxy 502s.
import { chromium } from "/opt/node22/lib/node_modules/playwright/index.mjs";
import { mkdirSync } from "node:fs";

const target = process.argv[2] || "coding";
const SHOTS = "/tmp/shots";
mkdirSync(SHOTS, { recursive: true });

const browser = await chromium.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
const errs = [];
page.on("pageerror", (e) => errs.push(String(e)));
page.on("console", (m) => {
  if (m.type() === "error") errs.push("[console.error] " + m.text());
});

await page.goto("http://127.0.0.1:3000/", { waitUntil: "networkidle", timeout: 30000 });

if (target === "coding") {
  await page.locator("text=Coding Mode").click();
  // The "(N)" count appears in the registry tab once /api/tools resolves.
  // Allow a beat for the proxy round-trip; tolerate the agent being down.
  await page.waitForTimeout(1500);
}

const file = `${SHOTS}/${target}.png`;
await page.screenshot({ path: file });
const text = (await page.locator("body").innerText()).slice(0, 600);
console.log(JSON.stringify({ screenshot: file, text, errs }, null, 2));
await browser.close();
