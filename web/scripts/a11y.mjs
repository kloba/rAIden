// The WCAG 2.1 A and AA rules of axe-core (the engine of Lighthouse's accessibility audit) on every page of the console,
// in both themes, against a running demo console. `make a11y` installs what it needs (without touching package.json):
//
//   make demo                          # in one shell
//   make a11y                          # in another; BASE=http://host:port and CHROME=/path/to/chrome to change either
//
// Prints the violations of each page and exits with 1 if there are any.
import fs from "node:fs";
import { createRequire } from "node:module";
import puppeteer from "puppeteer-core";

const require = createRequire(import.meta.url);
const axe = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const base = process.env.BASE || "http://127.0.0.1:8787";
const chrome = process.env.CHROME
  || (process.platform === "darwin" ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" : "/usr/bin/google-chrome");
const pages = ["/", "/estate", "/servers/orders-prod", "/performance", "/incidents", "/incidents/:open", "/incidents/:resolved",
  "/approvals", "/chat", "/routines", "/monitors", "/knowledge", "/databases", "/databases/new", "/ai", "/autonomy", "/audit", "/settings"];
const rules = { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] } };

const { demo_login: login } = await (await fetch(`${base}/api/v1/auth/status`)).json();
if (!login) throw new Error(`${base} is not a demo console: start one with make demo`);
const browser = await puppeteer.launch({ executablePath: chrome, headless: true, defaultViewport: { width: 1440, height: 900 } });
let found = 0;

async function check(page, theme, path, ready) {
  await page.goto(`${base}${path}`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(ready, { timeout: 15000 }).catch(() => {});
  await new Promise((r) => setTimeout(r, 2500)); // charts and live data settle
  await page.evaluate(axe); // through the DevTools protocol: the console's CSP allows no injected script
  const { violations, passes } = await page.evaluate(async (opts) => await window.axe.run(document, opts), rules);
  found += violations.length;
  console.log(`${theme.padEnd(5)} ${path.padEnd(28)} ${passes.length} rules pass, ${violations.length} fail`);
  for (const v of violations) console.log(`      ${v.id} (${v.impact}, ${v.nodes.length}): ${v.help}  e.g. ${v.nodes[0]?.target?.join(" ")}`);
}

for (const theme of ["light", "dark"]) {
  const context = await browser.createBrowserContext();
  const page = await context.newPage();
  await page.evaluateOnNewDocument((t) => localStorage.setItem("raiden-theme", t), theme);
  await check(page, theme, "/login", () => document.querySelector("form"));
  await page.evaluate(async (body) => {
    await fetch("/api/v1/auth/login", { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "raiden" }, body });
  }, JSON.stringify(login));
  // an open incident (with the note box and the gates) and a resolved one (with the report)
  const first = (status) => page.evaluate(async (s) => (await (await fetch(`/api/v1/incidents?status=${s}`)).json())[0]?.id, status);
  const ids = { open: await first("open"), resolved: await first("resolved") };
  for (const path of pages.map((p) => p.replace(/:(open|resolved)$/, (_, s) => ids[s] ?? ""))) {
    await check(page, theme, path, () => document.querySelector("main") && !/Loading…/.test(document.body.innerText));
  }
  await context.close();
}
await browser.close();
console.log(found ? `${found} violations` : "no violations");
process.exit(found ? 1 : 0);
