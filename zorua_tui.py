"""Zorua TUI: `zorua tui`, a full-screen picker for accounts and providers (stdlib curses only).

The pieces that decide something are plain functions with no curses in them, so they can be
tested without a terminal:

  build_rows(core, ...)       the row model, from the same data `zorua ls` / `zorua usage` use
  handle_key(ui, key, rows)   one key press -> new UI state + the action it asks for

Drawing is the only part that talks to curses. Switching does not happen here: `run()` returns
the chosen action and the caller runs `cmd_use` / `cmd_off` once the terminal is restored, so the
statements written for the calling shell are exactly the ones `zorua use <name>` writes.

`core` is the zorua_core module object, passed in (not imported) because zorua_core usually runs
as __main__ and a second import would get its own, unflushed, Emitter.
"""
import locale
import os
import sys
import threading
import unicodedata

try:
    import curses
except ImportError:          # e.g. a Python build without _curses
    curses = None

KEY_UP = getattr(curses, "KEY_UP", 259)
KEY_DOWN = getattr(curses, "KEY_DOWN", 258)
KEY_ENTER = getattr(curses, "KEY_ENTER", 343)
KEY_HOME = getattr(curses, "KEY_HOME", 262)
KEY_END = getattr(curses, "KEY_END", 360)
KEY_RESIZE = getattr(curses, "KEY_RESIZE", 410)
ESC = 27

KEYS_LINE = "up/down j/k move  Enter use  o off  r refresh  q quit  ? help"
HELP_LINES = (
    "Enter  switch the calling shell to the highlighted account or provider (same as zorua use)",
    "o      clear every switch in the calling shell (same as zorua off)",
    "r      fetch live usage again       up/down j/k  move     g / G  first / last",
    "q Esc  quit without changing anything",
)
STATE_TEXT = {"ok": "ok", "none": "signed out", "apikey": "api key", "bad": "error"}
NOT_TTY = ("zorua tui: needs an interactive terminal (stdin and stdout must be a TTY) with curses; "
           "use 'zorua ls' or 'zorua usage' to list accounts and 'zorua use <name>' to switch")
NO_CURSOR = ("zorua tui: terminal type '%s' cannot move the cursor, so a full-screen view cannot draw; "
             "try TERM=xterm-256color zorua tui, or zorua ls / zorua use <name>")
UNKNOWN_TERM = ("zorua tui: terminal type '%s' is unknown here (no terminfo); try "
                "TERM=xterm-256color zorua tui, or zorua ls / zorua use <name>")


# --------------------------------------------------------------------------
# Row model (pure)
# --------------------------------------------------------------------------

def _windows(core, data, remaining):
    """-> [{label, used, shown, reset}] for the 5h window first, then the weekly one."""
    out = []
    rl = (data or {}).get("rate_limit") or {}
    for key in ("primary_window", "secondary_window"):
        w = rl.get(key)
        if not isinstance(w, dict) or not w.get("limit_window_seconds"):
            continue
        used = w.get("used_percent")
        out.append({"label": core.label(w["limit_window_seconds"]),
                    "used": used, "shown": core.remaining_of(used) if remaining else used,
                    "reset": core.left(w["reset_after_seconds"]) if w.get("reset_after_seconds") is not None else ""})
    out.sort(key=lambda w: w["label"].endswith("D"))
    return out


