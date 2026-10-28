// Unit tests for app/fuzzy.ts (the command palette's matching). No build needed. Run: npm run test:unit
// The source is TypeScript and CI runs Node 20, which cannot import it directly, so it is transpiled here with
// the `typescript` package the project already has.
import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const src = fs.readFileSync(fileURLToPath(new URL("../app/fuzzy.ts", import.meta.url)), "utf8");
const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } }).outputText;
const { matchWord, rank, runs } = await import(`data:text/javascript;base64,${Buffer.from(js).toString("base64")}`);

const score = (w, t) => matchWord(w, t)?.score;
const labels = (items, q) => rank(items, q, (c) => c).map((r) => r.item.label);
const cmd = (label, extra = {}) => ({ label, group: "Actions", ...extra });

test("a subsequence matches, in order, and reports where", () => {
  assert.deepEqual(matchWord("acc", "Add account").indices, [4, 5, 6]);
  assert.deepEqual(matchWord("usw", "zorua use work").indices, [6, 7, 10]);
});

test("letters out of order, missing letters and a longer query do not match", () => {
  assert.equal(matchWord("ca", "ac"), null);
  assert.equal(matchWord("xyz", "Add account"), null);
  assert.equal(matchWord("aab", "ab"), null);
  assert.equal(matchWord("", "anything"), null);
});

test("case does not matter", () => {
  assert.deepEqual(matchWord("add", "ADD ACCOUNT").indices, [0, 1, 2]);
  assert.equal(score("kimi", "Kimi"), score("kimi", "kimi"));
});

test("consecutive hits beat the same letters spread out", () => {
  assert.ok(score("acc", "account") > score("acc", "a_c_c_ount"));
  assert.ok(score("ab", "xab") > score("ab", "xaxb"));
});

test("a hit on a word start beats one in the middle of a word", () => {
  assert.ok(score("w", "zorua work") > score("w", "zorua awk"));
  assert.ok(score("uw", "use work") > score("uw", "ruewx"));
  // Same position, so only the word start differs.
  assert.ok(score("w", "ab w") > score("w", "abxw"));
  assert.ok(score("w", "ab-w") > score("w", "abxw"));
});

test("a camelCase hump counts as a word start", () => {
  assert.ok(score("fb", "fooBar") > score("fb", "fabric"));
  assert.deepEqual(matchWord("fb", "fooBar").indices, [0, 3]);
});

test("an earlier hit beats a later one", () => {
  assert.ok(score("x", "xa") > score("x", "aax"));
  // Neither is at a word start, so only the position differs.
  assert.ok(score("x", "axa") > score("x", "aaax"));
  assert.ok(score("pal", "palette") > score("pal", "open palette"));
});

test("the best placement is chosen, not the first one", () => {
  // "ab" occurs at 0..1 only as a gap match (a, then b far away) and consecutively at the end.
  assert.deepEqual(matchWord("ab", "a____ab").indices, [5, 6]);
});

test("empty query keeps the order and highlights nothing", () => {
  const items = [cmd("Zeta"), cmd("Alpha"), cmd("Mid")];
  const out = rank(items, "  ", (c) => c);
  assert.deepEqual(out.map((r) => r.item.label), ["Zeta", "Alpha", "Mid"]);
  assert.ok(out.every((r) => r.label.length === 0 && r.hint.length === 0));
});

test("results are sorted best first and ties keep their order", () => {
  const items = [cmd("Check all providers"), cmd("Add account"), cmd("Compact rows"), cmd("Account settings")];
  assert.deepEqual(labels(items, "acc"), ["Account settings", "Add account"]);
  const tied = [cmd("kimi"), cmd("kimi")].map((c, i) => ({ ...c, id: i }));
  assert.deepEqual(rank(tied, "kimi", (c) => c).map((r) => r.item.id), [0, 1]);
});

test("a label that starts with the query ranks first", () => {
  const items = [cmd("Bind a directory"), cmd("Add account"), cmd("Refresh now")];
  assert.equal(labels(items, "ad")[0], "Add account");
  assert.equal(labels([cmd("Compact rows"), cmd("Copy command")], "co")[0], "Compact rows");
});

test("every word must match, in any order", () => {
  const items = [cmd("zorua use work", { hint: "copy command", group: "Copy command" }), cmd("zorua use kimi", { hint: "copy command", group: "Copy command" })];
  assert.deepEqual(labels(items, "work use"), ["zorua use work"]);
  assert.deepEqual(labels(items, "use zzz"), []);
});

test("keywords, hint and group are searched, and weigh less than the label", () => {
  const items = [cmd("Light", { keywords: "theme" }), cmd("Theme settings"), cmd("Overview", { group: "Go to" })];
  assert.deepEqual(labels(items, "theme"), ["Theme settings", "Light"]);
  assert.deepEqual(labels(items, "go to"), ["Overview"]);
  assert.deepEqual(rank([cmd("work", { hint: "Codex · pro" })], "codex", (c) => c)[0].hint, [0, 1, 2, 3, 4]);
});

test("only label and hint hits are reported for highlighting", () => {
  const r = rank([cmd("Light", { keywords: "theme", hint: "sun" })], "th", (c) => c)[0];
  assert.deepEqual(r.label, []);
  assert.deepEqual(r.hint, []);
});

test("a fuzzy query finds a command a substring search would miss", () => {
  const items = [cmd("zorua use work", { group: "Copy command" }), cmd("Refresh now")];
  const r = rank(items, "usw", (c) => c);
  assert.equal(r.length, 1);
  assert.equal(r[0].item.label, "zorua use work");
});

test("runs splits text into highlighted and plain parts", () => {
  assert.deepEqual(runs("Add account", [4, 5, 6]), [
    { text: "Add ", hit: false },
    { text: "acc", hit: true },
    { text: "ount", hit: false },
  ]);
  assert.deepEqual(runs("abc", []), [{ text: "abc", hit: false }]);
  assert.deepEqual(runs("", []), []);
  assert.deepEqual(runs("ab", [0, 1]), [{ text: "ab", hit: true }]);
});
