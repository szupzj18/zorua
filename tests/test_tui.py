"""zorua tui: row model, key handling, the non-TTY error and one real run under a pseudo-terminal.

HOME and the config directory are isolated temp dirs; no account is signed in, so nothing here
reaches the network or a real claude / codex login."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import zorua_tui as zt  # noqa: E402

try:
    import pty
    import fcntl
    import struct
    import termios
except ImportError:          # e.g. Windows
    pty = None


def jwt(email, plan="plus"):
    claims = {"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": plan}}
    return "x." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".x"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / "cfg"
        self.cfg.mkdir()
        env = patch.dict(os.environ, {"HOME": str(self.root), "ZORUA_CONFIG_DIR": str(self.cfg)})
        env.start()
        self.addCleanup(env.stop)
        for v in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "GROK_HOME", "ZORUA_CLAUDE_PROVIDER", "ZORUA_CODEX_PROVIDER",
                  "ZORUA_CLAUDE_MODEL", "ZORUA_CODEX_MODEL", "ZORUA_FULL_EMAIL", "ZORUA_AUTO_ACTIVE"):
            os.environ.pop(v, None)
        spec = importlib.util.spec_from_file_location("tui_test_core", ROOT / "zorua_core.py")
        self.c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.c)
        self.c.init_registry()
        self.c.write_tsv(self.c.ACCOUNT_FILE, [])

    def codex(self, name, email=None):
        home = self.root / (".codex-" + name)
        home.mkdir()
        if email:
            (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": jwt(email), "access_token": "fake-" + name}}))
        self.c.write_tsv(self.c.ACCOUNT_FILE, self.c.accounts() + [(name, str(home))])
        return home

    def claude(self, name):
        home = self.root / (".claude-" + name)
        home.mkdir()
        self.c.write_tsv(self.c.CLAUDE_FILE, self.c.claude_accounts() + [(name, str(home))])
        return home

    def providers(self):
        self.c.zp.save(str(self.cfg), {
            "relay": {"env": {"ANTHROPIC_BASE_URL": "https://relay.example.com", "ANTHROPIC_AUTH_TOKEN": "sk-test-relay",
                              "ANTHROPIC_MODEL": "glm-4.6"}, "models": {"glm": "glm-4.6", "kimi": "kimi-k2"}},
            "gw": {"kind": "codex", "base_url": "https://gw.example.com/v1", "key": "sk-test-gw", "model": "gpt-x"},
        })


class Rows(Base):
    def usage(self, short, weekly):
        return {"plan_type": "pro", "rate_limit": {
            "primary_window": {"used_percent": short, "limit_window_seconds": 18000, "reset_after_seconds": 3000},
            "secondary_window": {"used_percent": weekly, "limit_window_seconds": 604800, "reset_after_seconds": 90000}}}, None

    def build(self, info=None, live=None, remaining=False, env=None, full=False):
        return zt.build_rows(self.c, self.c.all_accounts(), info, live, self.c.zp.load(str(self.cfg)),
                             os.environ if env is None else env, remaining, full)

    def test_groups_accounts_and_providers_with_masked_email_and_usage(self):
        work = self.codex("work", "jonathan.smith@gmail.com")
        self.codex("spare")
        self.claude("alt")
        self.providers()
        info, _, _, _ = self.c.collect_usage([a for a in self.c.all_accounts() if a[2] == "codex"], False)
        info["alt"] = {"email": None, "plan": None, "until": None, "tok": {}, "state": "none"}
        rows = self.build(info, {"work": self.usage(30, 85)}, env={"CODEX_HOME": str(work), "ZORUA_CLAUDE_PROVIDER": "relay",
                                                                   "ZORUA_CLAUDE_MODEL": "kimi"})
        self.assertEqual([r.get("heading") or r["name"] for r in rows],
                         ["Codex", "work", "spare", "Claude Code", "alt", "Providers (Claude Code)", "relay", "Providers (Codex)", "gw"])
        w = rows[1]
        self.assertTrue(w["active"])
        self.assertEqual(w["email"], "jon***h@gmail.com")            # same mask as zorua ls
        self.assertEqual((w["state"], w["plan"]), ("ok", "pro"))
        self.assertEqual([(x["label"], x["shown"], x["reset"]) for x in w["windows"]], [("5H", 30, "50m"), ("7D", 85, "1d1h")])
        self.assertEqual((rows[2]["state"], rows[2]["active"], rows[2]["usage"]), ("signed out", False, "–"))
        self.assertEqual(rows[4]["state"], "signed out")
        relay, gw = rows[6], rows[8]
        self.assertTrue(relay["active"])
        self.assertEqual((relay["endpoint"], relay["model"]), ("relay.example.com", "model kimi (this shell)"))
        self.assertEqual((gw["active"], gw["endpoint"], gw["model"]), (False, "gw.example.com", "model gpt-x (default)"))
        self.assertNotIn("sk-test", json.dumps(rows))                  # never a provider key

    def test_remaining_mode_full_emails_and_loading_placeholders(self):
        self.codex("work", "jonathan.smith@gmail.com")
        loading = self.build()
        self.assertEqual((loading[1]["state"], loading[1]["usage"], loading[1]["email"]), ("…", "…", ""))
        info, _, _, _ = self.c.collect_usage(self.c.all_accounts(), False)
        rows = self.build(info, {"work": self.usage(30, 85)}, remaining=True, full=True)
        self.assertEqual([x["shown"] for x in rows[1]["windows"]], [70, 15])
        self.assertEqual(rows[1]["email"], "jonathan.smith@gmail.com")
        rows = self.build(info, {"work": (None, "token expired (run codex once to refresh)")})
        self.assertEqual(rows[1]["usage"], "token expired (run codex once to refresh)")

    def test_default_account_is_active_when_codex_home_is_unset(self):
        self.c.write_tsv(self.c.ACCOUNT_FILE, [("default", str(self.root / ".codex"))])
        self.codex("work")
        rows = self.build(env={})
        self.assertEqual([r["name"] for r in rows if r.get("active")], ["default"])
        self.assertEqual(zt.initial_ui(rows)["sel"], 0)
        rows = self.build(env={"CODEX_HOME": str(self.root / ".codex-work")})
        self.assertEqual(zt.initial_ui(rows)["sel"], 1)

    def test_layout_aligns_columns(self):
        self.codex("a", "a@example.com")
        self.codex("longer-name")
        info, _, _, _ = self.c.collect_usage(self.c.all_accounts(), False)
        lines = zt.layout(self.build(info, {}), {"active": "*", "auto": "+", "remaining": False})
        self.assertEqual(lines[1][2][0], lines[2][2][0])               # the state column starts at the same x

    def test_layout_sizes_columns_by_display_width(self):
        self.codex("a", "张三丰@例子.com")
        self.codex("b", "bob@example.com")
        info, _, _, _ = self.c.collect_usage(self.c.all_accounts(), False)
        lines = zt.layout(self.build(info, {}), {"active": "*", "auto": "+", "remaining": False})
        self.assertEqual(zt.width("张***@例子.com"), 14)
        for line in lines[1:]:
            for (x, s, _), (nx, _, _) in zip(line, line[1:]):
                self.assertGreaterEqual(nx, x + zt.width(s), line)    # no cell runs into the next one
        self.assertEqual(lines[1][3][0], lines[2][3][0])


class FakeScr:
    """Just enough of a curses window: a grid of cells; writing past the right edge fails the test."""

    def __init__(self, h, w):
        self.h, self.w = h, w
        self.erase()

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.grid = [[" "] * self.w for _ in range(self.h)]
        self.rev = set()

    def addstr(self, y, x, text, attr=0):
        for ch in text:
            cw = zt.width(ch)
            if x + cw > self.w:
                raise AssertionError("text wraps past the right edge: %r at row %d" % (text, y))
            self.grid[y][x] = ch
            if cw == 2:
                self.grid[y][x + 1] = ""
            x += cw
        if attr & zt.curses.A_REVERSE:
            self.rev.add(y)

    def refresh(self):
        pass

    def row(self, y):
        return "".join(self.grid[y]).rstrip()


@unittest.skipIf(zt.curses is None, "no curses")
class Drawing(Base):
    def screen(self, h, w):
        sc = zt.Screen.__new__(zt.Screen)
        sc.scr = FakeScr(h, w)
        sc.attr = {}
        return sc

    def test_put_clips_wide_text_by_columns(self):
        sc = self.screen(6, 12)
        sc.put(1, 0, "张三丰张三丰张三丰")
        self.assertEqual(sc.scr.row(1), "张三丰张三")                # 10 of the 11 usable columns
        self.assertEqual(sc.scr.row(2), "")                           # nothing wrapped onto the next row
        sc.put(3, 9, "张三")                                           # 2 columns left: one wide character
        self.assertEqual(sc.scr.row(3).strip(), "张")

    def test_help_and_status_do_not_cover_the_selected_row(self):
        for i in range(20):
            self.codex("acct%d" % i)
        rows = zt.build_rows(self.c, self.c.all_accounts(), None, None, {}, {})
        sc = self.screen(12, 70)
        sc.draw(rows, {"sel": 19, "help": True}, "STATUS-LINE", {"active": "*", "auto": "+", "remaining": False})
        h, foot = 12, len(zt.HELP_LINES)
        last_body = 2 + (h - 3 - foot) - 1
        self.assertIn("acct19", sc.scr.row(last_body))
        self.assertIn(last_body, sc.scr.rev)
        self.assertEqual(sc.scr.row(h - 1 - foot).strip(), "STATUS-LINE")
        self.assertEqual([sc.scr.row(y).strip() for y in range(h - foot, h)], [l[:68] for l in zt.HELP_LINES])
        sc.draw(rows, {"sel": 19, "help": False}, "STATUS-LINE", {"active": "*", "auto": "+", "remaining": False})
        self.assertEqual(sc.scr.row(h - 1).strip(), "STATUS-LINE")
        self.assertIn("acct19", sc.scr.row(h - 2))


class Interrupt(Base):
    """Ctrl-C anywhere in the loop (not only while waiting for a key) returns "interrupt"."""

    def run_loop(self, draw=None, get_wch=None):
        class Scr:
            def keypad(self, _):
                pass

            def timeout(self, _):
                pass

        scr = Scr()
        scr.get_wch = get_wch or (lambda: "q")

        class Stub:
            def __init__(self, _):
                pass

            def draw(self, *a):
                if draw:
                    draw()

        with patch.object(zt, "Screen", Stub):
            action, loader = zt._loop(scr, self.c, [], {}, False, False)
        if loader.thread:
            loader.thread.join(5)
        return action

    def test_in_draw_and_in_get_wch(self):
        def ki():
            raise KeyboardInterrupt
        self.assertEqual(self.run_loop(draw=ki), "interrupt")
        self.assertEqual(self.run_loop(get_wch=ki), "interrupt")
        self.assertEqual(self.run_loop(), "quit")


class EscapeSequences(Base):
    """An ESC that starts a sequence curses did not decode is dropped whole; a lone ESC quits."""

    def setUp(self):
        super().setUp()
        self.codex("one")
        self.codex("two")

    def run_loop(self, keys):
        """keys: what get_wch returns in turn; None means no input is waiting (curses.error)."""
        keys = list(keys)

        class Scr:
            def keypad(self, _):
                pass

            def timeout(self, _):
                pass

            def nodelay(self, _):
                pass

            def get_wch(self):
                k = keys.pop(0) if keys else "q"
                if k is None:
                    raise zt.curses.error("no input")
                return k

        class Stub:
            def __init__(self, _):
                pass

            def draw(self, rows, ui, *a):
                seen.append(ui["sel"])

        seen = []
        flushed = []
        with patch.object(zt, "Screen", Stub), patch.object(zt.curses, "flushinp", lambda: flushed.append(1)):
            action, loader = zt._loop(Scr(), self.c, self.c.all_accounts(), {}, False, False)
        if loader.thread:
            loader.thread.join(5)
        self.assertEqual(flushed, [1], "typed-ahead input was not flushed")
        return action, seen[-1], keys

    def test_unknown_sequence_is_ignored(self):
        for seq in ("[1;5D", "[99~", "[1;2A"):          # Ctrl-Left, an unknown key, Shift-Up
            with self.subTest(seq=seq):
                action, sel, rest = self.run_loop(["\x1b"] + list(seq) + [None, "j", "\n"])
                self.assertEqual(action, ("use", "two"))
                self.assertEqual(rest, [])

    def test_lone_escape_quits(self):
        for esc in ("\x1b", zt.ESC):
            action, _, rest = self.run_loop([esc, None, "j"])
            self.assertEqual(action, "quit")
            self.assertEqual(rest, ["j"])


@unittest.skipIf(zt.curses is None, "no curses")
class NoColor(Base):
    def test_no_color_skips_colour_pairs(self):
        for env, want in (({"NO_COLOR": "1"}, False), ({}, True)):
            started = []
            fakes = {"curs_set": lambda *_: None, "has_colors": lambda: True,
                     "start_color": lambda: started.append(1), "use_default_colors": lambda: None,
                     "init_pair": lambda *_: None, "color_pair": lambda n: n << 8}
            with self.subTest(env=env), patch.dict(os.environ, env), contextlib.ExitStack() as stack:
                if not env:
                    os.environ.pop("NO_COLOR", None)
                for name, fn in fakes.items():
                    stack.enter_context(patch.object(zt.curses, name, fn))
                sc = zt.Screen(None)
                self.assertEqual(bool(started), want)
                self.assertEqual(sc.attr["red"] != 0, want)
                self.assertNotEqual(sc.attr["dim"], 0)


class Keys(Base):
    def rows(self):
        self.codex("one")
        self.codex("two")
        self.providers()
        return zt.build_rows(self.c, self.c.all_accounts(), None, None, self.c.zp.load(str(self.cfg)), {})

    def test_movement_clamps_and_skips_headings(self):
        rows = self.rows()                    # Codex, one, two, Providers (Claude Code), relay, Providers (Codex), gw
        ui = zt.initial_ui(rows)
        for key, sel in (("k", 0), ("j", 1), (zt.KEY_DOWN, 2), ("j", 3), ("j", 3), (zt.KEY_UP, 2), ("g", 0), ("G", 3)):
            ui, action = zt.handle_key(ui, key, rows)
            self.assertIsNone(action)
            self.assertEqual(ui["sel"], sel, key)
        self.assertEqual(rows[zt.entries(rows)[ui["sel"]]]["name"], "gw")

    def test_actions(self):
        rows = self.rows()
        ui = zt.initial_ui(rows)
        ui, _ = zt.handle_key(ui, "j", rows)
        for key in ("\n", "\r", 10, zt.KEY_ENTER):
            self.assertEqual(zt.handle_key(ui, key, rows)[1], ("use", "two"))
        self.assertEqual(zt.handle_key(ui, "o", rows)[1], "off")
        self.assertEqual(zt.handle_key(ui, "r", rows)[1], "refresh")
        self.assertEqual(zt.handle_key(ui, "q", rows)[1], "quit")
        self.assertEqual(zt.handle_key(ui, "\x1b", rows)[1], "quit")
        self.assertEqual(zt.handle_key(ui, zt.KEY_RESIZE, rows)[1], "redraw")
        shown, _ = zt.handle_key(ui, "?", rows)
        self.assertTrue(shown["help"])
        self.assertFalse(zt.handle_key(shown, "?", rows)[0]["help"])
        self.assertEqual(zt.handle_key(ui, "x", rows), (ui, None))
        self.assertFalse(ui is zt.handle_key(ui, "j", rows)[0])        # never mutates its input

    def test_empty_list(self):
        ui = zt.initial_ui([])
        self.assertEqual(zt.handle_key(ui, "j", [])[0]["sel"], 0)
        self.assertIsNone(zt.handle_key(ui, "\n", [])[1])
        self.assertEqual(zt.handle_key(ui, "q", [])[1], "quit")


class NotATerminal(Base):
    def test_error_and_exit_1(self):
        self.codex("work")
        out, errs = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
            rc = self.c.main(["tui"])
        self.assertEqual(rc, 1)
        self.assertIn("needs an interactive terminal", errs.getvalue())
        self.assertIn("zorua ls", errs.getvalue())
        self.assertIn("zorua use <name>", errs.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_subprocess_with_pipes(self):
        env = dict(os.environ, ZORUA_EVAL_FILE=str(self.root / "eval"))
        r = subprocess.run([sys.executable, str(ROOT / "zorua_core.py"), "tui"], env=env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("zorua ls", r.stderr)
        self.assertFalse((self.root / "eval").exists())

    @unittest.skipIf(pty is None or not hasattr(os, "fork"), "pty not available")
    def test_unknown_term_names_the_terminal_type(self):
        env = dict(os.environ, TERM="zorua-no-such-term", ZORUA_EVAL_FILE=str(self.root / "eval"))
        env.pop("TERMINFO", None)
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.execve(sys.executable, [sys.executable, str(ROOT / "zorua_core.py"), "tui"], env)
            finally:
                os._exit(127)
        out, deadline = b"", time.time() + 20
        while time.time() < deadline:
            if not select.select([fd], [], [], 0.2)[0]:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        _, st = os.waitpid(pid, 0)
        os.close(fd)
        self.assertEqual(os.WEXITSTATUS(st), 1, out)
        self.assertIn(b"terminal type 'zorua-no-such-term' is unknown here", out)
        self.assertIn(b"TERM=xterm-256color zorua tui", out)
        self.assertNotIn(b"must be a TTY", out)

    def test_bad_option(self):
        with contextlib.redirect_stderr(io.StringIO()) as e:
            self.assertEqual(self.c.main(["tui", "--nope"]), 1)
        self.assertIn("usage: zorua tui", e.getvalue())

    def test_reserved(self):
        self.assertIn("tui", self.c.RESERVED)
        self.assertFalse(self.c.valid_name("tui"))


@unittest.skipIf(pty is None or not hasattr(os, "fork"), "pty not available")
class EndToEnd(Base):
    """Run the real core under a pseudo-terminal, press j then Enter, compare with `zorua use`."""

    def run_tui(self, keys, eval_file, size=(24, 100), ready=(b"Enter use", b"two"), resize=None, extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ZORUA_AUTO", "ZORUA_PRE"))}
        env.update(TERM="xterm", ZORUA_SHELL="zsh", ZORUA_EVAL_FILE=str(eval_file), LANG="en_US.UTF-8")
        env.update(extra_env or {})
        env.pop("CODEX_HOME", None)
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.execve(sys.executable, [sys.executable, str(ROOT / "zorua_core.py"), "tui"], env)
            finally:
                os._exit(127)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))
        out, sent, deadline = b"", False, time.time() + 20
        status = None
        try:
            while time.time() < deadline:
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:          # EIO: the child closed the terminal
                        chunk = b""
                    if not chunk:
                        break
                    out += chunk
                if not sent and all(m in out for m in ready):
                    if resize:
                        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", resize[0], resize[1], 0, 0))
                        os.kill(pid, signal.SIGWINCH)
                        time.sleep(0.3)
                    for k in keys:
                        os.write(fd, k)
                        time.sleep(0.2)
                    sent = True
                done, st = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = st
                    while select.select([fd], [], [], 0.2)[0]:     # what it wrote just before exiting
                        try:
                            chunk = os.read(fd, 65536)
                        except OSError:
                            break
                        if not chunk:
                            break
                        out += chunk
                    break
        finally:
            end = time.time() + 5             # EOF on the terminal comes just before the exit
            while status is None and time.time() < end:
                done, st = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = st
                else:
                    time.sleep(0.05)
            if status is None:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            os.close(fd)
        self.assertTrue(sent, "TUI never drew its list:\n%r" % out[-2000:])
        self.assertIsNotNone(status, "TUI did not exit:\n%r" % out[-2000:])
        return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status >> 8, out

    def cli_eval(self, *args):
        """-> the eval file `zorua <args>` writes for the calling shell."""
        eval_file = self.root / ("cli-%s.eval" % "-".join(args))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ZORUA_AUTO", "ZORUA_PRE"))}
        env.update(ZORUA_SHELL="zsh", ZORUA_EVAL_FILE=str(eval_file))
        env.pop("CODEX_HOME", None)
        subprocess.run([sys.executable, str(ROOT / "zorua_core.py")] + list(args), env=env, check=True,
                       capture_output=True, timeout=30)
        return eval_file.read_text()

    def slow_claude(self):
        """A claude account whose `claude auth status` takes 30 s (and ignores Ctrl-C), so the
        background loader is still running when a key is pressed."""
        bin_dir = self.root / "slow-bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text("#!/bin/sh\ntrap '' INT\nsleep 30\necho '{\"loggedIn\":false}'\n")
        fake.chmod(0o755)
        self.claude("slow")
        return {"PATH": "%s%s%s" % (bin_dir, os.pathsep, os.environ.get("PATH", ""))}

    def test_move_and_enter_writes_the_same_export_as_use(self):
        self.codex("one")
        self.codex("two")
        tui_eval = self.root / "tui.eval"
        rc, out = self.run_tui([b"j", b"\r"], tui_eval)
        self.assertEqual(rc, 0, out[-2000:])
        self.assertIn(b"this shell -> codex account: two", out)
        self.assertIn("export CODEX_HOME=%s" % (self.root / ".codex-two"), tui_eval.read_text())
        self.assertEqual(tui_eval.read_text(), self.cli_eval("use", "two"))

    def test_keys_while_usage_is_still_loading_exit_at_once(self):
        self.codex("one")
        self.codex("two")
        slow = self.slow_claude()
        cases = (([b"j", b"\r"], 0, ("use", "two")), ([b"o"], 0, ("off",)), ([b"\x03"], 130, None))
        for keys, want_rc, cli in cases:
            with self.subTest(keys=keys):
                tui_eval = self.root / "tui.eval"
                if tui_eval.exists():
                    tui_eval.unlink()
                start = time.time()
                rc, out = self.run_tui(keys, tui_eval, ready=(b"Enter use", b"two", b"reading sign-ins"),
                                       extra_env=slow)
                self.assertLess(time.time() - start, 10, "waited for the pending fetch")
                self.assertEqual(rc, want_rc, out[-2000:])
                if cli:
                    self.assertEqual(tui_eval.read_text(), self.cli_eval(*cli))
                else:
                    self.assertFalse(tui_eval.exists() and tui_eval.read_text().strip())

    def test_tiny_terminal_and_resize_do_not_crash(self):
        self.codex("one")
        self.codex("two")
        tui_eval = self.root / "tui.eval"
        rc, out = self.run_tui([b"j", b"?", b"k", b"\r"], tui_eval, size=(4, 12), ready=(b"one",), resize=(2, 5))
        self.assertEqual(rc, 0, out[-2000:])
        self.assertNotIn(b"Traceback", out)
        self.assertIn("export CODEX_HOME=%s" % (self.root / ".codex-one"), tui_eval.read_text())

    def test_unrecognised_escape_sequence_does_not_quit(self):
        # vt100's terminfo has no modified arrow keys, so curses hands back ESC and then the rest
        # (as tmux-256color / screen-256color do on macOS for Ctrl/Shift+arrows).
        self.codex("one")
        self.codex("two")
        tui_eval = self.root / "tui.eval"
        rc, out = self.run_tui([b"\x1b[1;5D", b"\x1b[1;2A", b"j", b"\r"], tui_eval, extra_env={"TERM": "vt100"})
        self.assertEqual(rc, 0, out[-2000:])
        self.assertEqual(tui_eval.read_text(), self.cli_eval("use", "two"))
        rc, _ = self.run_tui([b"\x1b"], tui_eval.with_name("esc.eval"), extra_env={"TERM": "vt100"})
        self.assertEqual(rc, 0)
        self.assertFalse(tui_eval.with_name("esc.eval").exists())

    def test_quit_changes_nothing(self):
        self.codex("one")
        self.codex("two")
        tui_eval = self.root / "tui.eval"
        rc, _ = self.run_tui([b"j", b"q"], tui_eval)
        self.assertEqual(rc, 0)
        self.assertFalse(tui_eval.exists())


class Loading(Base):
    def test_claude_sign_in_is_read_once_per_load(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        log = self.root / "claude.log"
        fake = bin_dir / "claude"
        fake.write_text("#!/bin/sh\necho \"$*\" >> '%s'\necho '{\"loggedIn\":false}'\n" % log)
        fake.chmod(0o755)
        self.claude("c1")
        loader = zt.Loader(self.c, self.c.all_accounts())
        with patch.dict(os.environ, {"PATH": "%s%s%s" % (bin_dir, os.pathsep, os.environ.get("PATH", ""))}):
            for want in (1, 2):                   # first load, then one `r` refresh
                self.assertTrue(loader.start())
                loader.thread.join(30)
                self.assertEqual(log.read_text().count("auth status"), want)
        info, live, error = loader.snapshot()
        self.assertEqual((info["c1"]["state"], error), ("none", ""))
        self.assertIsNotNone(live)

    def test_window_labels_and_reset_match_zorua_usage(self):
        data = {"rate_limit": {"primary_window": {"limit_window_seconds": 604800, "used_percent": 10,
                                                  "reset_after_seconds": 3 * 86400 + 4 * 3600},
                               "secondary_window": {"limit_window_seconds": 18000, "used_percent": 60,
                                                    "reset_after_seconds": 7 * 60}}}
        self.assertEqual([(w["label"], w["reset"]) for w in zt._windows(self.c, data, False)],
                         [("5H", "7m"), ("7D", "3d4h")])


class PinnedOldRelease(Base):
    """install.sh from main with ZORUA_REF set to a release that has no zorua_tui.py."""

    def install(self, missing):
        dl, bin_dir = self.root / "dl", self.root / "bin"
        dl.mkdir()
        bin_dir.mkdir()
        (dl / "install.sh").write_text((ROOT / "install.sh").read_text())     # no clone next to it: downloads
        curl = bin_dir / "curl"
        curl.write_text("""#!/bin/sh