def build_rows(core, all_acc, info, live, provs, remaining=False, full=False, ages=None, expired=None):
    """-> list of rows: {"heading": title} or an entry dict with "name" and "kind".

    all_acc / info / live are what core.all_accounts() / core.collect_usage() return; info is None
    while the first read is still running and live is None until online usage arrived.
    provs is zp.load(). Active entries come from core.current_account(), the rule ls uses.
    ages / expired are collect_usage's claude_age / claude_expired: Claude windows are the relay's
    cache from the last session, so their row says how old they are."""
    env = os.environ
    zp = core.zp
    hide = core.email_masker([i.get("email") for i in (info or {}).values()], full)
    rows = []
    for kind in ("codex", "claude", "grok"):
        group = [(n, h) for n, h, k in all_acc if k == kind]
        if not group:
            continue
        rows.append({"heading": core.TITLES[kind]})
        cur = core.current_account(kind)
        auto = env.get(core.AUTO_VARS[kind][0], "")
        for n, h in group:
            i = (info or {}).get(n)
            data, error = (live or {}).get(n, (None, None))
            row = {"name": n, "kind": kind, "home": h,
                   "active": cur == n,
                   "auto": bool(auto) and auto == n,
                   "state": "…", "email": "", "plan": "", "windows": [], "usage": "…" if live is None else ""}
            if i is not None:
                row["state"] = STATE_TEXT.get(i["state"], i["state"])
                row["email"] = (hide(i["email"]) or "") if i["state"] in ("ok", "apikey") else ""
                if i["state"] == "bad" and i.get("email"):     # read_claude_info keeps its error there
                    row["usage"] = hide(i["email"])
                row["plan"] = (data or {}).get("plan_type") or i.get("plan") or "–"
            if live is not None:
                row["windows"] = _windows(core, data, remaining)
                if error and error != "-":
                    row["usage"] = error
                elif kind == "claude" and (ages or {}).get(n) is not None:
                    note = ["as of %s ago" % core.ago(ages[n])]
                    note += ["%s reset %s ago" % (core.label(g["window_seconds"]), core.ago(g["reset_ago_seconds"]))
                             for g in (expired or {}).get(n, [])]
                    row["usage"] = ", ".join(note)
                    row["note"] = True
                elif not row["windows"] and not row["usage"]:
                    row["usage"] = "–"
            rows.append(row)
    for agent in zp.AGENTS:
        kind = core.AGENT_PROVIDER[agent]
        mine = sorted((n, p) for n, p in provs.items() if zp.agent(p) == agent)
        if not mine:
            continue
        rows.append({"heading": "Providers (%s)" % core.TITLES[agent]})
        cur = core.current_account(kind)
        auto = env.get(core.AUTO_VARS[kind][0], "")
        for n, p in mine:
            active = cur == n
            pick = env.get(core.MODEL_VAR[kind], "") if active else ""
            default = zp.default_model(p)
            if pick:
                model = "model %s (this shell)" % pick
            elif default:
                model = "model %s (default)" % zp.selected_alias(p, default)
            else:
                model = "model: claude's own /model choice" if agent == "claude" else "model –"
            rows.append({"name": n, "kind": kind, "active": active, "auto": bool(auto) and auto == n,
                         "endpoint": zp.endpoint(p), "model": model, "summary": zp.model_summary(p)})
    return rows


def entries(rows):
    """-> indexes of the selectable rows (everything but headings)"""
    return [i for i, r in enumerate(rows) if "name" in r]


def initial_ui(rows):
    """Start on the first entry active in this shell, else the first entry."""
    sel = next((k for k, i in enumerate(entries(rows)) if rows[i]["active"]), 0)
    return {"sel": sel, "help": False}


# --------------------------------------------------------------------------
# Keys (pure)
# --------------------------------------------------------------------------

def handle_key(ui, key, rows):
    """-> (new ui, action). ui["sel"] indexes entries(rows). key is a curses key code or a
    one-character string. action is None, "quit", "off", "refresh", "redraw" or ("use", name)."""
    if isinstance(key, str):
        key = ord(key) if len(key) == 1 else -1
    ui = dict(ui)
    n = len(entries(rows))
    if key in (ord("q"), ESC):
        return ui, "quit"
    if key in (KEY_DOWN, ord("j")):
        ui["sel"] = min(ui["sel"] + 1, n - 1) if n else 0
    elif key in (KEY_UP, ord("k")):
        ui["sel"] = max(ui["sel"] - 1, 0)
    elif key in (KEY_HOME, ord("g")):
        ui["sel"] = 0
    elif key in (KEY_END, ord("G")):
        ui["sel"] = max(n - 1, 0)
    elif key in (10, 13, KEY_ENTER):
        if n:
            return ui, ("use", rows[entries(rows)[ui["sel"]]]["name"])
    elif key == ord("o"):
        return ui, "off"
    elif key == ord("r"):
        return ui, "refresh"
    elif key == ord("?"):
        ui["help"] = not ui["help"]
    elif key == KEY_RESIZE:
        return ui, "redraw"
    return ui, None


# --------------------------------------------------------------------------
# Data loading (background, so the first paint never waits on claude or the network)
# --------------------------------------------------------------------------

class Loader:
    """Reads local sign-in info, then online usage, in a daemon thread; `snapshot()` is polled."""

    def __init__(self, core, all_acc):
        self.core, self.all_acc = core, all_acc
        self.lock = threading.Lock()
        self.info = self.live = None
        self.ages, self.expired = {}, {}
        self.error = ""
        self.thread = None

    def start(self):
        if self.busy():
            return False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return True

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def _run(self):
        try:
            # The first load shows sign-ins early, then reuses them for the online pass; a
            # refresh (`r`) reads them again.
            first = None
            if self.info is None:
                first, _, _, _ = self.core.collect_usage(self.all_acc, False)
                with self.lock:
                    self.info = first
            info, live, ages, expired = self.core.collect_usage(self.all_acc, True, info=first)
            with self.lock:
                self.info, self.live, self.error = info, live, ""
                self.ages, self.expired = ages, expired
        except Exception as e:          # shown in the status line; the list stays usable
            with self.lock:
                self.error = "usage failed: %s" % e

    def snapshot(self):
        with self.lock:
            return self.info, self.live, self.error, self.ages, self.expired


