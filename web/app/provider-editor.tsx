"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Check, ProviderDoc } from "@/lib/types";
import { CheckBadge } from "./check";
import { CopyButton } from "./copy";
import { act } from "./manage";
import { actionBtn, alertDanger, card, dangerBtn, ghostBtn } from "./ui";

const field =
  "w-full rounded-lg border border-line-strong bg-bg px-2 py-1 font-mono text-xs text-fg outline-none transition-colors placeholder:text-dim/60 focus:border-accent";
const ghost = ghostBtn;

type Row = { id: number; k: string; v: string };
type Status = { kind: "idle" } | { kind: "saving" } | { kind: "saved" } | { kind: "failed"; message: string };

/** Same rule as zorua_providers.is_secret. */
const isSecret = (v: string) => /TOKEN|KEY|SECRET|PASSWORD/.test(v.toUpperCase()) && !v.toUpperCase().endsWith("_TOKENS");

// The few variables worth editing by hand; everything else sits under "Other variables".
const BASE_URL = "ANTHROPIC_BASE_URL";
const KEY_VARS = ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"];
const ROLES: [string, string][] = [
  ["ANTHROPIC_MODEL", "main model"],
  ["ANTHROPIC_DEFAULT_OPUS_MODEL", "opus"],
  ["ANTHROPIC_DEFAULT_SONNET_MODEL", "sonnet"],
  ["ANTHROPIC_DEFAULT_HAIKU_MODEL", "haiku"],
  ["CLAUDE_CODE_SUBAGENT_MODEL", "subagent"],
];
const keyVarOf = (env: Row[]) => KEY_VARS.find((v) => env.some((r) => r.k === v)) ?? KEY_VARS[0];

/** JSON with sorted keys, so re-adding a variable (it lands at the end) is not a change. */
const canon = (d: unknown) =>
  JSON.stringify(d, (_, v) => (v && typeof v === "object" && !Array.isArray(v) ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => (a < b ? -1 : 1))) : v));

let nextId = 1;
const rows = (o: Record<string, string>): Row[] => Object.entries(o).map(([k, v]) => ({ id: nextId++, k, v }));

async function fetchDoc(name: string, reveal: boolean): Promise<ProviderDoc> {
  const r = await fetch("/api/provider", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Zorua-Web": "1" },
    body: JSON.stringify({ name, reveal }),
    cache: "no-store",
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error ?? `HTTP ${r.status}`);
  return body.doc as ProviderDoc;
}

/** A one-line-looking input that grows to show a long value (a key is ~50 characters) in full. */
function Value({ label, value, onChange, className = "" }: { label: string; value: string; onChange: (v: string) => void; className?: string }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const t = ref.current;
    if (!t) return;
    t.style.height = "auto";
    t.style.height = `${t.scrollHeight + 2}px`;
  }, [value]);
  return (
    <textarea
      ref={ref}
      aria-label={label}
      rows={1}
      className={`${field} resize-none break-all leading-snug ${className}`}
      value={value}
      onChange={(e) => onChange(e.target.value.replace(/[\r\n]/g, ""))}
      spellCheck={false}
      autoComplete="off"
    />
  );
}

