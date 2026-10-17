import { execFile } from "node:child_process";
import { homedir } from "node:os";
import path from "node:path";
import type { ProviderDoc, ZoruaState } from "./types";

export const CORE = process.env.ZORUA_CORE ?? path.join(homedir(), ".zorua", "zorua_core.py");
const TTL_MS = 30_000;
const MIN_FORCE_GAP_MS = 5_000;

type Cache = { at: number; data: ZoruaState };
// Kept on globalThis so route modules share one cache.
const g = globalThis as unknown as {
  __zoruaCache?: { cache: Cache | null; inflight: Promise<ZoruaState> | null; lastError: string | null; gen: number };
};
const store = (g.__zoruaCache ??= { cache: null, inflight: null, lastError: null, gen: 0 });

export class ZoruaError extends Error {}

/** Run `zorua_core.py <args>`; never through a shell. Secrets go in `env`, not in args. */
export function runCore(
  args: string[],
  opts: { env?: Record<string, string>; cwd?: string; timeout?: number; input?: string } = {},
): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = execFile(
      "python3",
      [CORE, ...args],
      {
        timeout: opts.timeout ?? 60_000,
        maxBuffer: 5 * 1024 * 1024,
        cwd: opts.cwd,
        env: { ...process.env, ...opts.env, ...(opts.cwd ? { PWD: opts.cwd } : {}) },
      },
      (err, stdout, stderr) => {
        if (err) {
          const msg = (stderr || stdout || err.message).trim().replace(/\s+/g, " ").slice(0, 400);
          return reject(new ZoruaError(msg));
        }
        resolve(stdout);
      },
    );
    if (opts.input !== undefined) {
      // If the core exits before it reads its input the write fails with EPIPE; without a listener that is an
      // uncaught exception and takes the whole server down. The callback above reports the real failure.
      child.stdin?.on("error", () => {});
      child.stdin?.end(opts.input);
    }
  });
}

async function readState(): Promise<ZoruaState> {
  const out = await runCore(["usage", "--json"]);
  try {
    return JSON.parse(out) as ZoruaState;
  } catch {
    throw new ZoruaError("zorua returned invalid JSON");
  }
}

function refresh(): Promise<ZoruaState> {
  if (!store.inflight) {
    const gen = store.gen;
    const read: Promise<ZoruaState> = readState()
      .then((data) => {
        // A change made while this read was running makes it stale: whoever waited for it gets it, but it is not cached.
        if (gen === store.gen) {
          store.cache = { at: Date.now(), data };
          store.lastError = null;
        }
        return data;
      })
      .catch((e: Error) => {
        store.lastError = e.message;
        throw e;
      })
      .finally(() => {
        if (store.inflight === read) store.inflight = null;
      });
    store.inflight = read;
  }
  return store.inflight;
}

/** Drop the snapshot, and any read still running, after a change so the next read is fresh. */
export function invalidate() {
  store.cache = null;
  store.inflight = null;
  store.gen += 1;
}

/** Stale-while-revalidate: an old snapshot is served at once and refreshed in the background. */
export async function getState(force: boolean) {
  const age = store.cache ? Date.now() - store.cache.at : Infinity;
  if (!store.cache || (force && age > MIN_FORCE_GAP_MS)) {
    const data = await refresh();
    return { data, stale: false, error: null as string | null };
  }
  const stale = age > TTL_MS;
  if (stale) refresh().catch(() => {});
  return { data: store.cache.data, stale, error: store.lastError };
}

/** A cheap, uncached read of the registry (no network): used to validate actions. */
export async function readRegistry(): Promise<ZoruaState> {
  return JSON.parse(await runCore(["ls", "--json"])) as ZoruaState;
}

/** One provider's editable document; secrets are always masked (including legacy `reveal`). */
export async function readProvider(name: string, reveal: boolean): Promise<ProviderDoc> {
  const out = await runCore(["provider", "get", name, ...(reveal ? ["--reveal"] : [])], { timeout: 15_000 });
  try {
    return JSON.parse(out) as ProviderDoc;
  } catch {
    throw new ZoruaError("zorua returned invalid JSON");
  }
}