# --------------------------------------------------------------------------
# Drawing (curses)
# --------------------------------------------------------------------------

def _char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def width(s):
    """Terminal columns `s` takes: CJK and other wide characters count 2, combining marks 0."""
    return sum(_char_width(ch) for ch in s)


def clip(s, cols):
    """The longest prefix of `s` that fits in `cols` terminal columns."""
    used = 0
    for i, ch in enumerate(s):
        used += _char_width(ch)
        if used > cols:
            return s[:i]
    return s


def _cells(row, glyph):
    """-> list of (text, colour role) cells for one row; headings are a single cell."""
    if "heading" in row:
        return [(row["heading"], "heading")]
    mark = glyph["active"] if row["active"] else glyph["auto"] if row["auto"] else " "
    head = [(mark, "active" if row["active"] else "auto"), (row["name"], "name")]
    if "endpoint" in row:
        return head + [("provider", "dim"), (row["endpoint"], None), (row["model"], "dim"), (row["summary"], "dim")]
    cells = head + [(row["state"], "bad" if row["state"] not in ("ok", "…") else "dim"),
                    (row["email"], None), (row["plan"], "dim")]
    for w in row["windows"]:
        if w["shown"] is None:
            cells.append(("%s –" % w["label"], "dim"))
        else:
            used = w["used"]
            role = "red" if used >= 80 else "yellow" if used >= 50 else "green"
            cells.append(("%s %3d%%%s %s" % (w["label"], w["shown"], " left" if glyph["remaining"] else "",
                                              w["reset"]), role))
    if row["usage"]:
        cells.append((row["usage"], "dim" if row["usage"] in ("…", "–") or row.get("note") else "bad"))
    return cells


def layout(rows, glyph):
    """-> one list of (x, text, role) per row. The first five columns line up across the account
    rows, and separately across the provider rows; the name column lines up across both."""
    cells = [_cells(r, glyph) for r in rows]
    family = ["heading" if "heading" in r else "provider" if "endpoint" in r else "account" for r in rows]
    widths = {}
    for c, f in zip(cells, family):
        if f != "heading":
            for k, (s, _) in enumerate(c[:5]):
                key = k if k < 2 else (f, k)
                widths[key] = max(widths.get(key, 0), width(s))
    out = []
    for c, f in zip(cells, family):
        x, line = 0, []
        for k, (s, role) in enumerate(c):
            line.append((x, s, role))
            w = widths.get(k if k < 2 else (f, k), width(s)) if f != "heading" and k < 5 else width(s)
            x += w + (1 if k == 0 else 2)
        out.append(line)
    return out


class Screen:
    def __init__(self, scr):
        self.scr = scr
        self.attr = {None: 0, "dim": curses.A_DIM, "heading": curses.A_BOLD, "name": curses.A_BOLD,
                     "bad": 0, "active": 0, "auto": 0, "red": 0, "yellow": 0, "green": 0}
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors() and not os.environ.get("NO_COLOR"):     # bold / dim / reverse stay
            try:
                curses.start_color()
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            for pair, (role, fg) in enumerate((("red", curses.COLOR_RED), ("yellow", curses.COLOR_YELLOW),
                                               ("green", curses.COLOR_GREEN), ("active", curses.COLOR_CYAN),
                                               ("auto", curses.COLOR_YELLOW), ("bad", curses.COLOR_RED)), 1):
                try:
                    curses.init_pair(pair, fg, bg)
                    self.attr[role] = curses.color_pair(pair)
                except curses.error:
                    pass

    def put(self, y, x, text, attr=0):
        """Write what fits; never raises at the screen edges (the last cell, a tiny window)."""
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w - 1 or not text:
            return
        # Clip by display columns, not characters: a wide character takes two, and text that ran
        # past the right edge would wrap onto the next row.
        for t in (text, text.encode("ascii", "replace").decode()):    # a non-UTF-8 locale
            t = clip(t, w - 1 - x)
            if not t:
                return
            try:
                self.scr.addstr(y, x, t, attr)
                return
            except UnicodeEncodeError:
                continue
            except (curses.error, ValueError):
                return

    def draw(self, rows, ui, status, glyph):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        self.put(0, 0, "Zorua  " + KEYS_LINE, curses.A_BOLD)
        foot = list(HELP_LINES) if ui["help"] else []
        body_top, body_h = 2, max(0, h - 2 - len(foot) - 1)
        lines = layout(rows, glyph)
        idx = entries(rows)
        cur = idx[ui["sel"]] if idx else -1
        # Scroll so the highlighted row (and its heading, when there is room) stays visible.
        top = ui.get("top", 0)
        if cur >= 0 and body_h:
            want = cur - 1 if cur and "heading" in rows[cur - 1] else cur
            if cur >= top + body_h:
                top = cur - body_h + 1
            if want < top and cur - want < body_h:
                top = want
            elif cur < top:
                top = cur
        ui["top"] = max(0, top)
        for y, k in enumerate(range(ui["top"], min(len(lines), ui["top"] + body_h))):
            rev = curses.A_REVERSE if k == cur else 0
            if rev:
                self.put(body_top + y, 0, " " * (w - 1), rev)
            for x, s, role in lines[k]:
                self.put(body_top + y, x + 1, s, self.attr.get(role, 0) | rev)
        if not idx:
            self.put(body_top, 1, "no accounts or providers yet (zorua add <name>, zorua provider add ...)")
        # Help on the bottom lines, the status line right above it, both below the last body row.
        for k, line in enumerate(foot):
            self.put(h - len(foot) + k, 1, line, curses.A_DIM)
        self.put(h - 1 - len(foot), 1, status, curses.A_DIM)
        self.scr.refresh()


