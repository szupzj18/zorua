"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Account, Check, StateResponse } from "@/lib/types";
import { AccountView } from "./account-view";
import { attention } from "./attention";
import { checkProvider, newest } from "./check";
import { copyText } from "./copy";
import { Icon } from "./icons";
import { act, AddAccountForm, AddBindingForm, AddProviderForm, LoginBanner, Modal, RemoveAccountForm } from "./manage";
import { Overview, OverviewSkeleton } from "./overview";
import { type Command, Palette } from "./palette";
import { ProviderPage } from "./provider-editor";
import { Sidebar } from "./sidebar";
import { SHORTCUTS } from "./shortcuts";
import { cycleTheme, isCompact, setThemeMode, toggleDensity } from "./theme";
import { alertDanger, ghostBtn, outlineBtn, primaryBtn } from "./ui";
import { peak } from "./usage";
import { formatHash, parseHash, type View } from "./view";

const POLL_MS = 30_000;
const HOME = "#/";

function ConfirmForm({ text, label, body, onDone, onCancel }: { text: string; label: string; body: Record<string, unknown>; onDone: (m: string) => void; onCancel: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try {
          onDone((await act(body)).message);
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err));
          setBusy(false);
        }
      }}
    >
      <p className="mb-3 text-xs text-dim">{text}</p>
      {error && <p className={`mb-3 ${alertDanger}`}>{error}</p>}
      <div className="flex justify-end gap-2">
        <button type="button" onClick={onCancel} className={ghostBtn}>
          cancel
        </button>
        <button type="submit" disabled={busy} className="rounded-lg bg-danger px-3 py-1.5 text-xs font-semibold text-on-danger transition hover:brightness-110 disabled:opacity-50">
          {busy ? "working…" : label}
        </button>
      </div>
    </form>
  );
}

type Dialog =
  | { kind: "account" }
  | { kind: "provider" }
  | { kind: "binding" }
  | { kind: "help" }
  | { kind: "remove-account"; a: Account }
  | { kind: "remove-provider"; name: string }
  | { kind: "unbind"; dir: string };

const ownedDir = (home: string) => /\/\.(codex|claude)-[A-Za-z0-9_-]+$/.test(home);

