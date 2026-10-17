// Browser tests for the dashboard: start the built server against a fake core (fixture_core.py) and drive it
// with Chrome. Needs `npm run build` first and a Chrome (CHROME_PATH, or a common install location).
// Never touches your real accounts. Run: npm run test:ui
import { spawn } from "node:child_process";
import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const axeSource = fs.readFileSync(createRequire(import.meta.url).resolve("axe-core/axe.min.js"), "utf8");

const WEB = fileURLToPath(new URL("../..", import.meta.url));
const PORT = process.env.ZW_UI_PORT ?? "4949";
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = [
  process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
].find((p) => p && fs.existsSync(p));
if (!CHROME) {
  console.log("no Chrome found (set CHROME_PATH): UI tests skipped");
  process.exit(process.env.CI ? 1 : 0);
}

const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), "zorua-ui-"));
const FX = path.join(ROOT, "fx");
for (const d of [FX, path.join(ROOT, "bind1"), path.join(ROOT, "bind2")]) fs.mkdirSync(d);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const logFd = fs.openSync(path.join(FX, "server.log"), "w");
const server = spawn("node", ["node_modules/next/dist/bin/next", "start", ".", "-H", "127.0.0.1", "-p", PORT], {
  cwd: WEB,
  env: { ...process.env, ZORUA_CORE: path.join(WEB, "tests/ui/fixture_core.py"), FX_DIR: FX, FX_BIND1: path.join(ROOT, "bind1"), FX_BIND2: path.join(ROOT, "bind2") },
  stdio: ["ignore", logFd, logFd],
});

/** Poll until `fn` returns something truthy, or give up after `ms`. */
async function until(fn, ms = 8000, step = 100) {
  const end = Date.now() + ms;
  for (;;) {
    const v = await Promise.resolve(fn()).catch(() => false);
    if (v) return v;
    if (Date.now() > end) return false;
    await sleep(step);
  }
}

for (let i = 0; i < 80 && !(await fetch(BASE).then((r) => r.ok).catch(() => false)); i++) await sleep(250);

const results = [];
const ok = (name, pass, note = "") => results.push({ name, pass, note });
const W = { "content-type": "application/json", "x-zorua-web": "1", origin: BASE };
const post = (p, body) => fetch(BASE + p, { method: "POST", headers: W, body: JSON.stringify(body) }).then(async (r) => ({ status: r.status, body: await r.json().catch(() => null) }));
const browser = await puppeteer.launch({ executablePath: CHROME, headless: "new", args: ["--no-sandbox"] });
const open = async () => {
  const p = await browser.newPage();
  await p.setViewport({ width: 1280, height: 900 });
  return p;
};
const text = (p) => p.evaluate(() => document.body.textContent);
const has = (p, re) => async () => re.test(await text(p));
const baseState = await fetch(BASE + "/api/state").then((r) => r.json());
const clone = (v) => JSON.parse(JSON.stringify(v));
const setField = (p, i, v) =>
  p.evaluate(
    (i, v) => {
      const t = document.querySelectorAll("textarea")[i];
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(t, v);
      t.dispatchEvent(new Event("input", { bubbles: true }));
      t.focus();
    },
    i,
    v,
  );
const navButton = (p, label) => p.evaluateHandle((l) => [...document.querySelectorAll("nav button")].find((x) => x.textContent.trim() === l), label);
const signIn = (p, email) =>
  p.evaluate((email) => {
    const li = [...document.querySelectorAll("li")].find((l) => l.textContent.includes(email));
    [...li.querySelectorAll("button")].find((x) => x.textContent.trim() === "sign in").click();
  }, email);
const palette = async (p, q) => {
  await p.keyboard.press("/");
  await sleep(250);
  await p.keyboard.type(q);
  await sleep(150);
  await p.keyboard.press("Enter");
};