function Table({
  items,
  onChange,
  secret,
  keyLabel,
  valueLabel,
  addLabel,
  copy,
}: {
  items: Row[];
  onChange: (r: Row[]) => void;
  secret?: (k: string) => boolean;
  keyLabel: string;
  valueLabel: string;
  addLabel: string;
  copy?: (r: Row) => Promise<string>;
}) {
  const set = (id: number, patch: Partial<Row>) => onChange(items.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  return (
    <div>
      <div className={`grid ${copy ? "grid-cols-[minmax(9rem,20rem)_minmax(0,1fr)_auto_2rem]" : "grid-cols-[minmax(9rem,20rem)_minmax(0,1fr)_2rem]"} items-start gap-x-3 gap-y-2.5`}>
        {items.map((r) => (
          <div key={r.id} className="contents">
            <input aria-label={keyLabel} className={field} value={r.k} onChange={(e) => set(r.id, { k: e.target.value })} spellCheck={false} />
            <Value label={valueLabel} className={secret?.(r.k) ? "text-warn" : ""} value={r.v} onChange={(v) => set(r.id, { v })} />
            {copy && (secret?.(r.k) ? <CopyButton label={`copy ${r.k}`} get={() => copy(r)} /> : <span />)}
            <button type="button" aria-label={`delete ${r.k || "row"}`} className="rounded-lg border border-transparent py-1 text-dim hover:border-danger hover:text-danger" onClick={() => onChange(items.filter((x) => x.id !== r.id))}>
              ×
            </button>
          </div>
        ))}
      </div>
      <button type="button" className={`mt-4 ${actionBtn}`} onClick={() => onChange([...items, { id: nextId++, k: "", v: "" }])}>
        + {addLabel}
      </button>
    </div>
  );
}

function Labeled({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="grid gap-1 text-xs text-dim sm:grid-cols-[12rem_minmax(0,1fr)] sm:gap-3">
      <span className="py-1">{label}</span>
      <span>
        {children}
        {hint && <span className="mt-1 block text-xs text-dim">{hint}</span>}
      </span>
    </label>
  );
}

/** A single-line input offering the provider's model ids as suggestions. */
function ModelInput({ label, value, onChange, listId }: { label: string; value: string; onChange: (v: string) => void; listId: string }) {
  return <input aria-label={label} list={listId} className={field} value={value} onChange={(e) => onChange(e.target.value)} placeholder="not set" spellCheck={false} autoComplete="off" />;
}

/**
 * One provider as a page. There is no save button: a field is saved when it loses focus (and a
 * change that has no focus, like deleting a row, is saved at once). The previous file is kept as
 * providers.json.bak by `zorua provider put`.
 */
export function ProviderPage({
  name,
  endpoint,
  check,
  checking,
  onCheck,
  onSaved,
  onDirty,
  onRemove,
}: {
  name: string;
  endpoint: string;
  check?: Check;
  checking: boolean;
  onCheck: () => void;
  onSaved: () => void;
  onDirty: (dirty: boolean) => void;
  onRemove: () => void;
}) {
  const [base, setBase] = useState<ProviderDoc | null>(null); // as stored (secrets masked)
  const [env, setEnv] = useState<Row[]>([]);
  const [cat, setCat] = useState<Row[]>([]);
  const [codex, setCodex] = useState({ base_url: "", key: "", model: "", wire_api: "responses" });
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [tick, setTick] = useState(0); // bumped when an edit should be saved
  const inflight = useRef(false);
  const queued = useRef(false);
  const sending = useRef<ProviderDoc | null>(null); // the document of the save that is in flight

  const apply = useCallback((d: ProviderDoc) => {
    setCat(rows(d.models));
    if (d.agent === "claude") setEnv(rows(d.env));
    else setCodex({ base_url: d.base_url, key: d.key, model: d.model, wire_api: d.wire_api });
  }, []);

  useEffect(() => {
    let stop = false;
    fetchDoc(name, false)
      .then((d) => {
        if (stop) return;
        setBase(d);
        apply(d);
      })
      .catch((e) => !stop && setLoadError(e instanceof Error ? e.message : String(e)));
    return () => {
      stop = true;
    };
  }, [name, apply]);

  /** Stored secrets cannot be exported. Freshly typed values can still be copied. */
  const copyValue = async (shown: string, masked: string | undefined) => {
    if (masked !== undefined && shown === masked) throw new Error("Stored keys stay masked; enter a replacement to rotate the key.");
    return shown;
  };

  const keyVar = keyVarOf(env);
  const isBasic = (k: string) => k === BASE_URL || k === keyVar || ROLES.some(([v]) => v === k);
  const get = (v: string) => env.find((r) => r.k === v)?.v ?? "";
  /** Set one variable by name; an empty optional one is removed instead of saved as "". */
  const setVar = (v: string, value: string, optional = false) =>
    setEnv((cur) => {
      const i = cur.findIndex((r) => r.k === v);
      if (i < 0) return value === "" && optional ? cur : [...cur, { id: nextId++, k: v, v: value }];
      if (value === "" && optional) return cur.filter((_, j) => j !== i);
      return cur.map((r, j) => (j === i ? { ...r, v: value } : r));
    });
  /** The "Other variables" table edits only the non-basic rows, in place, so the order never changes. */
  const setOther = (next: Row[]) =>
    setEnv((cur) => {
      const byId = new Map(next.map((r) => [r.id, r]));
      const have = new Set(cur.map((r) => r.id));
      const kept = cur.flatMap((r) => (isBasic(r.k) ? [r] : byId.has(r.id) ? [byId.get(r.id)!] : []));
      return [...kept, ...next.filter((r) => !have.has(r.id))];
    });
  const modelIds = [...new Set(cat.map((r) => r.v).filter(Boolean))];

  const toMap = (items: Row[], what: string): Record<string, string> => {
    const out: Record<string, string> = {};
    for (const r of items) {
      if (!r.k && !r.v) continue;
      if (!r.k) throw new Error(`${what}: a row has a value but no name`);
      if (r.k in out) throw new Error(`${what}: '${r.k}' appears twice`);
      out[r.k] = r.v;
    }
    return out;
  };

  const built = useMemo((): { doc: ProviderDoc | null; problem: string | null } => {
    if (!base) return { doc: null, problem: null };
    try {
      const models = toMap(cat, "models");
      if (base.agent === "claude") return { doc: { agent: "claude", env: toMap(env, "env"), models }, problem: null };
      return { doc: { agent: "codex", ...codex, models }, problem: null };
    } catch (e) {
      return { doc: null, problem: e instanceof Error ? e.message : String(e) };
    }
  }, [base, cat, env, codex]);

  const reference = base;
  const dirty = built.doc !== null && reference !== null && canon(built.doc) !== canon(reference);

  // An edit whose save is already on its way is not "unsaved": leaving the page right after a field loses
  // its focus (which starts the save) must not ask "Discard unsaved changes?". A newer edit, or a failed
  // save, still counts.
  const inTransit = status.kind === "saving" && sending.current !== null && built.doc !== null && canon(built.doc) === canon(sending.current);
  const unsaved = dirty && !inTransit;
  useEffect(() => {
    onDirty(unsaved);
    return () => onDirty(false);
  }, [unsaved, onDirty]);

  const save = useCallback(async () => {
    if (!built.doc || !dirty) return;
    if (inflight.current) {
      queued.current = true;
      return;
    }
    inflight.current = true;
    const sent = built.doc;
    sending.current = sent;
    // Tell the page now, not after the next render: a click on a link starts this save (the field loses focus)
    // and navigates in the same moment. If the save fails the effect below flags the edit as unsaved again.
    onDirty(false);
    setStatus({ kind: "saving" });
    setError(null);
    try {
      await act({ action: "provider.save", name, doc: sent });
      // Re-read what is stored (secrets masked again). A secret that was just typed turns back into
      // its masked form unless it was edited again meanwhile; ids are kept so no field loses focus.
      const stored = await fetchDoc(name, false);
      setBase(stored);
      if (sent.agent === "claude" && stored.agent === "claude") {
        setEnv((cur) => cur.map((r) => (isSecret(r.k) && r.v === sent.env[r.k] && stored.env[r.k] !== undefined ? { ...r, v: stored.env[r.k] } : r)));
      } else if (sent.agent === "codex" && stored.agent === "codex") {
        setCodex((c) => (c.key === sent.key ? { ...c, key: stored.key } : c));
      }
      setStatus({ kind: "saved" });
      onSaved();
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setStatus({ kind: "failed", message });
    } finally {
      inflight.current = false;
      sending.current = null;
      if (queued.current) {
        queued.current = false;
        setTick((t) => t + 1);
      }
    }
  }, [built.doc, dirty, name, onSaved, onDirty]);

  useEffect(() => {
    if (tick > 0) save();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick]);

  useEffect(() => {
    if (status.kind !== "saved") return;
    const t = setTimeout(() => setStatus({ kind: "idle" }), 2000);
    return () => clearTimeout(t);
  }, [status]);

  const commit = () => setTick((t) => t + 1);

  if (loadError) return <p className={alertDanger}>{loadError}</p>;
  if (!base) return <p className="text-xs text-dim">reading {name}…</p>;

  const claude = base.agent === "claude";
  const failure = status.kind === "failed" ? status.message : (error ?? built.problem);
  const other = env.filter((r) => !isBasic(r.k));
  return (
    <form onSubmit={(e) => e.preventDefault()} onBlur={commit} autoComplete="off">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <span className="text-sm text-dim">
          {claude ? "Claude Code" : "Codex"} provider · <span className="font-mono text-[13px] text-fg">{endpoint}</span>
        </span>
        <span className="text-xs" aria-live="polite">
          {status.kind === "saving" ? (
            <span className="text-dim">saving…</span>
          ) : status.kind === "saved" ? (
            <span className="text-accent">✓ saved</span>
          ) : dirty && status.kind !== "failed" ? (
            <span className="text-warn">editing — saves when you leave the field</span>
          ) : null}
        </span>
        <CheckBadge check={check} checking={checking} onCheck={onCheck} />
        <span className="ml-auto flex flex-wrap items-center gap-2">
          <CopyButton variant="action" text="copy command" label={`zorua use ${name}`} get={async () => `zorua use ${name}`} />
          <button type="button" className={dangerBtn} onClick={onRemove}>
            remove provider
          </button>
        </span>
      </div>
      <p className="mt-3 text-xs leading-relaxed text-dim">
        {claude ? "What `claude` runs with while this provider is active." : "Codex gets this endpoint, key and model through -c overrides."} Changes are saved when you leave a field; the previous file is kept as
        providers.json.bak. Stored keys stay masked; enter a replacement key to rotate it.
      </p>

      {failure && (
        <p className={`mt-3 flex items-center gap-3 ${alertDanger}`}>
          <span className="min-w-0 flex-1 break-words">{status.kind === "failed" ? `not saved: ${failure}` : failure}</span>
          {status.kind === "failed" && (
            <button type="button" className={ghost} onClick={commit}>
              retry
            </button>
          )}
        </p>
      )}

      <div className={`mt-4 ${card}`}>
        <div className="grid gap-3 p-4">
          {claude ? (
            <>
              <Labeled label="base URL">
                <Value label="base URL" value={get(BASE_URL)} onChange={(v) => setVar(BASE_URL, v)} />
              </Labeled>
              <Labeled label="key" hint={keyVar}>
                <span className="flex items-start gap-2">
                  <span className="min-w-0 flex-1">
                    <Value label="key" className="text-warn" value={get(keyVar)} onChange={(v) => setVar(keyVar, v)} />
                  </span>
                </span>
              </Labeled>
              <div className="mt-1 border-t border-line pt-3 text-xs text-dim">Models Claude Code asks for; leave a line empty to use its default.</div>
              {ROLES.map(([v, label]) => (
                <Labeled key={v} label={label} hint={v}>
                  <ModelInput label={label} listId="model-ids" value={get(v)} onChange={(x) => setVar(v, x, true)} />
                </Labeled>
              ))}
              <datalist id="model-ids">
                {modelIds.map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
            </>
          ) : (
            <>
              <Labeled label="base URL">
                <Value label="base URL" value={codex.base_url} onChange={(v) => setCodex({ ...codex, base_url: v })} />
              </Labeled>
              <Labeled label="key">
                <span className="flex items-start gap-2">
                  <span className="min-w-0 flex-1">
                    <Value label="key" className="text-warn" value={codex.key} onChange={(v) => setCodex({ ...codex, key: v })} />
                  </span>
                </span>
              </Labeled>
              <Labeled label="model" hint="what Codex sends when no model is picked">
                <Value label="model" value={codex.model} onChange={(v) => setCodex({ ...codex, model: v })} />
              </Labeled>
              <Labeled label="wire API">
                <select
                  className={field}
                  value={codex.wire_api}
                  onChange={(e) => {
                    setCodex({ ...codex, wire_api: e.target.value });
                    commit();
                  }}
                >
                  <option value="responses">responses</option>
                  <option value="chat">chat</option>
                </select>
              </Labeled>
            </>
          )}
        </div>

        <details className="border-t border-line">
          <summary className="cursor-pointer px-4 py-2.5 text-xs text-dim hover:text-fg">Models ({cat.length}) · alias → model id, used by `zorua use {name}:&lt;alias&gt;`</summary>
          <div className="px-4 pb-4">
            <Table items={cat} onChange={(r) => { setCat(r); if (r.length < cat.length) commit(); }} keyLabel="alias" valueLabel="model id" addLabel="add model" />
          </div>
        </details>
        {claude && (
          <details className="border-t border-line">
            <summary className="cursor-pointer px-4 py-2.5 text-xs text-dim hover:text-fg">Other variables ({other.length})</summary>
            <div className="px-4 pb-4">
              <Table
                items={other}
                onChange={(r) => {
                  setOther(r);
                  if (r.length < other.length) commit();
                }}
                secret={isSecret}
                keyLabel="variable"
                valueLabel="value"
                addLabel="add variable"
                copy={(r) => copyValue(r.v, base.agent === "claude" && isSecret(r.k) ? base.env[r.k] : undefined)}
              />
            </div>
          </details>
        )}
        <details className="border-t border-line">
          <summary className="cursor-pointer px-4 py-2.5 text-xs text-dim hover:text-fg">JSON</summary>
          <pre className="max-h-[50vh] overflow-auto whitespace-pre-wrap break-all px-4 pb-4 text-xs text-dim">{built.doc ? JSON.stringify(built.doc, null, 2) : built.problem}</pre>
        </details>
      </div>
    </form>
  );
}
