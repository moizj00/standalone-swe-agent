// Playwright driver for the web dashboard.
//
// The dashboard's "Coding Mode" is where the Python agent integration lives;
// that's the screen most PRs to web/ will care about. This driver navigates
// there, asserts the "Tool Schema Registry (N)" header appears (the cheap
// React → proxy → Python integration check), and writes a screenshot to
// /tmp/shots/.
//
// Usage:
//   node screenshot.mjs [overview|coding]   # default: coding
//
// Playwright resolution order:
//   1. PLAYWRIGHT_MODULE_PATH env var (explicit override)
//   2. standard `playwright` (works if installed in node_modules / global)
//   3. /opt/node22/lib/node_modules/playwright (this container's preinstalled copy)
//
// Assumes:
//   - web dev server on http://127.0.0.1:3000  (npm run dev)
//   - agent server on :8765 for the registry count to populate. Without it,
//     the "(N)" never appears and this driver exits non-zero.
import { mkdirSync } from "node:fs";

async function importPlaywright() {
  const candidates = [
    process.env.PLAYWRIGHT_MODULE_PATH,
    "playwright",
    "/opt/node22/lib/node_modules/playwright/index.mjs",
  ].filter(Boolean);
  const errors = [];
  for (const spec of candidates) {
    try {
      return await import(spec);
    } catch (e) {
      errors.push(`  ${spec}: ${e.message}`);
    }
  }
  throw new Error(
    "Could not import Playwright from any of:\n" +
      errors.join("\n") +
      "\nSet PLAYWRIGHT_MODULE_PATH=/path/to/playwright/index.mjs " +
      "or `npm install playwright` somewhere on the NODE_PATH.",
  );
}

const target = process.argv[2] || "coding";
const SHOTS = "/tmp/shots";
mkdirSync(SHOTS, { recursive: true });

const { chromium } = await importPlaywright();
const browser = await chromium.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
const errs = [];
page.on("pageerror", (e) => errs.push(String(e)));
page.on("console", (m) => {
  if (m.type() === "error") errs.push("[console.error] " + m.text());
});

await page.goto("http://127.0.0.1:3000/", { waitUntil: "networkidle", timeout: 30000 });

let assertion = "(none)";
if (target === "coding") {
  await page.locator("text=Coding Mode").click();
  // The "(N)" count appears in the registry tab once the agent's /api/tools
  // round-trip resolves through the Node proxy. Without it, Coding Mode renders
  // but shows "0 real tools" — i.e. a silent integration failure. Wait for the
  // count to appear; fail loudly if it doesn't.
  try {
    await page.getByText(/Tool Schema Registry \(\d+\)/).waitFor({ timeout: 8000 });
    assertion = await page
      .getByText(/Tool Schema Registry \(\d+\)/)
      .first()
      .innerText();
  } catch {
    await page.screenshot({ path: `${SHOTS}/${target}-failed.png` });
    console.error(
      JSON.stringify(
        {
          error: "Tool Schema Registry count never appeared — agent /api/tools not reachable through the proxy",
          failureScreenshot: `${SHOTS}/${target}-failed.png`,
          errs,
        },
        null,
        2,
      ),
    );
    await browser.close();
    process.exit(1);
  }
}

const file = `${SHOTS}/${target}.png`;
await page.screenshot({ path: file });
const text = (await page.locator("body").innerText()).slice(0, 600);
console.log(JSON.stringify({ screenshot: file, assertion, text, errs }, null, 2));
await browser.close();