def _glyphs(remaining):
    utf = locale.getpreferredencoding(False).lower().replace("-", "") == "utf8"
    return {"active": "●" if utf else "*", "auto": "◆" if utf else "+", "remaining": remaining}


def _esc_alone(scr):
    """After an ESC: True when nothing follows it (the Esc key). Otherwise it began an escape
    sequence curses did not recognise (e.g. Ctrl-Left under a terminfo without kLFT5); the rest
    of it is read and dropped, so it neither quits nor leaks into the shell afterwards."""
    scr.nodelay(True)
    try:
        alone = True
        while True:
            try:
                scr.get_wch()
            except curses.error:        # no more input waiting
                return alone
            alone = False
    finally:
        scr.nodelay(False)
        scr.timeout(150)


def _flush_input():
    """Drop typed-ahead keys so none reach the calling shell's command line."""
    try:
        curses.flushinp()
    except curses.error:
        pass


def _loop(scr, core, all_acc, provs, remaining, full):
    try:
        return _keys(scr, core, all_acc, provs, remaining, full)
    finally:
        _flush_input()


def _keys(scr, core, all_acc, provs, remaining, full):
    loader = Loader(core, all_acc)
    try:
        screen = Screen(scr)
        scr.keypad(True)
        scr.timeout(150)                # poll the loader between key presses
        loader.start()
        glyph = _glyphs(remaining)
        rows = build_rows(core, all_acc, None, None, provs, remaining, full)
        ui = initial_ui(rows)
        seen = None
        while True:
            info, live, error, ages, expired = loader.snapshot()
            if (id(info), id(live)) != seen:
                seen = (id(info), id(live))
                rows = build_rows(core, all_acc, info, live, provs, remaining, full, ages, expired)
            if loader.busy():
                status = "reading sign-ins…" if info is None else "fetching live usage…"
            else:
                status = error or ("live usage loaded; %s; r to refresh" % ("remaining share" if remaining else "used share"))
            screen.draw(rows, ui, status, glyph)
            try:
                key = scr.get_wch()
            except curses.error:       # timeout: nothing pressed
                continue
            if key in (ESC, "\x1b") and not _esc_alone(scr):
                continue
            ui, action = handle_key(ui, key, rows)
            if action == "refresh":
                loader.start()
            elif action and action != "redraw":     # a resize just needs the next draw
                return action, loader
    except KeyboardInterrupt:
        # Ctrl-C: quit without a change (curses.wrapper restores the tty). It can land anywhere in
        # the loop (often in refresh()), so catch it here: the caller then skips waiting on fetches.
        return "interrupt", loader


def run(core, full=False):
    """Show the TUI. -> (action, loader); action is "quit", "interrupt", "off" or ("use", name).
    curses.wrapper restores the terminal on any exception."""
    try:
        locale.setlocale(locale.LC_ALL, "")          # wide characters (●, emails) need it
    except locale.Error:
        pass
    os.environ.setdefault("ESCDELAY", "25")          # read by initscr: Esc quits without a 1 s lag
    all_acc = core.all_accounts()
    provs = core.zp.load(core.CONFIG_DIR)
    remaining = core.usage_mode() == "remaining"
    return curses.wrapper(_loop, core, all_acc, provs, remaining, full)


def term_problem():
    """-> an error message when TERM cannot draw a full screen (unknown, or no cursor addressing
    like TERM=dumb), else None. Checked before curses takes over the terminal."""
    term = os.environ.get("TERM", "")
    try:
        curses.setupterm(term or None, sys.stdout.fileno())
    except curses.error:
        return UNKNOWN_TERM % term
    if not curses.tigetstr("cup"):
        return NO_CURSOR % term
    return None


def available(stdin, stdout):
    """-> True when a full-screen UI can run on these streams."""
    try:
        return curses is not None and stdin.isatty() and stdout.isatty()
    except (AttributeError, ValueError):
        return False