// The committed landing page and demo (docs/), served under /zorua/ the way GitHub Pages serves them.
const DOCS = path.resolve(WEB, "../docs");
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".webp": "image/webp", ".png": "image/png", ".txt": "text/plain", ".json": "application/json" };
const docsServer = http.createServer((req, res) => {
  let rel = decodeURIComponent(new URL(req.url, "http://x").pathname);
  if (!rel.startsWith("/zorua/")) rel = "/__none__";
  let file = path.join(DOCS, rel.slice("/zorua/".length));
  if (!file.startsWith(DOCS)) file = "";
  if (file && fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, "index.html");
  if (!file || !fs.existsSync(file)) {
    res.statusCode = 404;
    return res.end("not found");
  }
  res.setHeader("content-type", MIME[path.extname(file)] ?? "application/octet-stream");
  res.end(fs.readFileSync(file));
});
await new Promise((r) => docsServer.listen(0, "127.0.0.1", r));
const DOCS_BASE = `http://127.0.0.1:${docsServer.address().port}/zorua/`;

/** Run axe-core on the current page; returns a short description of every violation. */
async function axeViolations(p) {
  await p.evaluate(axeSource);
  return p.evaluate(async () => {
    const r = await axe.run(document, { resultTypes: ["violations"] });
    return r.violations.map((v) => `${v.id} x${v.nodes.length} (${v.nodes[0].target.join(" ").slice(0, 80)})`);
  });
}

