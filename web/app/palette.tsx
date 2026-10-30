"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { rank, runs } from "./fuzzy";
import { Icon, type IconName } from "./icons";

export type Command = {
  id: string;
  group: string;
  label: string;
  hint?: string;
  /** Shown as a key cap on the right (the shortcut that does the same thing). */
  shortcut?: string;
  icon?: IconName;
  /** Extra words the search also matches. */
  keywords?: string;
  run: () => void;
};

const RECENT_KEY = "zorua-recent";

function readRecent(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string").slice(0, 3) : [];
  } catch {
    return [];
  }
}

function remember(id: string) {
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify([id, ...readRecent().filter((x) => x !== id)].slice(0, 3)));
  } catch {}
}

/** A command and the label / hint positions the typed words hit, for highlighting. */
type Hit = { c: Command; label: number[]; hint: number[] };

/** Every word typed must match (as a fuzzy subsequence) the label, hint, keywords or group; best match first. */
function search(commands: Command[], q: string): Hit[] {
  return rank(commands, q, (c) => c).map((r) => ({ c: r.item, label: r.label, hint: r.hint }));
}

/** The matched characters get an accent tint and underline and a heavier weight. The text colour stays the body colour: accent text on an accent tint is too faint in the light theme. */
function Marked({ text, at }: { text: string; at: number[] }) {
  if (at.length === 0) return text;
  return runs(text, at).map((r, i) =>
    r.hit ? (
      <mark key={i} className="rounded-[3px] bg-accent/20 font-semibold text-fg underline decoration-accent decoration-2 underline-offset-2">
        {r.text}
      </mark>
    ) : (
      r.text
    ),
  );
}

/** ⌘K: jump to an account or provider, run an action, copy a command. Mounted only while open. */
export function Palette({ commands, initial = "", onClose }: { commands: Command[]; initial?: string; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const [q, setQ] = useState(initial);
  const [active, setActive] = useState(0);
  const uid = useId();
  const [recent] = useState(readRecent);
  const shown = useMemo((): Hit[] => {
    if (q.trim()) return search(commands, q);
    // With nothing typed, the last few commands you ran come first.
    const again = recent.flatMap((id) => commands.filter((c) => c.id === id)).map((c) => ({ ...c, id: `recent:${c.id}`, group: "Recent" }));
    return [...again, ...commands].map((c) => ({ c, label: [], hint: [] }));
  }, [commands, q, recent]);
  const current = Math.min(active, Math.max(0, shown.length - 1));
  const count = shown.filter((h) => h.c.group !== "Recent").length; // a recent command is also listed in its own group

  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);

  useEffect(() => {
    list.current?.querySelector(`[data-index="${current}"]`)?.scrollIntoView({ block: "nearest" });
  }, [current]);

  const run = (c: Command | undefined) => {
    if (!c) return;
    ref.current?.close();
    remember(c.id.replace(/^recent:/, ""));
    c.run();
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((current + 1) % Math.max(1, shown.length));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((current - 1 + shown.length) % Math.max(1, shown.length));
    } else if (e.key === "Enter") {
      e.preventDefault();
      run(shown[current]?.c);
    }
  };

  let lastGroup = "";
  return (
    <dialog
      ref={ref}
      data-palette
      aria-label="Command palette"
      onClose={onClose}
      onClick={(e) => e.target === ref.current && ref.current?.close()}
      className="squircle m-0 mx-auto mt-[12vh] w-[min(92vw,36rem)] overflow-hidden rounded-2xl border border-line-strong bg-panel p-0 text-fg shadow-pop backdrop:bg-black/60 backdrop:backdrop-blur-sm open:animate-pop"
    >
      <div className="flex items-center gap-3 border-b border-line px-4">
        <Icon name="search" className="size-4 text-dim" />
        <input
          autoFocus
          role="combobox"
          aria-expanded
          aria-controls={`${uid}-list`}
          aria-activedescendant={shown[current] ? `${uid}-${current}` : undefined}
          aria-label="Search accounts, providers and actions"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKey}
          placeholder="Jump to an account, provider or action…"
          className="h-12 flex-1 bg-transparent text-sm outline-none placeholder:text-dim/70"
          spellCheck={false}
          autoComplete="off"
        />
        <kbd className="rounded border border-line-strong px-1.5 py-px text-xs text-dim">esc</kbd>
      </div>
      <div ref={list} id={`${uid}-list`} role="listbox" className="max-h-[min(24rem,52vh)] overflow-y-auto p-2">
        {shown.length === 0 && <p className="px-3 py-8 text-center text-xs text-dim">Nothing matches “{q}”.</p>}
        {shown.map(({ c, label, hint }, i) => {
          const header = !q && c.group !== lastGroup;
          lastGroup = c.group;
          return (
            <div key={c.id}>
              {header && <div className="px-3 pb-1 pt-2.5 text-xs font-medium uppercase tracking-wider text-dim">{c.group}</div>}
              <div
                id={`${uid}-${i}`}
                data-index={i}
                role="option"
                aria-selected={i === current}
                onMouseMove={() => i !== current && setActive(i)}
                onClick={() => run(c)}
                className={`flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 text-[13px] ${i === current ? "bg-accent/10 text-accent" : ""}`}
              >
                {c.icon && <Icon name={c.icon} className={i === current ? "" : "text-dim"} />}
                <span className="min-w-0 flex-1 truncate"><Marked text={c.label} at={label} /></span>
                {c.hint && <span className="truncate text-xs text-dim"><Marked text={c.hint} at={hint} /></span>}
                {c.shortcut && <kbd className="rounded border border-line-strong px-1.5 py-px font-mono text-xs text-dim">{c.shortcut}</kbd>}
                {i === current && <Icon name="enter" className="size-3.5" />}
              </div>
            </div>
          );
        })}
      </div>
      <div className="flex gap-4 border-t border-line px-4 py-2 text-xs text-dim">
        <span>↑↓ select</span>
        <span>↵ run</span>
        <span>esc close</span>
        <span className="ml-auto tabular-nums">{count} {count === 1 ? "result" : "results"}</span>
      </div>
    </dialog>
  );
}