export default function Dashboard() {
  const [res, setRes] = useState<StateResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [toast, setToast] = useState<{ text: string; undo?: () => Promise<void> } | null>(null);
  const [toastHeld, setToastHeld] = useState(false);
  const [loginFor, setLoginFor] = useState<string | null>(null);
  const [loginRun, setLoginRun] = useState(0); // a new sign-in for the same account must restart the banner
  const [view, setView] = useState<View>({ kind: "overview" });
  const [palette, setPalette] = useState<{ initial: string } | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const menuBtn = useRef<HTMLButtonElement>(null);
  const navWasOpen = useRef(false);
  const [checks, setChecks] = useState<Record<string, Check>>({});
  const [checking, setChecking] = useState<Set<string>>(new Set());
  const notice = toast?.text ?? null;
  const setNotice = useCallback((text: string | null) => setToast(text ? { text } : null), []);
  const dirty = useRef(false);
  const lastG = useRef(0);
  const loadSeq = useRef(0);
  const applied = useRef(HOME);

  const load = useCallback(async (force: boolean) => {
    // Requests can finish out of order (a slow poll against a fast forced refresh); only the newest one counts.
    const id = ++loadSeq.current;
    setLoading(true);
    try {
      const r = await fetch(`/api/state${force ? "?refresh=1" : ""}`, { cache: "no-store" });
      const body = await r.json();
      if (id !== loadSeq.current) return;
      if (!r.ok) throw new Error(body.error ?? `HTTP ${r.status}`);
      const next = body as StateResponse;
      setRes(next);
      // A removed provider's last check must not outlive it (a new provider may reuse the name).
      const names = new Set(next.data.providers.map((p) => p.name));
      setChecks((cur) => newest(Object.fromEntries(Object.entries(cur).filter(([k]) => names.has(k))), next.checks ?? {}));
      setErr(null);
    } catch (e) {
      if (id === loadSeq.current) setErr(e instanceof Error ? e.message : String(e));
    } finally {
      if (id === loadSeq.current) setLoading(false);
    }
  }, []);

  // The view lives in the URL hash (#/provider/kimi): links, back and reload all work.
  useEffect(() => {
    applied.current = location.hash || HOME;
    setView(parseHash(location.hash));
    const onHash = () => {
      const h = location.hash || HOME;
      if (h === applied.current) return;
      if (dirty.current && !window.confirm("Discard unsaved changes?")) {
        history.replaceState(null, "", applied.current);
        return;
      }
      dirty.current = false;
      applied.current = h;
      setView(parseHash(h));
    };
    const onUnload = (e: BeforeUnloadEvent) => {
      if (dirty.current) e.preventDefault();
    };
    window.addEventListener("hashchange", onHash);
    window.addEventListener("beforeunload", onUnload);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener("beforeunload", onUnload);
    };
  }, []);
  // Closing the drawer returns focus to the button that opened it; widening the window past lg turns the
  // drawer back into the sidebar, so it must not stay "open" (and the page inert).
  useEffect(() => {
    if (navWasOpen.current && !navOpen) menuBtn.current?.focus();
    navWasOpen.current = navOpen;
  }, [navOpen]);
  useEffect(() => {
    const wide = matchMedia("(min-width: 1024px)");
    const close = () => {
      if (wide.matches) setNavOpen(false);
    };
    wide.addEventListener("change", close);
    return () => wide.removeEventListener("change", close);
  }, []);
  const go = useCallback((v: View) => {
    setNavOpen(false);
    location.hash = formatHash(v);
  }, []);
  const setDirty = useCallback((d: boolean) => {
    dirty.current = d;
  }, []);

  const finished = (message: string, job?: { name: string }) => {
    setDialog(null);
    setNotice(message);
    if (job) {
      setLoginFor(job.name);
      setLoginRun((n) => n + 1);
    }
    load(true);
  };
  const onLoginFinished = useCallback(() => load(true), [load]);

  useEffect(() => {
    load(false);
    const t = setInterval(() => {
      if (!document.hidden) load(false);
    }, POLL_MS);
    const onVisible = () => {
      if (!document.hidden) load(false);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(t);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load]);

  // ⌘K / Ctrl+K toggles the palette. "/" and the single-key shortcuts (SHORTCUTS) work only when no field
  // has focus and no dialog is open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const anyDialog = document.querySelector("dialog[open]");
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (!document.querySelector("dialog[open]:not([data-palette])")) setPalette((p) => (p ? null : { initial: "" }));
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey || anyDialog) return;
      const t = e.target as HTMLElement | null;
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
      const k = e.key;
      const sequence = k === "o" && Date.now() - lastG.current < 1000;
      lastG.current = k === "g" ? Date.now() : 0;
      if (k === "/") setPalette({ initial: "" });
      else if (k === "r") void load(true);
      else if (k === "a") setDialog({ kind: "account" });
      else if (k === "p") setDialog({ kind: "provider" });
      else if (k === "b") setDialog({ kind: "binding" });
      else if (k === "t") cycleTheme();
      else if (k === "d") toggleDensity();
      else if (k === "?") setDialog({ kind: "help" });
      else if (sequence) go({ kind: "overview" });
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [load, go]);

  useEffect(() => {
    if (!toast) setToastHeld(false);
  }, [toast]);

  // A toast goes away on its own, but not while the pointer or keyboard focus is on it.
  useEffect(() => {
    if (!toast || toastHeld) return;
    const t = setTimeout(() => setToast(null), toast.undo ? 10_000 : 7000);
    return () => clearTimeout(t);
  }, [toast, toastHeld]);

  const data = res?.data;
  const items = useMemo(() => (data ? attention(data, checks) : []), [data, checks]);
  // The tab title carries the count, so a pinned tab shows when something needs a look.
  useEffect(() => {
    document.title = `${items.length ? `(${items.length}) ` : ""}Zorua — accounts and usage`;
  }, [items.length]);
  const startLogin = async (a: Account) => {
    try {
      const r = await act({ action: "account.login", name: a.name });
      finished(r.message, r.job);
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e));
    }
  };

  const check = async (name: string) => {
    setChecking((cur) => new Set(cur).add(name));
    try {
      const c = await checkProvider(name);
      setChecks((cur) => ({ ...cur, [name]: c }));
    } catch (e) {
      setNotice(`check ${name}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setChecking((cur) => {
        const next = new Set(cur);
        next.delete(name);
        return next;
      });
    }
  };
  // In parallel: each check is one request to a different endpoint, and one slow endpoint must not hold up the rest.
  const checkAll = async () => {
    await Promise.all((data?.providers ?? []).map((p) => check(p.name)));
  };
  // A saved provider has a new endpoint or key, so its last result no longer applies.
  const dropCheck = (name: string) =>
    setChecks((cur) => {
      const { [name]: _gone, ...rest } = cur;
      return rest;
    });

  const account = view.kind === "account" ? data?.accounts.find((a) => a.name === view.name) : undefined;
  const provider = view.kind === "provider" ? data?.providers.find((p) => p.name === view.name) : undefined;
  const title = view.kind === "overview" ? "Overview" : view.kind === "account" ? `Account · ${view.name}` : `Provider · ${view.name}`;

  /** What ⌘K offers: every account and provider by name, the page-level actions, and the copy-command shortcuts. */
  const commands = (): Command[] => {
    const out: Command[] = [{ id: "go:overview", group: "Go to", label: "Overview", icon: "grid", shortcut: "g o", run: () => go({ kind: "overview" }) }];
    const copy = (text: string) =>
      copyText(text).then(
        () => setNotice(`Copied: ${text}`),
        () => setNotice("Copy failed"),
      );
    for (const a of data?.accounts ?? []) {
      const p = peak(a);
      out.push({
        id: `go:a:${a.name}`,
        group: "Go to",
        label: a.name,
        icon: "user",
        hint: `${a.agent === "codex" ? "Codex" : "Claude Code"}${a.plan ? ` · ${a.plan}` : ""}${p != null ? ` · ${p}%` : ""}`,
        keywords: `account ${a.email ?? ""}`,
        run: () => go({ kind: "account", name: a.name }),
      });
    }
    for (const p of data?.providers ?? []) {
      out.push({ id: `go:p:${p.name}`, group: "Go to", label: p.name, icon: "plug", hint: `${p.agent} provider`, keywords: "provider", run: () => go({ kind: "provider", name: p.name }) });
    }
    out.push(
      { id: "act:refresh", group: "Actions", label: "Refresh now", icon: "refresh", shortcut: "r", run: () => load(true) },
      { id: "act:account", group: "Actions", label: "Add account", icon: "plus", shortcut: "a", run: () => setDialog({ kind: "account" }) },
      { id: "act:provider", group: "Actions", label: "Add provider", icon: "plus", shortcut: "p", run: () => setDialog({ kind: "provider" }) },
      { id: "act:binding", group: "Actions", label: "Bind a directory", icon: "plus", shortcut: "b", run: () => setDialog({ kind: "binding" }) },
    );
    if (data?.providers.length) out.push({ id: "act:check", group: "Actions", label: "Check all providers", icon: "plug", run: () => void checkAll() });
    for (const n of [...(data?.accounts ?? []), ...(data?.providers ?? [])].map((x) => x.name)) {
      out.push({ id: `copy:${n}`, group: "Copy command", label: `zorua use ${n}`, icon: "copy", hint: "copy command", run: () => void copy(`zorua use ${n}`) });
    }
    out.push(
      { id: "act:density", group: "Actions", label: isCompact() ? "Comfortable rows" : "Compact rows", icon: "grid", shortcut: "d", keywords: "density spacing", run: toggleDensity },
      { id: "act:help", group: "Actions", label: "Keyboard shortcuts", icon: "search", shortcut: "?", run: () => setDialog({ kind: "help" }) },
      { id: "theme:auto", group: "Theme", label: "Match system", icon: "monitor", keywords: "auto theme", run: () => setThemeMode("auto") },
      { id: "theme:light", group: "Theme", label: "Light", icon: "sun", keywords: "theme", run: () => setThemeMode("light") },
      { id: "theme:dark", group: "Theme", label: "Dark", icon: "moon", keywords: "theme", run: () => setThemeMode("dark") },
    );
    return out;
  };

  return (
    <div className="mx-auto grid max-w-[1800px] gap-6 px-4 pb-16 pt-4 lg:grid-cols-[15.5rem_minmax(0,1fr)] lg:px-6">
      <Sidebar data={data} view={view} items={items} onGo={go} onSearch={() => setPalette({ initial: "" })} open={navOpen} onClose={() => setNavOpen(false)} />

      <main id="main" tabIndex={-1} inert={navOpen} className="min-w-0 outline-none">
        <header className="sticky top-0 z-20 -mx-4 flex flex-wrap items-center gap-x-3 gap-y-3 border-b border-line bg-bg/80 px-4 py-3 backdrop-blur-md lg:-mx-6 lg:px-6">
          <button ref={menuBtn} type="button" onClick={() => setNavOpen(true)} aria-label="Open menu" className="rounded-lg border border-line-strong p-2 text-dim hover:text-fg lg:hidden">
            <Icon name="menu" />
          </button>
          <div className="min-w-0 flex-1">
            <div className="truncate font-mono text-xs text-accent">$ zorua usage{data ? ` · ${data.version}` : ""}</div>
            <h1 className="truncate text-xl font-semibold leading-tight tracking-tight">{title}</h1>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={() => setPalette({ initial: "" })} aria-label="Search" className="rounded-lg border border-line-strong p-2 text-dim hover:text-fg lg:hidden">
              <Icon name="search" />
            </button>
            <button type="button" onClick={() => setPalette({ initial: "add " })} aria-label="Add" className="rounded-lg border border-accent/60 p-2 text-accent hover:bg-accent/10 sm:hidden">
              <Icon name="plus" />
            </button>
            {([["account", "account"], ["provider", "provider"], ["binding", "binding"]] as const).map(([k, label]) => (
              <button key={k} type="button" onClick={() => setDialog({ kind: k })} className={`${outlineBtn} hidden items-center gap-1 sm:inline-flex`}>
                <Icon name="plus" className="size-3.5" />
                {label}
              </button>
            ))}
            <button type="button" onClick={() => load(true)} disabled={loading} aria-label="Refresh" title="Refresh (r)" className={`${primaryBtn} inline-flex items-center gap-1.5 max-sm:p-2`}>
              <Icon name="refresh" className={`size-3.5 max-sm:size-4 ${loading ? "animate-spin" : ""}`} />
              <span className="max-sm:hidden">{loading ? "loading…" : "refresh"}</span>
            </button>
          </div>
        </header>

        <p className="mt-4 flex items-center gap-2 text-xs text-dim" aria-live="polite">
          <span className={`size-1.5 rounded-full ${res ? (res.stale || loading ? "animate-pulse bg-warn" : "bg-accent") : err ? "bg-danger" : "animate-pulse bg-dim"}`} aria-hidden />
          {res ? `updated ${new Date(res.data.generated_at * 1000).toLocaleTimeString()}${res.stale ? " · refreshing" : ""}` : err ? "could not read accounts" : "reading accounts…"}
        </p>
        {(err || res?.error) && (
          <p className={`mt-3 flex items-center gap-3 ${alertDanger}`} role="alert">
            <span className="min-w-0 flex-1 break-words">{err ?? res?.error}</span>
            <button type="button" onClick={() => load(true)} disabled={loading} className="shrink-0 rounded-lg border border-danger/50 px-2.5 py-1 font-medium hover:bg-danger/10 disabled:opacity-50">
              retry
            </button>
          </p>
        )}
        {loginFor && <LoginBanner key={`${loginFor}:${loginRun}`} name={loginFor} onFinished={onLoginFinished} />}

        <div className="mt-6">
          {!res && !err && <OverviewSkeleton />}
          {data && view.kind === "overview" && (
            <Overview
              data={data}
              checks={checks}
              checking={checking}
              onOpen={go}
              onLogin={startLogin}
              onCheck={check}
              onCheckAll={checkAll}
              onRemoveAccount={(a) => setDialog({ kind: "remove-account", a })}
              onRemoveProvider={(name) => setDialog({ kind: "remove-provider", name })}
              onUnbind={(dir) => setDialog({ kind: "unbind", dir })}
            />
          )}
          {data && view.kind === "account" &&
            (account ? (
              <AccountView a={account} bindings={data.bindings.filter((b) => b.name === account.name)} onLogin={() => startLogin(account)} onRemove={() => setDialog({ kind: "remove-account", a: account })} onUnbind={(dir) => setDialog({ kind: "unbind", dir })} />
            ) : (
              <NotFound what={`account '${view.name}'`} onBack={() => go({ kind: "overview" })} />
            ))}
          {data && view.kind === "provider" &&
            (provider ? (
              <ProviderPage
                key={provider.name}
                name={provider.name}
                endpoint={provider.endpoint}
                check={checks[provider.name]}
                checking={checking.has(provider.name)}
                onCheck={() => check(provider.name)}
                onDirty={setDirty}
                onSaved={() => {
                  dropCheck(provider.name);
                  load(true);
                }}
                onRemove={() => setDialog({ kind: "remove-provider", name: provider.name })}
              />
            ) : (
              <NotFound what={`provider '${view.name}'`} onBack={() => go({ kind: "overview" })} />
            ))}
        </div>

        <footer className="mt-12 border-t border-line pt-4 text-xs leading-relaxed text-dim">
          Data comes from <code className="text-fg">zorua usage --json</code>; changes run the matching <code className="text-fg">zorua</code> command. Account tokens never reach this page; stored provider keys always stay masked.
        </footer>
      </main>

      <div aria-live="polite" className="pointer-events-none fixed inset-x-0 bottom-6 z-50 flex justify-center px-4">
        {toast && (
          <div
            className="squircle pointer-events-auto flex max-w-[34rem] animate-toast items-center gap-3 rounded-xl border border-line-strong bg-panel px-4 py-2.5 text-xs shadow-pop"
            onMouseEnter={() => setToastHeld(true)}
            onMouseLeave={() => setToastHeld(false)}
            onFocus={() => setToastHeld(true)}
            onBlur={() => setToastHeld(false)}
          >
            <Icon name="check" className="size-4 text-accent" />
            <span className="min-w-0 break-words">{toast.text}</span>
            {toast.undo && (
              <button
                type="button"
                className="font-semibold text-accent hover:underline"
                onClick={() => {
                  const undo = toast.undo!;
                  setToast(null);
                  undo().catch((e) => setNotice(`undo failed: ${e instanceof Error ? e.message : String(e)}`));
                }}
              >
                undo
              </button>
            )}
            <button type="button" onClick={() => setToast(null)} aria-label="Dismiss" className="text-dim hover:text-fg">
              <Icon name="close" className="size-3.5" />
            </button>
          </div>
        )}
      </div>
      {palette && <Palette commands={commands()} initial={palette.initial} onClose={() => setPalette(null)} />}

      {dialog?.kind === "account" && (
        <Modal title="Add account" onClose={() => setDialog(null)}>
          <AddAccountForm onDone={(r) => finished(r.message, r.job)} onCancel={() => setDialog(null)} />
        </Modal>
      )}
      {dialog?.kind === "provider" && (
        <Modal title="Add provider" onClose={() => setDialog(null)}>
          <AddProviderForm onDone={(r) => finished(r.message)} onCancel={() => setDialog(null)} />
        </Modal>
      )}
      {dialog?.kind === "binding" && data && (
        <Modal title="Bind a directory" onClose={() => setDialog(null)}>
          <AddBindingForm data={data} onDone={(r) => finished(r.message)} onCancel={() => setDialog(null)} />
        </Modal>
      )}
      {dialog?.kind === "remove-account" && (
        <Modal title={`Remove ${dialog.a.name}`} onClose={() => setDialog(null)}>
          <RemoveAccountForm
            name={dialog.a.name}
            home={dialog.a.home}
            canPurge={ownedDir(dialog.a.home)}
            onDone={(r) => {
              finished(r.message);
              if (view.kind === "account" && view.name === dialog.a.name) go({ kind: "overview" });
            }}
            onCancel={() => setDialog(null)}
          />
        </Modal>
      )}
      {dialog?.kind === "remove-provider" && (
        <Modal title={`Remove provider ${dialog.name}`} onClose={() => setDialog(null)}>
          <ConfirmForm
            text="The provider and its stored key are deleted from Zorua."
            label="remove"
            body={{ action: "provider.remove", name: dialog.name }}
            onDone={(m) => {
              dirty.current = false;
              dropCheck(dialog.name);
              finished(m);
              if (view.kind === "provider" && view.name === dialog.name) go({ kind: "overview" });
            }}
            onCancel={() => setDialog(null)}
          />
        </Modal>
      )}
      {dialog?.kind === "help" && (
        <Modal title="Keyboard shortcuts" onClose={() => setDialog(null)}>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-4 gap-y-2.5 text-xs">
            {SHORTCUTS.map(([keys, what]) => (
              <div key={keys} className="contents">
                <dt>
                  <kbd className="whitespace-nowrap rounded border border-line-strong px-1.5 py-0.5 text-xs text-dim">{keys}</kbd>
                </dt>
                <dd className="text-fg">{what}</dd>
              </div>
            ))}
          </dl>
        </Modal>
      )}
      {dialog?.kind === "unbind" && (
        <Modal title="Unbind directory" onClose={() => setDialog(null)}>
          <ConfirmForm
            text={`Stop switching automatically in ${dialog.dir}.`}
            label="unbind"
            body={{ action: "binding.remove", dir: dialog.dir }}
            onDone={(m) => {
              const { dir } = dialog;
              const name = data?.bindings.find((b) => b.dir === dir)?.name;
              finished(m);
              if (name)
                setToast({
                  text: m,
                  undo: async () => {
                    await act({ action: "binding.add", dir, name });
                    setNotice(`Bound ${dir} to ${name} again`);
                    void load(true);
                  },
                });
            }}
            onCancel={() => setDialog(null)}
          />
        </Modal>
      )}
    </div>
  );
}

function NotFound({ what, onBack }: { what: string; onBack: () => void }) {
  return (
    <p className="rounded-lg border border-line bg-panel px-4 py-3 text-sm text-dim">
      No {what}.{" "}
      <button type="button" onClick={onBack} className="text-accent underline">
        back to overview
      </button>
    </p>
  );
}
