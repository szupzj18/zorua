// Fuzzy matching for the command palette. Pure and import-free so tests/fuzzy.test.mjs can load it directly.

export type Match = { score: number; indices: number[] };

const MATCH = 16;
/** The first character, a character after a separator, and a camelCase hump start a word; hitting one is worth more. */
const BONUS_FIRST = 10;
const BONUS_BOUNDARY = 8;
const BONUS_CAMEL = 7;
/** A hit right after the previous one, so "acc" in "account" beats the same letters spread out. */
const BONUS_CONSECUTIVE = 10;
/** Each character skipped between two hits, and each one before the first hit (up to a cap): earlier is better. */
const GAP = 1;
const LEAD = 0.5;
const LEAD_CAP = 5;

const SEPARATOR = /[\s\-_/.:·,]/;

function boundaryBonus(text: string, j: number): number {
  if (j === 0) return BONUS_FIRST;
  const prev = text[j - 1];
  if (SEPARATOR.test(prev)) return BONUS_BOUNDARY;
  const cur = text[j];
  if (cur !== cur.toLowerCase() && prev === prev.toLowerCase() && prev !== prev.toUpperCase()) return BONUS_CAMEL;
  return 0;
}

/** Match `word` (lower case, no spaces) as a subsequence of `text`, choosing the best-scoring placement. */
export function matchWord(word: string, text: string): Match | null {
  const m = word.length;
  const n = text.length;
  if (m === 0 || m > n) return null;
  const lower = text.split("").map((c) => c.toLowerCase());
  const bonus = Array.from({ length: n }, (_, j) => boundaryBonus(text, j));

  let prev = new Array<number>(n).fill(-Infinity);
  const from: number[][] = [];
  for (let j = 0; j < n; j++) if (lower[j] === word[0]) prev[j] = MATCH + bonus[j] - Math.min(j * LEAD, LEAD_CAP);
  for (let i = 1; i < m; i++) {
    const cur = new Array<number>(n).fill(-Infinity);
    const back = new Array<number>(n).fill(-1);
    // Best earlier hit k <= j-2 of the previous letter, as prev[k] + GAP*k; the gap cost is then -GAP*(j-1) for any j.
    let run = -Infinity;
    let runAt = -1;
    for (let j = 1; j < n; j++) {
      if (j >= 2 && prev[j - 2] + GAP * (j - 2) > run) {
        run = prev[j - 2] + GAP * (j - 2);
        runAt = j - 2;
      }
      if (lower[j] !== word[i]) continue;
      const adjacent = prev[j - 1] + BONUS_CONSECUTIVE;
      const spread = run - GAP * (j - 1);
      if (adjacent === -Infinity && spread === -Infinity) continue;
      const useAdjacent = adjacent >= spread;
      cur[j] = (useAdjacent ? adjacent : spread) + MATCH + bonus[j];
      back[j] = useAdjacent ? j - 1 : runAt;
    }
    from.push(back);
    prev = cur;
  }

  let end = -1;
  for (let j = 0; j < n; j++) if (prev[j] > -Infinity && (end < 0 || prev[j] > prev[end])) end = j;
  if (end < 0) return null;
  const indices = [end];
  for (let i = m - 2, j = end; i >= 0; i--) {
    j = from[i][j];
    indices.unshift(j);
  }
  return { score: prev[end], indices };
}

export type Fields = { label: string; hint?: string; keywords?: string; group: string };
export type Ranked<T> = { item: T; score: number; /** positions in `label` / `hint` to highlight */ label: number[]; hint: number[] };

/** A hit in the label counts fully; the same hit in a secondary field counts for less, so the name a command goes by wins. */
const WEIGHT = { label: 1, hint: 0.5, keywords: 0.5, group: 0.25 } as const;

/**
 * Every space-separated word of `query` must match somewhere (label, hint, keywords or group), in any order.
 * Best score first, ties in the original order. An empty query returns the items unchanged.
 */
export function rank<T>(items: T[], query: string, fields: (item: T) => Fields): Ranked<T>[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return items.map((item) => ({ item, score: 0, label: [], hint: [] }));
  const out: Ranked<T>[] = [];
  for (const item of items) {
    const f = fields(item);
    const label = new Set<number>();
    const hint = new Set<number>();
    let total = 0;
    let all = true;
    for (const w of words) {
      let best: { score: number; field: keyof typeof WEIGHT; indices: number[] } | null = null;
      for (const field of ["label", "hint", "keywords", "group"] as const) {
        const text = f[field];
        const hit = text ? matchWord(w, text) : null;
        if (hit && (!best || hit.score * WEIGHT[field] > best.score)) best = { score: hit.score * WEIGHT[field], field, indices: hit.indices };
      }
      if (!best) {
        all = false;
        break;
      }
      total += best.score;
      if (best.field === "label") best.indices.forEach((i) => label.add(i));
      else if (best.field === "hint") best.indices.forEach((i) => hint.add(i));
    }
    if (all) out.push({ item, score: total, label: [...label].sort((a, b) => a - b), hint: [...hint].sort((a, b) => a - b) });
  }
  return out.sort((a, b) => b.score - a.score);
}

/** Split `text` into runs for rendering; `hit` runs are the highlighted ones. */
export function runs(text: string, indices: number[]): { text: string; hit: boolean }[] {
  const at = new Set(indices);
  const out: { text: string; hit: boolean }[] = [];
  for (let j = 0; j < text.length; j++) {
    const hit = at.has(j);
    const last = out[out.length - 1];
    if (last && last.hit === hit) last.text += text[j];
    else out.push({ text: text[j], hit });
  }
  return out;
}