try {
  // The toast: closing one with the pointer over it must not stop later ones from timing out.
  {
    const p = await open();
    await p.goto(BASE, { waitUntil: "networkidle0" });
    await palette(p, "zorua use work");
    const dismiss = await until(() => p.$('button[aria-label="Dismiss"]'));
    await dismiss.hover();
    await dismiss.click();
    await p.mouse.move(5, 5);
    await sleep(300);
    await palette(p, "zorua use work");
    await until(has(p, /Copied:/), 3000);
    ok("a toast times out after an earlier one was closed with the pointer over it", await until(async () => !/Copied:/.test(await text(p)), 12000));
    await p.close();
  }

  // Leaving a provider page right after editing a field: the save is already on its way.
  {
    const p = await open();
    let prompt = null;
    p.on("dialog", async (d) => {
      prompt = d.message();
      await d.dismiss();
    });
    await p.goto(BASE + "/#/provider/kimi", { waitUntil: "networkidle0" });
    await until(() => p.$("textarea"));
    ok("provider keys have no reveal/export controls", await p.evaluate(() => ![...document.querySelectorAll("button")].some((b) => /show keys|copy key/i.test(b.textContent))));
    await setField(p, 0, "https://api.example.com/x");
    await sleep(100);
    await (await navButton(p, "Overview")).asElement().click();
    await sleep(1200);
    ok("leaving right after editing a provider field does not ask to discard", prompt === null && (await p.evaluate(() => location.hash)) === "#/", prompt ?? "");
    await p.close();
  }
  {
    const p = await open();
    let prompt = null;
    p.on("dialog", async (d) => {
      prompt = d.message();
      await d.dismiss();
    });
    await p.goto(BASE + "/#/provider/kimi", { waitUntil: "networkidle0" });
    await until(() => p.$("textarea"));
    await setField(p, 0, "https://api.example.com/y");
    await p.evaluate(() => document.activeElement.blur());
    await setField(p, 1, "sk-newer-key-0000");
    await (await navButton(p, "Overview")).asElement().click();
    await sleep(300);
    ok("an edit newer than the save in flight still asks before leaving", prompt !== null);
    await p.close();
  }

  // Signing in twice: the banner follows the second run.
  {
    const p = await open();
    await p.goto(BASE + "/#/", { waitUntil: "networkidle0" });
    await until(has(p, /demo@example\.com/));
    await signIn(p, "demo@example.com");
    const first = await until(has(p, /sign-in default: done/), 12000);
    await signIn(p, "demo@example.com");
    const second = await until(has(p, /waiting for the browser/), 3000);
    ok("a second sign-in of the same account shows the new run", first && second && !/sign-in default: done/.test(await text(p)));
    await p.close();
  }
  // A failed poll must not end the wait for a sign-in.
  {
    const p = await open();
    await p.goto(BASE + "/#/", { waitUntil: "networkidle0" });
    await until(has(p, /work@example\.com/));
    await p.setRequestInterception(true);
    let polls = 0;
    p.on("request", (r) => (r.url().includes("/api/login") && ++polls <= 2 ? r.abort() : r.continue()));
    await signIn(p, "work@example.com");
    ok("the sign-in banner survives two failed polls and reports the result", await until(has(p, /sign-in work: done/), 20000, 250));
    await p.close();
  }

  // Responses can arrive out of order.
  {
    const p = await open();
    await p.goto(BASE, { waitUntil: "networkidle0" });
    await p.setRequestInterception(true);
    let n = 0;
    p.on("request", async (r) => {
      if (!r.url().includes("/api/state")) return r.continue();
      const body = clone(baseState);
      if (++n === 1) {
        body.data.accounts = body.data.accounts.filter((a) => a.name === "default");
        await sleep(2500);
      } else {
        body.data.accounts.push({ ...body.data.accounts[0], name: "fresh", email: "fresh@example.com" });
      }
      r.respond({ status: 200, contentType: "application/json", body: JSON.stringify(body) }).catch(() => {});
    });
    await p.keyboard.press("r");
    await sleep(150);
    await p.keyboard.press("r");
    await until(has(p, /fresh@example\.com/), 5000);
    await sleep(3000);
    ok("a slow older response does not overwrite a newer one", /fresh@example\.com/.test(await text(p)));
    await p.close();
  }
  // A removed provider's last check must not come back with a new provider of the same name.
  {
    const p = await open();
    await p.setRequestInterception(true);
    let n = 0;
    const failing = { status: "fail", http: 401, ms: 90, via: "GET /models", detail: "key rejected (HTTP 401)", checked_at: Math.floor(Date.now() / 1000) };
    p.on("request", (r) => {
      if (!r.url().includes("/api/state")) return r.continue();
      const i = ++n;
      const body = clone(baseState);
      body.checks = i === 1 ? { glm: failing } : {};
      if (i === 2) body.data.providers = body.data.providers.filter((x) => x.name !== "glm");
      r.respond({ status: 200, contentType: "application/json", body: JSON.stringify(body) }).catch(() => {});
    });
    await p.goto(BASE, { waitUntil: "networkidle0" });
    const shown = await until(has(p, /key rejected/));
    await sleep(5500); // the forced refresh is limited to one per 5 s on the server, not here; this only spaces the keys
    await p.keyboard.press("r");
    await sleep(500);
    await p.keyboard.press("r");
    await sleep(800);
    ok("a removed provider's failed check is forgotten when a provider of that name returns", shown && !/key rejected/.test(await text(p)), shown ? "" : "setup: the failure was never shown");
    await p.close();
  }

  // Accessibility: axe-core over the main states, in both themes and at phone width.
  for (const [name, scheme, size, hash, setup] of [
    ["overview, dark", "dark", [1280, 900], "#/", null],
    ["overview, light", "light", [1280, 900], "#/", null],
    ["command palette", "dark", [1280, 900], "#/", (p) => p.keyboard.press("/")],
    ["add-account dialog", "dark", [1280, 900], "#/", (p) => p.keyboard.press("a")],
    ["shortcuts dialog", "dark", [1280, 900], "#/", (p) => p.keyboard.press("?")],
    ["account page, light", "light", [1280, 900], "#/account/work", null],
    ["provider page", "dark", [1280, 900], "#/provider/kimi", null],
    ["phone, overview", "dark", [390, 844], "#/", null],
    ["phone, menu open", "dark", [390, 844], "#/", (p) => p.click('button[aria-label="Open menu"]')],
  ]) {
    const p = await open();
    await p.setViewport({ width: size[0], height: size[1] });
    await p.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
    await p.goto(BASE + "/" + hash, { waitUntil: "networkidle0" });
    await until(has(p, /Overview|Account|Provider/));
    await sleep(500);
    if (setup) {
      await setup(p);
      await sleep(500);
    }
    const found = await axeViolations(p);
    ok(`no accessibility violations: ${name}`, found.length === 0, found.join("; "));
    await p.close();
  }

  // Text size: the root is 14px, so rem-based steps are smaller than they look (text-xs was 10.5px).
  for (const [name, size] of [["desktop", [1280, 900]], ["phone", [390, 844]]]) {
    const p = await open();
    await p.setViewport({ width: size[0], height: size[1] });
    await p.goto(BASE + "/#/", { waitUntil: "networkidle0" });
    await until(has(p, /Overview/));
    await sleep(500);
    const sizes = await p.evaluate(() => {
      const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let total = 0, legible = 0, smallest = 99, smallestText = "";
      for (let n; (n = w.nextNode()); ) {
        const t = n.textContent.trim();
        const el = n.parentElement;
        if (!t || !el || el.closest("script,style")) continue;
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height || getComputedStyle(el).visibility === "hidden") continue;
        const px = parseFloat(getComputedStyle(el).fontSize);
        total += t.length;
        if (px >= 12) legible += t.length;
        if (px < smallest) [smallest, smallestText] = [px, t.slice(0, 24)];
      }
      return { share: legible / total, smallest, smallestText };
    });
    ok(`text is legible on ${name}: nothing under 12px`, sizes.smallest >= 12 && sizes.share === 1, `smallest ${sizes.smallest}px ("${sizes.smallestText}"), ${(sizes.share * 100).toFixed(1)}% at 12px or more`);
    await p.close();
  }

  // The menu drawer on a phone behaves like a dialog.
  {
    const p = await open();
    await p.setViewport({ width: 390, height: 844 });
    await p.goto(BASE + "/#/", { waitUntil: "networkidle0" });
    await until(has(p, /Overview/));
    const where = () => p.evaluate(() => `${document.activeElement?.getAttribute("aria-label") ?? document.activeElement?.tagName}`);
    await p.click('button[aria-label="Open menu"]');
    await sleep(400);
    ok("opening the menu moves focus into it", (await where()) === "Close menu", await where());
    ok("the page behind the open menu is inert", await p.evaluate(() => document.getElementById("main").inert === true));
    for (let i = 0; i < 25; i++) await p.keyboard.press("Tab");
    ok("Tab stays inside the open menu", await p.evaluate(() => !!document.activeElement?.closest('nav[aria-label="Zorua"]')), await where());
    await p.keyboard.press("Escape");
    await sleep(300);
    ok("Esc closes the menu and returns focus to its button", (await where()) === "Open menu" && (await p.evaluate(() => document.getElementById("main").inert === false)), await where());
    await p.close();
  }

  // The landing page and the live demo it embeds.
  {
    const html = fs.readFileSync(path.join(DOCS, "index.html"), "utf8");
    const png = fs.readFileSync(path.join(DOCS, "assets/og.png"));
    ok("the landing page has a canonical address and share tags", /rel="canonical"/.test(html) && /property="og:image" content="https:\/\/[^"]+\/assets\/og\.png"/.test(html) && /name="twitter:card"/.test(html));
    ok("the share image is a 1200x630 PNG", png.subarray(1, 4).toString() === "PNG" && png.readUInt32BE(16) === 1200 && png.readUInt32BE(20) === 630);
    for (const [name, size, url] of [
      ["landing page, desktop", [1280, 900], DOCS_BASE],
      ["landing page, phone", [390, 844], DOCS_BASE],
      ["demo, desktop", [1280, 900], DOCS_BASE + "demo/"],
      ["demo, phone", [390, 844], DOCS_BASE + "demo/"],
    ]) {
      const p = await open();
      await p.setViewport({ width: size[0], height: size[1] });
      await p.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "dark" }]);
      await p.goto(url, { waitUntil: "networkidle0" });
      await sleep(800);
      if (url.endsWith("/demo/")) await until(has(p, /Accounts/i));
      if (url === DOCS_BASE) await p.evaluate(() => document.getElementById("stage").scrollIntoView({ block: "center" })); // loads the embed too
      await sleep(url === DOCS_BASE ? 2500 : 300);
      const found = await axeViolations(p);
      ok(`no accessibility violations: ${name}`, found.length === 0, found.join("; "));
      await p.close();
    }
    const p = await open();
    let demoRequests = 0;
    p.on("request", (r) => r.url().includes("/zorua/demo/") && demoRequests++);
    await p.goto(DOCS_BASE, { waitUntil: "networkidle0" });
    await sleep(500);
    ok("the demo is not requested until it is near", demoRequests === 0 && !(await p.evaluate(() => document.getElementById("demo").getAttribute("src"))));
    await p.evaluate(() => document.getElementById("stage").scrollIntoView({ block: "center" }));
    const frame = await until(() => p.frames().find((f) => f.url().includes("/zorua/demo/")), 6000);
    ok("the embedded demo loads and shows its accounts", !!frame && (await until(() => frame.evaluate(() => /Accounts/i.test(document.body.textContent) && !/reading accounts/.test(document.body.textContent)), 8000)));
    if (frame) {
      const box = await (await p.$("#demo")).boundingBox();
      await p.mouse.click(box.x + box.width * 0.6, box.y + 40);
      await p.keyboard.press("/");
      ok("the palette opens inside the embedded demo", await until(() => frame.evaluate(() => !!document.querySelector("dialog[open][data-palette]")), 3000));
    }
    await p.close();
  }

  // Another site cannot frame the dashboard.
  // A page on another origin (a second local server) embeds the dashboard and, as a control, one of its own pages.
  {
    const other = http.createServer((req, res) => {
      res.setHeader("content-type", "text/html");
      res.end(req.url === "/ok" ? "<p>ok</p>" : `<iframe id="a" src="${BASE}/"></iframe><iframe id="b" src="/ok"></iframe>`);
    });
    await new Promise((r) => other.listen(0, "127.0.0.1", r));
    const origin = `http://localhost:${other.address().port}`; // another hostname, so another origin
    const p = await open();
    await p.goto(origin + "/", { waitUntil: "networkidle0" });
    await sleep(1200);
    const urls = p.frames().filter((f) => f !== p.mainFrame()).map((f) => f.url());
    const dashboard = urls.find((u) => u.startsWith("chrome-error:") || u.startsWith(BASE));
    ok("another site cannot frame the dashboard", urls.some((u) => u === origin + "/ok") && dashboard?.startsWith("chrome-error:"), urls.join(" | "));
    await p.close();
    other.close();
  }

  // Server side.
  {
    const calls = () => (fs.readFileSync(path.join(FX, "calls.log"), "utf8").match(/^ls /gm) ?? []).length;
    const before = calls();
    const r = await post("/api/action", { action: "account.login.cancel", name: "default" });
    ok("an action that looks nothing up does not read the registry", r.status === 200 && calls() === before, `registry reads: +${calls() - before}`);
  }
  {
    fs.writeFileSync(path.join(FX, "slow"), "");
    const slow = fetch(BASE + "/api/state?refresh=1").then((r) => r.json());
    await sleep(300);
    const rm = await post("/api/action", { action: "binding.remove", dir: path.join(ROOT, "bind2") });
    const fresh = await fetch(BASE + "/api/state?refresh=1").then((r) => r.json());
    await slow;
    fs.rmSync(path.join(FX, "slow"));
    ok("a read that started before a change is not served after it", rm.status === 200 && fresh.data.bindings.length === 1, `bindings after unbind: ${fresh.data.bindings.length} (want 1)`);
  }
  {
    fs.writeFileSync(path.join(FX, "die-early"), "");
    const r = await post("/api/action", { action: "provider.save", name: "kimi", doc: { agent: "claude", env: { PAD: "x".repeat(190000) }, models: {} } }).catch((e) => ({ status: `ERR ${e.cause?.code}` }));
    await sleep(800);
    const alive = await fetch(BASE).then((x) => x.ok).catch(() => false);
    const epipe = /EPIPE/.test(fs.readFileSync(path.join(FX, "server.log"), "utf8"));
    ok("a core that exits before reading its input leaves no uncaught EPIPE", alive && r.status === 422 && !epipe, `status ${r.status}, server ${alive ? "alive" : "down"}, EPIPE logged: ${epipe}`);
  }
} finally {
  await browser.close().catch(() => {});
  server.kill();
  docsServer.close();
  fs.rmSync(ROOT, { recursive: true, force: true });
}

for (const r of results) console.log(`${r.pass ? "PASS" : "FAIL"}  ${r.name}${r.note ? `  (${r.note})` : ""}`);
const failed = results.filter((r) => !r.pass).length;
console.log(failed ? `\n${failed} of ${results.length} FAILED` : "\nALL OK");
process.exit(failed ? 1 : 0);