# fake curl: serves files from the checkout, 404 (curl -f exits 22) for $FAKE_MISSING
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift 2 ;; -*) shift ;; *) url="$1"; shift ;; esac
done
f=${url##*/}
case " $FAKE_MISSING " in *" $f "*) exit 22 ;; esac
cp "$FAKE_SRC/$f" "$out"
""")
        curl.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k not in ("ZDOTDIR", "CX_HOME", "ZORUA_HOME")}
        env.update(HOME=str(self.root), ZORUA_HOME=str(self.root / "inst"), ZORUA_REF="v0.7.1",
                   XDG_CONFIG_HOME=str(self.root / "xdg"), SHELL="/bin/sh", FAKE_SRC=str(ROOT), FAKE_MISSING=missing,
                   PATH="%s%s%s" % (bin_dir, os.pathsep, os.environ.get("PATH", "")))
        return subprocess.run(["sh", str(dl / "install.sh")], env=env, capture_output=True, text=True, timeout=60)

    def test_missing_tui_is_skipped(self):
        r = self.install("zorua_tui.py")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("skipped zorua_tui.py", r.stdout)
        self.assertTrue((self.root / "inst" / "zorua_core.py").exists())
        self.assertFalse((self.root / "inst" / "zorua_tui.py").exists())
        self.assertFalse((self.root / "inst" / "zorua_tui.py.part").exists())

    def test_missing_required_file_still_fails(self):
        r = self.install("zorua_providers.py")
        self.assertEqual(r.returncode, 1)
        self.assertIn("download failed", r.stderr)


if __name__ == "__main__":
    unittest.main()
