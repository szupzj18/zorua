#!/usr/bin/env python3
"""Zorua core: shell-independent logic for the `zorua` command.

The per-shell wrappers (zorua.zsh / .bash / .fish) are thin: they run
this program and then `source` the statements it wrote to $ZORUA_EVAL_FILE
(CODEX_HOME changes, auto-binding state, prompt marker). Everything else
(registry, bindings, rendering, login, setup wizard) lives here.

Stdlib only, Python 3.8+.
"""
import base64
import copy
import hashlib
import shutil
import stat
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))   # sibling module, also under python3 -I
import zorua_providers as zp  # noqa: E402

VERSION = "0.7.1"
HOME = os.path.expanduser("~")
# CX_* are the pre-rename names of the user-facing variables; still honoured.
CONFIG_DIR = os.environ.get("ZORUA_CONFIG_DIR") or os.environ.get("CX_CONFIG_DIR") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "zorua")
ACCOUNT_FILE = os.path.join(CONFIG_DIR, "accounts.tsv")
CLAUDE_SETTINGS_TEMPLATE = os.path.join(CONFIG_DIR, "claude-settings.json")
BINDING_FILE = os.path.join(CONFIG_DIR, "bindings.tsv")
CLAUDE_FILE = os.path.join(CONFIG_DIR, "claude-accounts.tsv")
SHELL = os.environ.get("ZORUA_SHELL", "zsh")
RESERVED = {"doctor", "tag", "pick", "provider", "launch", "model", "ls", "list", "status", "usage", "setup", "use", "login", "off", "reset",
            "unset", "help", "add", "rm", "bind", "unbind", "binds", "version",
            "prompt", "apply", "names", "hook", "-h", "--help", "-v", "--version"}


# --------------------------------------------------------------------------
# Talking back to the calling shell
# --------------------------------------------------------------------------

class Emitter:
    """Collects shell statements for the wrapper to source after we exit."""

    def __init__(self):
        self.lines = []

    @staticmethod
    def _q(v):
        if SHELL == "fish":
            return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
        return shlex.quote(v)

    def export(self, name, value):
        if SHELL == "fish":
            self.lines.append("set -gx %s %s" % (name, self._q(value)))
        else:
            self.lines.append("export %s=%s" % (name, self._q(value)))

    def setvar(self, name, value):
        if SHELL == "fish":
            self.lines.append("set -g %s %s" % (name, self._q(value)))
        else:
            self.lines.append("%s=%s" % (name, self._q(value)))

    def unset(self, name):
        self.lines.append("set -e %s" % name if SHELL == "fish" else "unset %s" % name)

    def flush(self):
        path = os.environ.get("ZORUA_EVAL_FILE")
        if path and self.lines:
            zp._write_private(path, "\n".join(self.lines) + "\n")


EMIT = Emitter()


def err(msg):
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------
# Registry + bindings (plain TSV files, same format as the old zsh version)
# --------------------------------------------------------------------------

def valid_name(n):
    import re
    return bool(re.match(r"^[A-Za-z0-9_-]+$", n)) and n not in RESERVED


def read_tsv(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip() or line.startswith("#"):
                    continue
                a, _, b = line.partition("\t")
                rows.append((a, b))
    except FileNotFoundError:
        pass
    return rows


def write_tsv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for a, b in rows:
            f.write("%s\t%s\n" % (a, b))
    os.replace(tmp, path)


LEGACY_CONFIG_DIR = os.path.join(os.path.dirname(CONFIG_DIR), "codex-switch")


def migrate_legacy_config():
    """Zorua was called codex-switch: copy its registry/bindings once (the old files stay as a backup)."""
    if os.path.exists(ACCOUNT_FILE) or os.environ.get("ZORUA_CONFIG_DIR") or os.environ.get("CX_CONFIG_DIR"):
        return
    legacy = os.path.join(LEGACY_CONFIG_DIR, "accounts.tsv")
    if not os.path.exists(legacy):
        return
    import shutil
    os.makedirs(CONFIG_DIR, exist_ok=True)
    for fn in ("accounts.tsv", "claude-accounts.tsv", "bindings.tsv"):
        src = os.path.join(LEGACY_CONFIG_DIR, fn)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(CONFIG_DIR, fn))


def init_registry():
    """First run: seed `default` and adopt ~/.codex-* homes that hold a sign-in."""
    migrate_legacy_config()
    if os.path.exists(ACCOUNT_FILE):
        return
    rows = [("default", os.path.join(HOME, ".codex"))]
    try:
        entries = sorted(os.listdir(HOME))
    except OSError:
        entries = []
    for e in entries:
        if not e.startswith(".codex-"):
            continue
        d = os.path.join(HOME, e)
        name = e[len(".codex-"):]
        if os.path.isfile(os.path.join(d, "auth.json")) and valid_name(name) and name != "default":
            rows.append((name, d))
    write_tsv(ACCOUNT_FILE, rows)


def accounts():
    """-> ordered list of Codex (name, home)"""
    return read_tsv(ACCOUNT_FILE)


def homes():
    return dict(accounts())


# Claude Code accounts live in their own registry file (phase 1: subscription
# logins, isolated through CLAUDE_CONFIG_DIR). Names are unique across both.
def claude_accounts():
    return read_tsv(CLAUDE_FILE)


def registry(kind):
    if is_provider(kind):
        return {n: n for n, p in zp.load(CONFIG_DIR).items() if zp.agent(p) == PROVIDER_AGENT[kind]}
    return dict(claude_accounts() if kind == "claude" else accounts())


def kind_of(name):
    if name in homes():
        return "codex"
    if name in dict(claude_accounts()):
        return "claude"
    p = zp.load(CONFIG_DIR).get(name)
    if p is not None:
        return AGENT_PROVIDER[zp.agent(p)]
    return None


def all_names():
    return [n for n, _, _ in all_accounts()] + list(zp.load(CONFIG_DIR))


def all_accounts():
    """-> [(name, home, kind)] Codex first, then Claude."""
    return [(n, h, "codex") for n, h in accounts()] + [(n, h, "claude") for n, h in claude_accounts()]


# For the provider kinds the "home" is the provider name itself (read by the `claude` / `codex`
# shell functions). Each agent has its own provider slot, so one shell can hold one of each.
VARS = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR",
        "claude_provider": "ZORUA_CLAUDE_PROVIDER", "codex_provider": "ZORUA_CODEX_PROVIDER"}
KINDS = tuple(VARS)
PROVIDER_AGENT = {"claude_provider": "claude", "codex_provider": "codex"}
# The model picked inside the active provider of each agent (an alias from the provider's catalog).
MODEL_VAR = {"claude_provider": "ZORUA_CLAUDE_MODEL", "codex_provider": "ZORUA_CODEX_MODEL"}
AGENT_PROVIDER = {a: k for k, a in PROVIDER_AGENT.items()}
PROVIDER_KEY_VAR = {"claude_provider": "ZORUA_CLAUDE_KEY", "codex_provider": zp.CODEX_KEY_VAR}


def is_provider(kind):
    return kind in PROVIDER_AGENT
# kind -> (env var the wrapper passes in, shell variable we set, env var for the pre-auto home, shell var for it)
AUTO_VARS = {
    "codex": ("ZORUA_AUTO_ACTIVE", "ZORUA_AUTO_ACTIVE", "ZORUA_PRE_AUTO_HOME", "_ZORUA_PRE_AUTO_HOME"),
    "claude": ("ZORUA_AUTO_CLAUDE", "ZORUA_AUTO_CLAUDE", "ZORUA_PRE_AUTO_CLAUDE", "_ZORUA_PRE_AUTO_CLAUDE"),
    "claude_provider": ("ZORUA_AUTO_CLAUDE_PROVIDER", "ZORUA_AUTO_CLAUDE_PROVIDER",
                        "ZORUA_PRE_AUTO_CLAUDE_PROVIDER", "_ZORUA_PRE_AUTO_CLAUDE_PROVIDER"),
    "codex_provider": ("ZORUA_AUTO_CODEX_PROVIDER", "ZORUA_AUTO_CODEX_PROVIDER",
                       "ZORUA_PRE_AUTO_CODEX_PROVIDER", "_ZORUA_PRE_AUTO_CODEX_PROVIDER"),
}

# Variables that outrank (or redirect) a Claude subscription login. See
# https://code.claude.com/docs/en/authentication#authentication-precedence
CLAUDE_OVERRIDES = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
OFFICIAL_URL = "https://api.anthropic.com"


def claude_clean_env(home):
    """Environment for running `claude` on a subscription account in `home`.

    Strips every inherited provider/auth override so the account's own login is
    used. A set ANTHROPIC_BASE_URL is pinned to the official endpoint rather
    than removed: with a local proxy configured, merely unsetting it can still
    route requests through the proxy.
    """
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_USE_")
                   or k.startswith("CLAUDE_CODE_GATEWAY_") or k == "CLAUDE_CODE_OAUTH_TOKEN")}
    if "ANTHROPIC_BASE_URL" in os.environ:
        env["ANTHROPIC_BASE_URL"] = OFFICIAL_URL
    env["CLAUDE_CONFIG_DIR"] = home
    return env


def claude_usage_cache(home):
    """-> dict from the status-line relay's cache (see zorua_statusline.py) or None."""
    for fn in (".zorua-usage.json", ".cx-usage.json"):       # second: written by 0.4.0 relays
        try:
            with open(os.path.join(home, fn)) as f:
                return json.load(f)
        except Exception:
            continue
    return None


def claude_status(home):
    """-> dict from `claude auth status` for the account in `home`, or {"error": ...}."""
    try:
        r = subprocess.run(["claude", "auth", "status", "--json"], env=claude_clean_env(home),
                           capture_output=True, text=True, timeout=20)
        return json.loads(r.stdout)
    except FileNotFoundError:
        return {"error": "claude not found on PATH"}
    except Exception as e:
        return {"error": "claude auth status failed: %s" % e}


def cwd():
    """Logical working directory ($PWD, as the shell shows it) when it is still accurate."""
    p = os.environ.get("PWD", "")
    try:
        if p and os.path.samefile(p, os.getcwd()):
            return p
    except OSError:
        pass
    return os.getcwd()


def short(path):
    return "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + "/") else path


def current_account(kind="codex"):
    """Name of the account active in this shell for `kind`; 'default'/None when unset, 'custom' if unregistered."""
    ch = os.environ.get(VARS[kind], "")
    if not ch:
        return "default" if kind == "codex" else None
    for n, h in registry(kind).items():
        if h == ch:
            return n
    return "custom"


def binding_for(d, kind="codex"):
    best_p, best_n = "", ""
    reg = registry(kind)
    for n, p in read_tsv(BINDING_FILE):
        if n in reg and (d == p or d.startswith(p.rstrip("/") + "/")) and len(p) > len(best_p):
            best_p, best_n = p, n
    return best_n


# --------------------------------------------------------------------------
# Shell state: CODEX_HOME / CLAUDE_CONFIG_DIR + auto-binding + prompt marker
# --------------------------------------------------------------------------

class State:
    """Tracks the shell's account variables / auto-binding as this process changes them."""

    def __init__(self):
        self.home = {k: os.environ.get(v, "") for k, v in VARS.items()}
        self.auto = {k: os.environ.get(AUTO_VARS[k][0], "") for k in VARS}
        self.pre = {k: os.environ.get(AUTO_VARS[k][2], "") for k in VARS}
        self.model = {k: os.environ.get(v, "") for k, v in MODEL_VAR.items()}

    def set_home(self, kind, home):
        if kind in PROVIDER_KEY_VAR:
            if home:
                prov = zp.resolve_key(home, zp.load(CONFIG_DIR)[home])
                EMIT.export(PROVIDER_KEY_VAR[kind], zp.secret(prov))
            else:
                EMIT.unset(PROVIDER_KEY_VAR[kind])
        self.home[kind] = home or ""
        if home:
            EMIT.export(VARS[kind], home)
        else:
            EMIT.unset(VARS[kind])
        if kind in MODEL_VAR:                 # a different provider starts without a model pick
            self.set_model(kind, "")

    def set_model(self, kind, alias):
        self.model[kind] = alias
        if alias:
            EMIT.export(MODEL_VAR[kind], alias)
        else:
            EMIT.unset(MODEL_VAR[kind])

    def set_auto(self, kind, auto, pre):
        self.auto[kind], self.pre[kind] = auto, pre
        EMIT.setvar(AUTO_VARS[kind][1], auto)
        EMIT.setvar(AUTO_VARS[kind][3], pre)

    def prompt_info(self):
        """-> (kind, name, text): kind is ''|manual|auto|custom."""
        parts, kinds, first = [], set(), ""
        for kind in KINDS:
            home = self.home[kind]
            if not home:
                continue
            reg = registry(kind)
            name = next((n for n, p in reg.items() if p == home), "")
            first = first or name
            if not name:
                kinds.add("custom")
                parts.append("%s:custom" % kind.replace("_", "-"))
            elif self.auto[kind] and reg.get(self.auto[kind]) == home:
                kinds.add("auto")
                parts.append("%s:%s:auto" % (kind.replace("_", "-"), name))
            else:
                kinds.add("manual")
                parts.append("%s:%s%s" % (kind.replace("_", "-"), name,
                                          "/" + self.model[kind] if self.model.get(kind) else ""))
        kind = "auto" if "auto" in kinds else "custom" if "custom" in kinds else "manual" if kinds else ""
        return kind, first, ("[%s]" % " ".join(parts)) if parts else ""

    def prompt(self):
        """Emit marker variables the wrappers use for the prompt."""
        kind, name, text = self.prompt_info()
        EMIT.setvar("ZORUA_PROMPT_KIND", kind)
        EMIT.setvar("ZORUA_PROMPT_NAME", name)
        EMIT.setvar("ZORUA_PROMPT_TEXT", text)


def apply_binding(st, pwd):
    for kind in KINDS:
        bound = binding_for(pwd, kind)
        target = registry(kind).get(bound) if bound else None
        if bound and target:
            if st.auto[kind] != bound:
                pre = st.home[kind] if not st.auto[kind] else st.pre[kind]
                st.set_home(kind, target)
                st.set_auto(kind, bound, pre)
        elif st.auto[kind]:
            st.set_home(kind, st.pre[kind])
            st.set_auto(kind, "", "")
    st.prompt()


# --------------------------------------------------------------------------
# Rendering (account table / usage)
# --------------------------------------------------------------------------

TITLES = {"codex": "Codex", "claude": "Claude Code"}


def emit_json(all_acc, info, live, claude_age, claude_expired=None):
    """Machine-readable ls / usage for other front ends. Never includes tokens or provider keys."""
    accounts = []
    for n, h, k in all_acc:
        i = info[n]
        # relay: is the status-line relay installed (Claude accounts only; None for Codex)
        # expired: cached windows whose reset time has passed (they have reset; the new usage is unknown)
        u = {"windows": [], "error": None, "age_seconds": claude_age.get(n),
             "expired": (claude_expired or {}).get(n, []) if k == "claude" else None,
             "relay": hook_installed(n) if k == "claude" else None,
             "shadowed_by": [{"file": s["file"], "dir": s["dir"]} for s in shadow_files(n, False) if not s["relay"]] if k == "claude" else None}
        if n in live:
            data, e = live[n]
            u["error"] = e
            rl = (data or {}).get("rate_limit") or {}
            for key in ("primary_window", "secondary_window"):
                w = rl.get(key)
                if isinstance(w, dict):
                    u["windows"].append({"window_seconds": w.get("limit_window_seconds"),
                                         "used_percent": w.get("used_percent"),
                                         "reset_after_seconds": w.get("reset_after_seconds")})
        accounts.append({"name": n, "agent": k, "home": h, "state": i["state"],
                         "email": i["email"], "plan": i["plan"], "until": i["until"], "usage": u})
    providers = [{"name": n, "agent": zp.agent(p), "endpoint": zp.endpoint(p), "models": zp.models(p)}
                 for n, p in zp.load(CONFIG_DIR).items()]
    bindings = [{"name": n, "dir": d, "kind": kind_of(n)} for n, d in read_tsv(BINDING_FILE)]
    print(json.dumps({"version": VERSION, "generated_at": int(time.time()),
                      "accounts": accounts, "providers": providers, "bindings": bindings},
                     ensure_ascii=False, indent=2))


def read_codex_info(home):
    r = {"email": None, "plan": None, "until": None, "tok": {}, "state": "ok"}
    try:
        with open(os.path.join(home, "auth.json")) as f:
            d = json.load(f)
    except Exception:
        r["state"] = "none"
        return r
    t = d.get("tokens") or {}
    r["tok"] = t
    if not t.get("id_token"):
        r["state"] = "apikey" if d.get("OPENAI_API_KEY") else "none"
        return r
    try:
        p = t["id_token"].split(".")[1]
        p += "=" * (-len(p) % 4)
        c = json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        r["state"] = "bad"
        return r
    a = c.get("https://api.openai.com/auth") or {}
    r["email"] = c.get("email") or c.get("preferred_username") or c.get("sub")
    r["plan"] = a.get("chatgpt_plan_type")
    r["until"] = (a.get("chatgpt_subscription_active_until") or "")[:10] or None
    return r


def fetch_codex_usage(tok):
    if not tok.get("access_token"):
        return None, "-"
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/wham/usage",
        headers={"Authorization": "Bearer " + tok["access_token"],
                 "chatgpt-account-id": tok.get("account_id", ""),
                 "User-Agent": "zorua"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=15)), None
    except urllib.error.HTTPError as e:
        return None, ("token expired (run codex once to refresh)" if e.code in (401, 403) else "HTTP %d" % e.code)
    except Exception as e:
        r = getattr(e, "reason", e)
        return None, "network error: %s (check proxy)" % (str(r) or type(r).__name__)

def read_claude_info(home):
    st = claude_status(home)
    r = {"email": None, "plan": None, "until": None, "tok": {}, "state": "ok"}
    if st.get("error"):
        r["state"], r["email"] = "bad", st["error"]
    elif not st.get("loggedIn"):
        r["state"] = "none"
    else:
        r["email"] = st.get("email") or st.get("orgName") or "(signed in)"
        r["plan"] = st.get("subscriptionType")
    return r


def token_expired(tokens):
    for field in ("access_token", "id_token"):
        try:
            part = tokens.get(field, "").split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            if "exp" in claims:
                return float(claims["exp"]) <= time.time()
        except (ValueError, IndexError, TypeError):
            continue
    return False


def collect_usage(all_acc, online):
    claude_age = {}
    claude_expired = {}
    accts = [(n, h) for n, h, _ in all_acc]
    kind = {n: k for n, _, k in all_acc}
    info = {n: read_codex_info(h) for n, h in accts if kind[n] == "codex"}
    with ThreadPoolExecutor(max_workers=8) as ex:
        cfuts = {n: ex.submit(read_claude_info, h) for n, h in accts if kind[n] == "claude"}
        info.update({n: f.result() for n, f in cfuts.items()})
    live = {}
    if online:
        # One request per email quota pool, shared with pick and usage for 30 seconds.
        cache_path = os.path.join(CONFIG_DIR, "usage-cache.json")
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
        pools = {}
        for n, h in accts:
            if kind[n] == "codex" and info[n]["state"] == "ok":
                identity = str(info[n]["email"] or info[n]["tok"].get("account_id") or h).casefold()
                pool = hashlib.sha256(identity.encode()).hexdigest()
                pools.setdefault(pool, []).append(n)
        now = time.time()
        results, pending = {}, {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            for pool, names in pools.items():
                cached = cache.get(pool, {})
                age = now - cached.get("at", 0)
                if 0 <= age < 30 and "result" in cached:
                    data, error = copy.deepcopy(cached["result"])
                    for w in ((data or {}).get("rate_limit") or {}).values():
                        if isinstance(w, dict) and "reset_after_seconds" in w:
                            w["reset_after_seconds"] = max(0, w["reset_after_seconds"] - int(age))
                    results[pool] = (data, error)
                else:
                    # Prefer a non-expired credential when several homes share a pool.
                    n = next((n for n in names if not token_expired(info[n]["tok"])), names[0])
                    pending[pool] = ex.submit(fetch_codex_usage, info[n]["tok"])
            for pool, future in pending.items():
                results[pool] = future.result()
                cache[pool] = {"at": now, "result": results[pool]}
        if pending:
            _write_pick_json(cache_path, {p: c for p, c in cache.items() if now - c.get("at", 0) < 30})
        live = {n: results[pool] for pool, names in pools.items() for n in names}
        # Claude Code windows come from the status-line relay's cache, not the network.
        # A window is dropped once its reset time has passed (it no longer applies).
        now = time.time()
        for n, h in accts:
            if kind[n] != "claude":
                continue
            c = claude_usage_cache(h) or {}
            ws, gone = [], []
            for key, secs in (("five_hour", 18000), ("seven_day", 604800)):
                w = c.get(key)
                if isinstance(w, dict) and w.get("resets_at") and w["resets_at"] > now:
                    ws.append({"limit_window_seconds": secs, "used_percent": int(round(float(w["used_percentage"]))),
                               "reset_after_seconds": int(w["resets_at"] - now)})
                elif isinstance(w, dict) and w.get("resets_at"):
                    gone.append({"window_seconds": secs, "reset_ago_seconds": int(now - w["resets_at"])})
            claude_expired[n] = gone
            claude_age[n] = (int(now - c["updated_at"]) if c.get("updated_at") else None) if c else None
            if ws:
                live[n] = ({"rate_limit": {"primary_window": ws[0], "secondary_window": ws[1] if len(ws) > 1 else None}}, None)

    return info, live, claude_age, claude_expired


def render(online, verbose, expiry=False, as_json=False):
    # The subscription end date is read from the cached sign-in token, which can be stale, so it
    # is only shown on request: --expiry, or ZORUA_EXPIRY=1.
    show_exp = expiry or os.environ.get("ZORUA_EXPIRY") == "1"
    color = (sys.stdout.isatty() and not os.environ.get("NO_COLOR")) or "always" in (os.environ.get("ZORUA_COLOR"), os.environ.get("CX_COLOR"))
    all_acc = all_accounts()
    accts = [(n, h) for n, h, _ in all_acc]
    kind = {n: k for n, _, k in all_acc}
    has_claude = any(k == "claude" for k in kind.values())
    cur = {"codex": current_account("codex"), "claude": current_account("claude")}
    auto = {"codex": os.environ.get("ZORUA_AUTO_ACTIVE", ""), "claude": os.environ.get("ZORUA_AUTO_CLAUDE", "")}

    def is_cur(n):
        return cur[kind[n]] == n

    def is_auto(n):
        return bool(auto[kind[n]]) and auto[kind[n]] == n

    def paint(code, s):
        return "\033[%sm%s\033[0m" % (code, s) if color and code else s

    info, live, claude_age, claude_expired = collect_usage(all_acc, online)

    if as_json:
        emit_json(all_acc, info, live, claude_age, claude_expired)
        return

    def ago(secs):
        return "%dm" % (secs // 60) if secs < 3600 else "%dh%dm" % (secs // 3600, secs % 3600 // 60) if secs < 86400 else "%dd" % (secs // 86400)

    def left(s):
        s = int(s)
        d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
        return "%dd%dh" % (d, h) if d and h else "%dd" % d if d else "%dh%dm" % (h, m) if h else "%dm" % m

    def label(s):
        s = int(s)
        return "%dH" % (s // 3600) if s < 86400 else "%dD" % (s // 86400)

    def pct_code(p):
        return "31" if p >= 80 else "33" if p >= 50 else "32"

    def bar(p, n):
        f = math.ceil(p / 100 * n) if p > 0 else 0
        return paint(pct_code(p), "▓" * f) + paint("2", "░" * (n - f))

    def plain_bar(p, n):
        f = math.ceil(p / 100 * n) if p > 0 else 0
        return "▓" * f + "░" * (n - f)

    PLAN = {"pro": "36", "promax": "35", "max": "35", "team": "34", "enterprise": "34", "plus": "32"}
    stale = [False]
    today = date.today()

    def expiry(u):
        if not u:
            return "–", "2"
        try:
            days = (date.fromisoformat(u) - today).days
        except ValueError:
            return u, None
        if days <= 7:
            stale[0] = True
            return u + " ⚠", "31"
        return u, None

    def windows(n):
        d, e = live.get(n, (None, None))
        ws = []
        if d:
            rl = d.get("rate_limit") or {}
            for k in ("primary_window", "secondary_window"):
                w = rl.get(k)
                if w:
                    ws.append((label(w["limit_window_seconds"]), w["used_percent"],
                               w["reset_after_seconds"], w["limit_window_seconds"]))
        return d, e, ws

    def who(n):
        i = info[n]
        return i["email"] or {"none": "(not signed in)", "apikey": "API key", "bad": "(unknown)"}.get(i["state"], "?")

    def mark(n):
        return paint("36", "●") if is_cur(n) else paint("33", "◆") if is_auto(n) else " "

    def table(rows, header):
        # A short row (e.g. an error message in place of the usage columns) spills
        # its last cell to the right; it must not widen the column it starts in.
        def counts(r, i):
            return i < len(r) and (len(r) == len(header) or i < len(r) - 1)
        widths = [max([len(r[i][0]) for r in [header] + rows if counts(r, i)] or [0]) for i in range(len(header))]
        for r in [header] + rows:
            out = []
            for i, (s, code) in enumerate(r):
                pad = " " * (widths[i] - len(s))
                out.append(paint(code, s) + pad if i < len(r) - 1 else paint(code, s))
            print(" " + "  ".join(out).rstrip())

    def head(*cols):
        return [(c, "2") for c in cols]

    def name_cell(n):
        return (("● " if is_cur(n) else "◆ " if is_auto(n) else "  ") + n, None)

    groups = [(k, [(n, h) for n, h in accts if kind[n] == k]) for k in ("codex", "claude")]
    groups = [(k, g) for k, g in groups if g]
    titled = len(groups) > 1          # headings only when both agents are present

    def heading(k):
        if titled:
            print(" " + paint("1", TITLES[k]))

    print()
    for gi, (k, group) in enumerate(groups):
        if gi:
            print()
        heading(k)
        has_exp = k == "codex" and show_exp     # Claude subscription expiry is not exposed
        if verbose:
            sep = paint("2", " " + "─" * 58)
            for n, h in group:
                i = info[n]
                d, e, ws = windows(n)
                plan = (d or {}).get("plan_type") or i["plan"]
                exp, ecode = expiry(i["until"])
                print(sep)
                print(" %s %s  %s  %s  %s" % (mark(n), paint("1", "%-10s" % n), who(n),
                      paint(PLAN.get(plan, "2"), plan or "–"), paint(ecode, "exp " + exp) if i["until"] and show_exp else ""))
                print("   " + paint("2", short(h)))
                if e:
                    print("   " + paint("31", e))
                for lab, p, rs, _ in ws:
                    print("   %-3s %s %3d%%   %s" % (lab.lower(), bar(p, 20), p, paint("2", "resets in " + left(rs))))
                c = (d or {}).get("credits") or {}
                if c.get("has_credits") and c.get("balance"):
                    print("   " + paint("2", "credits %s" % format(int(float(c["balance"])), ",")))
            print(sep)
        elif online:
            rows = []
            for n, h in group:
                i = info[n]
                d, e, ws = windows(n)
                plan = (d or {}).get("plan_type") or i["plan"]
                exp, ecode = expiry(i["until"])
                tail = [(exp, ecode)] if has_exp else []
                row = [name_cell(n), (plan or "–", PLAN.get(plan, "2"))]
                if k == "claude" and i["state"] == "ok" and not ws:
                    rows.append(row + [("–", "2")] * 3)
                    continue
                if e or i["state"] != "ok":
                    row.append((e or who(n), "31"))
                    rows.append(row)
                    continue
                w5 = next((w for w in ws if w[3] < 86400), None)
                w7 = next((w for w in ws if w[3] >= 86400), None)
                for w in (w5, w7):
                    row.append(("%s %3d%%" % (plain_bar(w[1], 6), w[1]), pct_code(w[1])) if w else ("–", "2"))
                row.append((left(w7[2]) if w7 else left(w5[2]) if w5 else "–", None))
                rows.append(row + tail)
            table(rows, head(*(["  NAME", "PLAN", "5H", "7D", "RESET"] + (["EXPIRES"] if has_exp else []))))
        else:
            rows = []
            for n, h in group:
                i = info[n]
                exp, ecode = expiry(i["until"])
                row = [name_cell(n), (who(n), None), (i["plan"] or "–", PLAN.get(i["plan"], "2"))]
                rows.append(row + ([(exp, ecode)] if has_exp else []))
            table(rows, head(*(["  NAME", "ACCOUNT", "PLAN"] + (["EXPIRES"] if has_exp else []))))

    provs = zp.load(CONFIG_DIR)
    pcur = {a: os.environ.get(VARS[k], "") for k, a in PROVIDER_AGENT.items()}
    pauto = {a: os.environ.get(AUTO_VARS[k][0], "") for k, a in PROVIDER_AGENT.items()}
    for agent in zp.AGENTS:
        mine = {n: p for n, p in provs.items() if zp.agent(p) == agent}
        if not mine:
            continue
        if groups or agent == "codex" and any(zp.agent(p) == "claude" for p in provs.values()):
            print()
        print(" " + paint("1", "Providers (%s)" % TITLES[agent]))
        prows = []
        for n, p in sorted(mine.items()):
            prows.append([(("● " if n == pcur[agent] else "◆ " if n == pauto[agent] else "  ") + n, None),
                          (zp.endpoint(p), None), (zp.mask(zp.secret(p)) if zp.secret(p) else "-", "2"),
                          (zp.model_summary(p), "2")])
        table(prows, head("  NAME", "ENDPOINT", "KEY", "MODELS"))
    print()
    notes = []
    if any(is_cur(n) or is_auto(n) for n, _ in accts) or any(v in provs for v in list(pcur.values()) + list(pauto.values())):
        notes.append("● this shell  ◆ auto-bound directory")
    if online and has_claude:
        for n, _ in accts:
            if kind[n] != "claude":
                continue
            if claude_age.get(n) is not None:
                notes.append("%s: Claude usage as of %s ago (from its last session)" % (n, ago(claude_age[n])))
                for g in claude_expired.get(n, []):
                    notes.append("%s: the %s window reset %s ago; waiting for a new claude session to report it"
                                 % (n, label(g["window_seconds"]), ago(g["reset_ago_seconds"])))
            elif [s for s in shadow_files(n, False) if not s["relay"]]:
                s = [s for s in shadow_files(n, False) if not s["relay"]][0]
                notes.append("%s: no Claude usage yet — %s hides the relay in %s; run 'zorua hook install %s --shadows'" % (n, s["file"], s["dir"], n))
            elif hook_installed(n):
                notes.append("%s: no Claude usage yet — relay is installed; restart claude under this account and send a message" % n)
            else:
                notes.append("%s: no Claude usage yet — run 'zorua hook install %s', then use claude once" % (n, n))
    if stale[0] and show_exp:
        notes.append("⚠ expired or within 7 days (date comes from the cached sign-in token, may be stale)")
    if not online:
        notes.append("zorua usage: live limits  ·  zorua ls -v: details")
    for t in notes:
        print(" " + paint("2", t))
    print()


# --------------------------------------------------------------------------
# Small interactive helpers
# --------------------------------------------------------------------------

def confirm(prompt, default="y"):
    hint = "[y/N]" if default == "n" else "[Y/n]"
    try:
        ans = input("%s %s " % (prompt, hint)).strip()
    except EOFError:
        ans = ""
        print()
    if not ans:
        ans = default
    return ans[:1].lower() == "y"


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        return ""


def run_codex_login(home, device=False):
    env = dict(os.environ, CODEX_HOME=home)
    cmd = ["codex", "login"] + (["--device-auth"] if device else [])
    try:
        return subprocess.call(cmd, env=env)
    except FileNotFoundError:
        err("zorua: codex not found on PATH — install the Codex CLI first")
        return 127


def account_email(home):
    try:
        with open(os.path.join(home, "auth.json")) as f:
            d = json.load(f)
        t = (d.get("tokens") or {}).get("id_token")
        if not t:
            return "API key" if d.get("OPENAI_API_KEY") else "(no credentials)"
        p = t.split(".")[1]
        p += "=" * (-len(p) % 4)
        c = json.loads(base64.urlsafe_b64decode(p))
        return c.get("email") or c.get("preferred_username") or c.get("sub", "?")
    except FileNotFoundError:
        return "(not signed in)"
    except Exception:
        return "(unknown)"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_ls(args, usage=False):
    online, verbose, expiry, as_json = usage, False, False, False
    for a in args:
        if a in ("-u", "--usage"):
            online = True
        elif a in ("-v", "--verbose"):
            verbose = True
        elif a == "--expiry":
            expiry = True
        elif a == "--json":
            as_json = True
        else:
            err("zorua ls: unknown option %s (use -v, --expiry, --json)" % a)
            return 1
    render(online, verbose, expiry, as_json)
    return 0


def _write_pick_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".zorua-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(value, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def remove_tags(name):
    tags = load_tags()
    if name in tags:
        del tags[name]
        _write_pick_json(os.path.join(CONFIG_DIR, "tags.json"), tags)


def load_tags():
    try:
        with open(os.path.join(CONFIG_DIR, "tags.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def cmd_tag(args):
    if not args or args[0] not in all_names():
        err("usage: zorua tag <account-or-provider> [+tag ...] [-tag ...]")
        return 1
    tags = load_tags()
    name = args[0]
    chosen = set(tags.get(name, []))
    for spec in args[1:]:
        if not re.match(r"^[+-][A-Za-z0-9_.-]+$", spec):
            err("zorua tag: use +tag or -tag (letters, digits, . _ -)")
            return 1
        if spec[0] == "+":
            chosen.add(spec[1:])
        else:
            chosen.discard(spec[1:])
    if len(args) > 1:
        tags[name] = sorted(chosen)
        _write_pick_json(os.path.join(CONFIG_DIR, "tags.json"), tags)
    print("%s: %s" % (name, " ".join(sorted(chosen)) or "(no tags)"))
    return 0


def account_models(home, kind):
    """Only locally evidenced models; never send a discovery request during pick."""
    ids = []
    try:
        if kind == "codex":
            with open(os.path.join(home, "models_cache.json")) as f:
                catalog = json.load(f).get("models", [])
            ids.extend(m.get("slug") or m.get("id") for m in catalog if isinstance(m, dict))
        else:
            with open(os.path.join(home, "settings.json")) as f:
                settings = json.load(f)
            ids.extend(zp.env_models(settings.get("env") or {}))
            ids.append(settings.get("model"))
    except (OSError, ValueError, AttributeError):
        pass
    if kind == "codex":
        try:
            with open(os.path.join(home, "config.toml")) as f:
                head = f.read().split("\n[", 1)[0]
            m = re.search(r'^\s*model\s*=\s*"([^"\n]+)"', head, re.M)
            if m:
                ids.append(m.group(1))
        except OSError:
            pass
    return sorted(set(i for i in ids if isinstance(i, str) and i))


def _pick_model(cat, sel):
    if not sel:
        return next(iter(cat.values()), None)
    exact = cat.get(sel) or next((i for i in cat.values() if i == sel), None)
    if exact:
        return exact
    hits = {i for a, i in cat.items() if a.startswith(sel) or i.startswith(sel)}
    return next(iter(hits)) if len(hits) == 1 else None


def cmd_pick(args):
    opts = {"--kind": "", "--needs": "", "--avoid": "", "--model": ""}
    as_json, i = False, 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            as_json = True
        elif a in opts and i + 1 < len(args) and not args[i + 1].startswith("--"):
            opts[a] = args[i + 1]
            i += 1
        else:
            err("usage: zorua pick --kind codex|claude [--needs tag,...] [--avoid tag,...] [--model ID-or-prefix] [--json]")
            return 1
        i += 1
    kind = opts["--kind"]
    if kind not in zp.AGENTS:
        err("zorua pick: --kind must be codex or claude")
        return 1
    needs = set(filter(None, opts["--needs"].split(",")))
    avoid = set(filter(None, opts["--avoid"].split(",")))
    tags = load_tags()
    excluded, candidates = [], []
    matched = []
    # Filter cheap constraints before fetching any usage or Claude login status.
    for n, h, k in all_accounts():
        reasons = []
        have = set(tags.get(n, []))
        if k != kind:
            reasons.append("wrong kind: " + k)
        if needs - have:
            reasons.append("missing tags: " + ",".join(sorted(needs - have)))
        if avoid & have:
            reasons.append("avoided tags: " + ",".join(sorted(avoid & have)))
        cat = {mid: mid for mid in account_models(h, k)} if opts["--model"] else {}
        if opts["--model"] and not _pick_model(cat, opts["--model"]):
            reasons.append("model unavailable or ambiguous in local catalog: " + opts["--model"])
        if reasons:
            excluded.append({"name": n, "reasons": reasons})
        else:
            matched.append((n, h, k))
    info, live, age, expired = collect_usage(matched, True)
    for n, h, k in matched:
        item = info[n]
        reasons = []
        if item["state"] != "ok":
            reasons.append("not signed in or invalid credentials")
        if token_expired(item["tok"]):
            reasons.append("login token expired; run zorua login " + n)
        data, error = live.get(n, (None, None))
        if error and "token expired" in error:
            reasons.append(error)
        remaining = {"7d": None, "5h": None}
        for w in ((data or {}).get("rate_limit") or {}).values():
            if isinstance(w, dict) and w.get("reset_after_seconds", 0) > 0:
                secs = w.get("limit_window_seconds")
                if secs in (18000, 604800):
                    remaining["7d" if secs == 604800 else "5h"] = max(0, min(100, 100 - float(w["used_percent"])))
        if any(value == 0 for value in remaining.values()):
            reasons.append("quota exhausted")
        if reasons:
            excluded.append({"name": n, "reasons": reasons})
            continue
        candidates.append({"name": n, "agent": k, "email": item["email"], "remaining": remaining,
                           "tags": sorted(tags.get(n, [])), "reason": "ranked by remaining 7d, then 5h quota; unknown quota ranks last"})
    for n, p in zp.load(CONFIG_DIR).items():
        reasons = []
        have = set(tags.get(n, []))
        if zp.agent(p) != kind:
            reasons.append("wrong kind: " + zp.agent(p))
        if needs - have:
            reasons.append("missing tags: " + ",".join(sorted(needs - have)))
        if avoid & have:
            reasons.append("avoided tags: " + ",".join(sorted(avoid & have)))
        cat = zp.models(p)
        if not cat:
            ids = zp.env_models(p.get("env", {})) if kind == "claude" else [p.get("model")]
            cat = {mid: mid for mid in ids if mid}
        mid = _pick_model(cat, opts["--model"])
        if not mid:
            reasons.append("model unavailable or ambiguous: " + (opts["--model"] or "no configured model"))
        if not (zp.secret(p) or p.get("key_ref") == "keychain"):
            reasons.append("no configured provider key")
        if reasons:
            excluded.append({"name": n, "reasons": reasons})
        else:
            candidates.append({"name": n + ":" + mid, "agent": kind, "email": None,
                               "remaining": {"7d": None, "5h": None}, "tags": sorted(have),
                               "reason": "configured provider key and model; provider quota/expiry unknown; no extra network request"})
    def score(c):
        return tuple(c["remaining"][k] if c["remaining"][k] is not None else -1 for k in ("7d", "5h"))
    candidates.sort(key=lambda c: (tuple(-v for v in score(c)), c["name"]))
    pools, ranked = set(), []
    for c in candidates:
        pool = c["email"].casefold() if c["email"] else c["name"]
        if pool in pools:
            excluded.append({"name": c["name"], "reasons": ["shared email quota pool; a better candidate represents it"]})
        else:
            pools.add(pool)
            ranked.append(c)
    result = {"recommendation": ranked[0] if ranked else None, "alternatives": ranked[1:], "excluded": excluded}
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif ranked:
        for label, c in [("recommended", ranked[0])] + [("alternative", c) for c in ranked[1:]]:
            print("%s: %s  (7d left %s; 5h left %s)" % (label, c["name"],
                  "unknown" if c["remaining"]["7d"] is None else "%g%%" % c["remaining"]["7d"],
                  "unknown" if c["remaining"]["5h"] is None else "%g%%" % c["remaining"]["5h"]))
            print("  " + c["reason"])
    else:
        print("no eligible candidates")
        for c in excluded:
            print("  %s: %s" % (c["name"], "; ".join(c["reasons"])))
    return 0 if ranked else 1


def _doctor_json(path):
    try:
        with open(path) as f:
            value = json.load(f)
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except FileNotFoundError:
        return {}


def _doctor_toml(path):
    try:
        with open(path) as f:
            raw = f.read()
    except FileNotFoundError:
        return {}, ""
    try:
        import tomllib
    except ImportError:
        # Python 3.8-3.10: the hook trust table is simple enough to read without
        # a TOML dependency. Other inline hook definitions are reported unverified.
        states = {}
        pattern = r'^\s*\[hooks\.state\.("(?:[^"\\]|\\.)*"|[^\]]+)\]\s*\n(.*?)(?=^\s*\[|\Z)'
        for match in re.finditer(pattern, raw, re.M | re.S):
            key = match.group(1).strip()
            key = json.loads(key) if key.startswith('"') else key.strip("'")
            entry = {}
            trusted = re.search(r'^\s*trusted_hash\s*=\s*"([^"\n]+)"', match.group(2), re.M)
            if trusted:
                entry["trusted_hash"] = trusted.group(1)
            if re.search(r'^\s*enabled\s*=\s*false\b', match.group(2), re.M):
                entry["enabled"] = False
            states[key] = entry
        plugins = {}
        for match in re.finditer(r'^\s*\[plugins\.("(?:[^"\\]|\\.)*"|[^\]]+)\]\s*\n(.*?)(?=^\s*\[|\Z)', raw, re.M | re.S):
            key = match.group(1).strip()
            key = json.loads(key) if key.startswith('"') else key.strip("'")
            plugins[key] = {"enabled": bool(re.search(r'^\s*enabled\s*=\s*true\b', match.group(2), re.M))}
        return {"hooks": {"state": states}, "plugins": plugins, "_partial": True}, raw
    return tomllib.loads(raw), raw


def _doctor_hook_hash(event, group, handler):
    # Codex hashes sorted JSON of normalized TOML values, not the source file.
    # See codex-rs/hooks/src/engine/discovery.rs and config/src/fingerprint.rs.
    if handler.get("type") != "command":
        return None
    timeout = max(1, int(handler.get("timeout", 1 if event in ("SessionEnd", "Interrupt") else 600)))
    if event in ("SessionEnd", "Interrupt"):
        timeout = min(3, timeout)
    h = {"type": "command", "command": handler["command"], "timeout": timeout, "async": handler.get("async", False)}
    if handler.get("statusMessage") is not None:
        h["statusMessage"] = handler["statusMessage"]
    limit = handler.get("additionalContextLimit")
    if event in ("PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "SubagentStart") and limit is not None and limit != 2500:
        h["additionalContextLimit"] = limit
    identity = {"event_name": re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower(), "hooks": [h]}
    matcher = group.get("matcher")
    if matcher is not None and matcher.strip():
        identity["matcher"] = matcher.strip()
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def doctor_hooks(home, config, raw):
    path = os.path.abspath(os.path.join(home, "hooks.json"))
    sources = [(path, _doctor_json(path).get("hooks", {}))]
    hooks = config.get("hooks") or {}
    sources.append((os.path.abspath(os.path.join(home, "config.toml")), {k: v for k, v in hooks.items() if k != "state"}))
    states = hooks.get("state", {})
    missing, modified, unknown, total = 0, 0, 0, 0
    for source, events in sources:
        for event, groups in events.items():
            if not isinstance(groups, list):
                raise ValueError("hook event must contain matcher groups")
            event_key = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
            for gi, group in enumerate(groups):
                for hi, handler in enumerate(group.get("hooks", [])):
                    key = "%s:%s:%d:%d" % (source, event_key, gi, hi)
                    state = states.get(key, states.get("file:" + key, {}))
                    if state.get("enabled") is False:
                        continue
                    total += 1
                    trusted = state.get("trusted_hash")
                    current = _doctor_hook_hash(event, group, handler)
                    if not trusted:
                        missing += 1
                    elif current is None:
                        unknown += 1
                    elif trusted != current:
                        modified += 1
    partial = config.get("_partial") and re.search(r'^\s*\[\[?hooks\.(?!state)', raw, re.M)
    if missing or modified:
        return "fail", "%d untrusted, %d modified enabled hook(s)" % (missing, modified)
    # Plugin hooks have a separate content fingerprint. Never assert they are trusted
    # just because a declaration has a saved trusted_hash.
    if unknown or partial or config.get("plugins"):
        return "warn", "account hooks inspected; inline/plugin/MCP hooks need review in Codex /hooks"
    return "ok", "%d enabled account hook(s), trust hashes match" % total


def _doctor_permissions(path, sensitive=False):
    st = os.stat(path)
    mode = stat.S_IMODE(st.st_mode)
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return False
    if sensitive:
        return mode & 0o077 == 0 and bool(mode & 0o400)
    return mode & 0o022 == 0 and mode & 0o700 == 0o700


def doctor_account(name, home, kind):
    checks = []
    def add(label, status, detail, fix=""):
        checks.append({"check": label, "status": status, "detail": detail, "fix": fix if status != "ok" else ""})
    quoted = shlex.quote(home)
    exists = os.path.isdir(home)
    add("directory", "ok" if exists else "fail", "configuration directory exists" if exists else "configuration directory missing",
        "Restore the directory or re-register: zorua add %s --home %s --no-login" % (name, quoted))
    if exists:
        add("permissions", "ok" if _doctor_permissions(home) else "fail", "directory ownership and permissions",
            "Check ownership, then chmod 700 %s" % quoted)
    for filename in (("auth.json",) if kind == "codex" else (".credentials.json", "settings.json")):
        path = os.path.join(home, filename)
        if os.path.isfile(path):
            add("permissions:" + filename, "ok" if _doctor_permissions(path, True) else "fail", "private credential/settings file",
                "Check ownership, then chmod 600 %s" % shlex.quote(path))
    if kind == "claude":
        status = claude_status(home) if exists else {}
        add("login", "ok" if status.get("loggedIn") else "fail", "signed in" if status.get("loggedIn") else "not signed in or status unavailable",
            "zorua login " + name)
        try:
            settings = _doctor_json(os.path.join(home, "settings.json"))
            env = settings.get("env") or {}
            proxy = any(env.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"))
            add("proxy", "ok" if proxy else "warn", "proxy configured in account settings" if proxy else "no proxy in account settings; direct access may return 403/401",
                "Set env.HTTPS_PROXY (or HTTP_PROXY/ALL_PROXY) in this account's settings.json when a proxy is required")
        except (OSError, ValueError, AttributeError):
            settings = {}
            add("settings", "fail", "cannot parse/read settings.json", "Repair this account's settings.json")
        integration_text = json.dumps(settings.get("hooks", {}))
    else:
        try:
            auth = _doctor_json(os.path.join(home, "auth.json"))
            tokens = auth.get("tokens") or {}
            valid = bool(auth.get("OPENAI_API_KEY") or tokens.get("access_token") or tokens.get("id_token"))
            expired = False
            for field in ("access_token", "id_token"):
                try:
                    part = tokens.get(field, "").split(".")[1]
                    claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                    if "exp" in claims:
                        expired = float(claims["exp"]) <= time.time()
                        break
                except (ValueError, IndexError, TypeError):
                    continue
            add("login", "ok" if valid and not expired else "fail", "credentials present (local check)" if valid and not expired else "missing, invalid or expired credentials", "zorua login " + name)
        except (OSError, ValueError, AttributeError):
            add("login", "fail", "cannot parse/read auth.json", "zorua login " + name)
        try:
            config, raw = _doctor_toml(os.path.join(home, "config.toml"))
            hook_status, detail = doctor_hooks(home, config, raw)
            add("hooks", hook_status, detail, "Start codex with CODEX_HOME=%s, then review and trust the current definitions in /hooks" % quoted)
            integration_text = raw + json.dumps(_doctor_json(os.path.join(home, "hooks.json")))
            if shutil.which("codex"):
                plugins = config.get("plugins", {})
                browser = any("browser" in n.lower() and isinstance(p, dict) and p.get("enabled") is True for n, p in plugins.items())
                add("browser", "ok" if browser else "warn", "browser plugin enabled" if browser else "no enabled browser plugin detected in account config",
                    "Open /plugins under this CODEX_HOME and install/enable the browser plugin")
        except (OSError, ValueError, AttributeError, TypeError, KeyError):
            integration_text = ""
            add("hooks", "fail", "cannot parse account hook/config files", "Repair hooks.json/config.toml, then review /hooks")
    if shutil.which("herdr"):
        env = dict(os.environ, CODEX_HOME=home, CLAUDE_CONFIG_DIR=home)
        try:
            r = subprocess.run(["herdr", "integration", "status"], env=env, capture_output=True, text=True, timeout=10)
            line = next((line for line in r.stdout.splitlines() if line.startswith(kind + ":")), "")
            installed = bool(re.search(r": (?:current|installed|outdated)\b", line)) and "herdr" in integration_text.lower()
            add("herdr", "ok" if installed else "warn", "Herdr state hook registered" if installed else "Herdr state hook missing or status unavailable for this account",
                "With %s=%s, run herdr integration install %s, then review the account hook registration" % (VARS[kind], quoted, kind))
        except (OSError, subprocess.TimeoutExpired):
            add("herdr", "warn", "Herdr integration status unavailable", "Run herdr integration status under this account's environment")
    return {"name": name, "agent": kind, "checks": checks}


def cmd_doctor(args):
    names = [a for a in args if not a.startswith("-")]
    if set(args) - set(names) - {"--json"}:
        err("usage: zorua doctor [name ...] [--json]")
        return 1
    # Never init/migrate the registry: even a first-run doctor is read-only.
    acc = all_accounts()
    if not os.path.exists(ACCOUNT_FILE):
        acc.insert(0, ("default", os.path.join(HOME, ".codex"), "codex"))
    providers = zp.load(CONFIG_DIR)
    known = {n for n, _, _ in acc} | set(providers)
    if any(n not in known for n in names):
        err("zorua doctor: unknown name(s): " + " ".join(n for n in names if n not in known))
        return 1
    selected = set(names or known)
    reports = []
    for n, h, k in acc:
        if n not in selected:
            continue
        try:
            reports.append(doctor_account(n, h, k))
        except OSError:
            reports.append({"name": n, "agent": k, "checks": [{"check": "directory", "status": "fail", "detail": "cannot inspect account directory", "fix": "Check directory ownership and read permissions"}]})
    order = sorted(selected & set(providers))
    with ThreadPoolExecutor(max_workers=8) as ex:
        def check_provider(n):
            if hasattr(zp, "check_stored"):
                return zp.check_stored(n, providers[n])
            return zp.check(providers[n])
        results = {n: ex.submit(check_provider, n) for n in order}
        for n, future in results.items():
            res = future.result()
            reports.append({"name": n, "agent": zp.agent(providers[n]), "checks": [{"check": "provider", "status": res["status"], "detail": res["detail"], "fix": "" if res["status"] == "ok" else "Check endpoint/key and run zorua provider check " + n}]})
    if "--json" in args:
        print(json.dumps({"accounts": reports}, ensure_ascii=False, indent=2))
    else:
        for report in reports:
            print("[%s] (%s)" % (report["name"], report["agent"]))
            for c in report["checks"]:
                print("  %s %s: %s" % ("✓" if c["status"] == "ok" else "✗", c["check"], c["detail"]))
                if c["fix"]:
                    print("    fix: " + c["fix"])
    return 1 if any(c["status"] == "fail" for r in reports for c in r["checks"]) else 0


def claude_override_warning(home):
    """Warn when inherited variables would outrank the Claude account we just selected."""
    present = [v for v in CLAUDE_OVERRIDES if os.environ.get(v)]
    if os.environ.get("ANTHROPIC_BASE_URL"):
        present.append("ANTHROPIC_BASE_URL")
    if present:
        err("zorua: warning: %s %s set in this shell and may override or redirect the account's own login "
            "(unset them, or use the one-shot form 'zorua <name> ...', which cleans them)"
            % (", ".join(present), "is" if len(present) == 1 else "are"))


def pick_model(st, kind, prov, sel):
    """Select model `sel` of the provider just activated for `kind`. -> exit code"""
    mid = zp.resolve_model(prov, sel)
    if mid is None:
        err("zorua: unknown model '%s' for this provider (models: %s)" % (sel, " ".join(zp.models(prov)) or "none; add some with zorua provider models <name> add|fetch"))
        return 1
    st.set_model(kind, zp.selected_alias(prov, mid))
    return 0


def cmd_use(args, st):
    name, _, msel = (args[0] if args else "").partition(":")
    if not name or name == "-":
        for k in KINDS:
            st.set_home(k, "")
        print("codex account: default (%s/.codex); claude account and providers: cleared" % HOME)
        st.prompt()
        return 0
    kind = kind_of(name)
    if kind is None:
        names = all_names()
        err("zorua: unknown account '%s' (accounts: %s)" % (name, " ".join(names)))
        return 1
    if msel and not is_provider(kind):
        err("zorua: only providers have models; '%s' is an account" % name)
        return 1
    home = registry(kind)[name]
    if is_provider(kind):
        agent = PROVIDER_AGENT[kind]
        prov = zp.load(CONFIG_DIR)[name]
        if msel and zp.resolve_model(prov, msel) is None:
            return pick_model(st, kind, prov, msel)           # reports the error before anything changes
        st.set_home(kind, name)
        if msel:
            pick_model(st, kind, prov, msel)
        print("this shell -> %s provider: %s (%s)%s; plain '%s' in this shell now uses it"
              % (agent, name, zp.endpoint(prov), ", model " + st.model[kind] if st.model[kind] else "", agent))
        st.prompt()
        return 0
    if not os.path.isdir(home):
        err("zorua: directory not found: %s" % home)
        return 1
    if kind == "codex":
        st.set_home("codex", "" if name == "default" else home)
    else:
        st.set_home("claude", home)
        claude_override_warning(home)
    print("this shell -> %s account: %s" % (kind, name))
    st.prompt()
    return 0


def cmd_model(args, st):
    """zorua model            list the models of the active provider(s)
       zorua model <alias>    pick one for the active provider that has it
       zorua model -          back to the provider's own default"""
    provs = zp.load(CONFIG_DIR)
    active = [(k, st.home[k], provs[st.home[k]]) for k in MODEL_VAR if st.home.get(k) in provs]
    if not active:
        err("zorua: no provider is active in this shell (zorua use <provider>)")
        return 1
    if not args:
        for k, name, prov in active:
            cat = zp.models(prov)
            print("%s provider %s%s" % (PROVIDER_AGENT[k], name, "" if cat else "  (no models yet: zorua provider models %s add|fetch)" % name))
            for alias, mid in cat.items():
                print("  %s %-24s %s" % ("●" if st.model[k] == alias else " ", alias, mid))
        return 0
    sel = args[0]
    if sel == "-":
        for k, _, _ in active:
            st.set_model(k, "")
        st.prompt()
        print("model: back to the provider's default")
        return 0
    hits = [(k, name, prov) for k, name, prov in active if zp.resolve_model(prov, sel) is not None]
    if not hits:
        err("zorua: no active provider has a model '%s' (try: zorua model)" % sel)
        return 1
    if len(hits) > 1:
        err("zorua: '%s' matches the Claude and the Codex provider; use  zorua use <provider>:%s" % (sel, sel))
        return 1
    k, name, prov = hits[0]
    pick_model(st, k, prov, sel)
    st.prompt()
    print("%s provider %s -> model %s (%s)" % (PROVIDER_AGENT[k], name, st.model[k], zp.resolve_model(prov, sel)))
    return 0


def cmd_off(st, args=()):
    if args and args[0] in ("provider", "providers") and len(args) == 1:
        for k in PROVIDER_AGENT:
            st.set_home(k, "")
        st.prompt()
        print("cleared the providers; accounts (CODEX_HOME, CLAUDE_CONFIG_DIR) kept")
        return 0
    if args:
        err("usage: zorua off [providers]")
        return 1
    for k in KINDS:
        st.set_home(k, "")
    st.prompt()
    print("cleared CODEX_HOME, CLAUDE_CONFIG_DIR and the providers; back to defaults")
    return 0


def run_claude_login(home):
    try:
        return subprocess.call(["claude", "auth", "login", "--claudeai"], env=claude_clean_env(home))
    except FileNotFoundError:
        err("zorua: claude not found on PATH — install Claude Code first")
        return 127


def cmd_login(args):
    names = all_names()
    if not args or kind_of(args[0]) is None:
        err("zorua: usage: zorua login <%s>" % "/".join(names))
        return 1
    kind = kind_of(args[0])
    if is_provider(kind):
        err("zorua: '%s' is a provider (API key); it has no login" % args[0])
        return 1
    home = registry(kind)[args[0]]
    return run_claude_login(home) if kind == "claude" else run_codex_login(home)


def seed_claude_settings(home):
    """Give a new Claude account the user's settings template (e.g. proxy env), never overwriting."""
    dst = os.path.join(home, "settings.json")
    if os.path.isfile(CLAUDE_SETTINGS_TEMPLATE) and not os.path.exists(dst):
        with open(CLAUDE_SETTINGS_TEMPLATE) as f:
            data = f.read()
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(data)


def cmd_add(args):
    name, home_override, do_login, device, claude = "", "", True, False, False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--home":
            if i + 1 >= len(args):
                err("zorua add: --home needs a directory")
                return 1
            home_override = args[i + 1]
            i += 2
            continue
        if a == "--no-login":
            do_login = False
        elif a == "--device-auth":
            device = True
        elif a == "--claude":
            claude = True
        elif a.startswith("-"):
            err("zorua add: unknown flag %s" % a)
            return 1
        elif not name:
            name = a
        else:
            err("zorua add: unexpected argument %s" % a)
            return 1
        i += 1
    if not name:
        err("usage: zorua add <name> [--claude] [--home DIR] [--no-login] [--device-auth]")
        return 1
    if claude and device:
        err("zorua add: --device-auth is Codex-only (Claude Code signs in through the browser)")
        return 1
    if not valid_name(name):
        err("zorua: name must match [A-Za-z0-9_-] and not collide with a command: %s" % name)
        return 1
    existing = kind_of(name)
    if existing:
        err("zorua: account '%s' already exists (%s): %s" % (name, existing, registry(existing)[name]))
        return 1
    if home_override:
        home = os.path.abspath(os.path.expanduser(home_override))
    else:
        home = os.path.join(HOME, (".claude-%s" if claude else ".codex-%s") % name)
    for n, p, k in all_accounts():
        if p == home:
            err("zorua: directory already registered as account '%s': %s" % (n, home))
            return 1
    os.makedirs(home, exist_ok=True)
    if claude:
        seed_claude_settings(home)
        signed_in = claude_status(home).get("loggedIn")
        if do_login and not signed_in:
            print("Complete the sign-in in your browser (Claude account: %s)..." % name)
            if run_claude_login(home) != 0:
                err("zorua: login failed; account not registered (directory kept: %s)" % home)
                return 1
        write_tsv(CLAUDE_FILE, claude_accounts() + [(name, home)])
    else:
        if do_login and not os.path.isfile(os.path.join(home, "auth.json")):
            print("Complete the sign-in in your browser (account: %s)..." % name)
            if run_codex_login(home, device) != 0:
                err("zorua: login failed; account not registered (directory kept: %s)" % home)
                return 1
        write_tsv(ACCOUNT_FILE, accounts() + [(name, home)])
    print("added %saccount: %s -> %s" % ("claude " if claude else "", name, home))
    signed_in = (claude_status(home).get("loggedIn") if claude
                 else os.path.isfile(os.path.join(home, "auth.json")))
    if not signed_in:
        print("not signed in yet: run  zorua login %s" % name)
    return 0


def cmd_rm(args):
    name = args[0] if args else ""
    purge = len(args) > 1 and args[1] == "--purge"
    if not name:
        err("usage: zorua rm <name> [--purge]")
        return 1
    if name == "default":
        err("zorua: default is built-in and cannot be removed")
        return 1
    kind = kind_of(name)
    if kind is None:
        names = all_names()
        err("zorua: unknown account '%s' (accounts: %s)" % (name, " ".join(names)))
        return 1
    if is_provider(kind):
        return provider_rm([name])
    home = registry(kind)[name]
    if home == os.environ.get(VARS[kind], ""):
        err("zorua: '%s' is active in this shell; run 'zorua use -' first" % name)
        return 1
    reg_file = CLAUDE_FILE if kind == "claude" else ACCOUNT_FILE
    write_tsv(reg_file, [(n, p) for n, p in read_tsv(reg_file) if n != name])
    if os.path.exists(BINDING_FILE):
        write_tsv(BINDING_FILE, [(n, p) for n, p in read_tsv(BINDING_FILE) if n != name])
    remove_tags(name)
    print("removed account from registry: %s" % name)
    if not purge and os.path.isdir(home) and sys.stdin.isatty():
        purge = confirm("Also delete data directory %s ? This cannot be undone" % home, "n")
    if purge:
        import shutil
        shutil.rmtree(home, ignore_errors=True)
        print("deleted data directory: %s" % home)
    elif os.path.isdir(home):
        print("data directory kept: %s (delete it yourself, or re-register with zorua add)" % home)
    return 0


def cmd_bind(args, st):
    name = args[0] if args else ""
    d = cwd()
    if not name:
        active = [k for k in KINDS if st.home[k]]
        if not active:
            err("zorua: currently on default; use 'zorua bind <name>' or 'zorua use <name>' first")
            return 1
        if len(active) > 1:
            err("zorua: more than one account/provider is active; name one: zorua bind <name>")
            return 1
        name = current_account(active[0])
    kind = kind_of(name)
    if kind is None:
        names = all_names()
        err("zorua: unknown account '%s' (accounts: %s)" % (name, " ".join(names)))
        return 1
    rows, existed = [], False
    for n, p in read_tsv(BINDING_FILE):
        if p == d and kind_of(n) == kind:
            existed = True
            rows.append((name, d))
        else:
            rows.append((n, p))
    if not existed:
        rows.append((name, d))
    write_tsv(BINDING_FILE, rows)
    print("bound: %s -> %s (auto-switch in this directory and subdirectories)" % (d, name))
    if existed:
        print("(replaced previous %s binding for this directory)" % kind)
    apply_binding(st, d)
    return 0


def cmd_unbind(args, st):
    d = args[0] if args else cwd()
    rows = read_tsv(BINDING_FILE)
    if not rows:
        print("zorua: no project bindings")
        return 0
    kept = [(n, p) for n, p in rows if p != d]
    if len(kept) == len(rows):
        print("zorua: no binding for %s" % d)
        return 0
    write_tsv(BINDING_FILE, kept)
    print("unbound: %s" % d)
    apply_binding(st, cwd())
    return 0


# --------------------------------------------------------------------------
# Providers (third-party Claude Code / Codex endpoints; logic in zorua_providers.py)
# --------------------------------------------------------------------------

def _flush_before_exec():
    """exec replaces the process without flushing: on Python 3.8 stderr is block-buffered when
    redirected, so a warning printed just before would be lost."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass


def _exec_agent(agent, extra, penv, args):
    EMIT.flush()
    _flush_before_exec()
    try:
        os.execvpe(agent, [agent] + extra + list(args), penv)
    except FileNotFoundError:
        err("zorua: %s not found on PATH" % agent)
        return 127


def _provider_for(agent, name):
    """-> the provider dict for `name` if it exists and belongs to `agent`, else None (after a warning)."""
    if not name:
        return None
    prov = zp.load(CONFIG_DIR).get(name)
    if prov is None or zp.agent(prov) != agent:
        err("zorua: %s provider '%s' not found; running plain %s" % (agent, name, agent))
        return None
    return zp.resolve_key(name, prov)


def _chosen_model(prov, sel):
    """-> model id for the alias picked in this shell, or None for the provider's own default"""
    if not sel:
        return None
    mid = zp.resolve_model(prov, sel)
    if mid is None:
        err("zorua: model '%s' is not in this provider's list any more; using its default (zorua model -)" % sel)
    return mid


def launch_claude(name, args, model_sel=""):
    """Replace this process with claude running on provider `name` (optionally on one of its models)."""
    prov = _provider_for("claude", name)
    if prov is None:
        return _exec_agent("claude", [], dict(os.environ), args)
    env = dict(prov["env"])
    mid = _chosen_model(prov, model_sel)
    if mid:
        env["ANTHROPIC_MODEL"] = mid
    settings = zp.write_settings(CONFIG_DIR, name, zp.settings_for(os.environ, env))
    return _exec_agent("claude", ["--settings", settings], zp.process_env(os.environ, env), args)


def launch_codex(name, args, model_sel=""):
    """Replace this process with codex running on provider `name` (key via env, config via -c)."""
    prov = _provider_for("codex", name)
    if prov is None:
        return _exec_agent("codex", [], dict(os.environ), args)
    return _exec_agent("codex", zp.codex_args(name, prov, _chosen_model(prov, model_sel)),
                       zp.codex_env(os.environ, prov), args)


def read_key(flag_key, key_env):
    if flag_key:
        return flag_key
    if key_env:
        return os.environ.get(key_env, "")
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass("API key (input hidden): ").strip()
    return sys.stdin.readline().strip()


def provider_add(args):
    name, url, key, key_env, api_key, force, codex, wire = "", "", "", "", False, False, False, "responses"
    models, extra = [], []
    flags = {"--base-url": "url", "--key": "key", "--key-env": "key_env", "--model": "model", "--env": "env",
             "--wire-api": "wire"}
    i = 0
    while i < len(args):
        a = args[i]
        if a in flags:
            if i + 1 >= len(args):
                err("zorua provider add: %s needs a value" % a)
                return 1
            v = args[i + 1]
            if flags[a] == "url":
                url = v
            elif flags[a] == "key":
                key = v
            elif flags[a] == "key_env":
                key_env = v
            elif flags[a] == "model":
                models.append(v)
            elif flags[a] == "wire":
                wire = v
            else:
                extra.append(v)
            i += 2
            continue
        if a == "--api-key":
            api_key = True
        elif a == "--force":
            force = True
        elif a == "--codex":
            codex = True
        elif a.startswith("-"):
            err("zorua provider add: unknown flag %s" % a)
            return 1
        elif not name:
            name = a
        else:
            err("zorua provider add: unexpected argument %s" % a)
            return 1
        i += 1
    if not name or not url:
        err("usage: zorua provider add <name> --base-url URL [--key KEY | --key-env VAR] [--api-key]\n"
            "                          [--model ROLE=ID]... [--env VAR=VALUE]... [--force]\n"
            "       ROLE: %s\n"
            "       zorua provider add <name> --codex --base-url URL --model ID [--wire-api responses]\n"
            "                          [--key KEY | --key-env VAR] [--force]" % ", ".join(zp.MODEL_VARS))
        return 1
    if not valid_name(name):
        err("zorua: name must match [A-Za-z0-9_-] and not collide with a command: %s" % name)
        return 1
    existing = kind_of(name)
    if existing and not (is_provider(existing) and force):
        err("zorua: '%s' already exists (%s)%s" % (name, existing, "; use --force to replace" if is_provider(existing) else ""))
        return 1
    if not url.startswith(("http://", "https://")):
        err("zorua: --base-url must start with http:// or https://")
        return 1
    if codex and (api_key or extra or len(models) != 1):
        err("zorua provider add --codex: give exactly one --model ID; --api-key and --env are Claude-only")
        return 1
    key = read_key(key, key_env)
    if not key:
        err("zorua: no API key given")
        return 1
    try:
        prov = zp.build_codex(url, key, models[0], wire) if codex else {"env": zp.build_env(url, key, api_key, models, extra)}
    except ValueError as e:
        err("zorua provider add: %s" % e)
        return 1
    provs = zp.load(CONFIG_DIR)
    zp.add_models(prov, [prov["model"]] if codex else zp.env_models(prov["env"]))
    provs[name] = zp.store_key(name, prov, zp.secret(prov))
    zp.save(CONFIG_DIR, provs)
    print("added %s provider: %s -> %s  (zorua use %s, or: zorua %s)" % (zp.agent(prov), name, zp.endpoint(prov), name, name))
    return 0


def provider_rm(args):
    name = args[0] if args else ""
    provs = zp.load(CONFIG_DIR)
    if name not in provs:
        err("zorua: unknown provider '%s' (providers: %s)" % (name, " ".join(provs) or "none"))
        return 1
    if os.environ.get(VARS[AGENT_PROVIDER[zp.agent(provs[name])]]) == name:
        err("zorua: '%s' is active in this shell; run 'zorua use -' first" % name)
        return 1
    del provs[name]
    zp.save(CONFIG_DIR, provs)
    if os.path.exists(BINDING_FILE):
        write_tsv(BINDING_FILE, [(n, p) for n, p in read_tsv(BINDING_FILE) if n != name])
    try:
        os.remove(os.path.join(CONFIG_DIR, "run", "%s.settings.json" % name))
    except OSError:
        pass
    remove_tags(name)
    print("removed provider: %s" % name)
    return 0


def provider_ls():
    provs = zp.load(CONFIG_DIR)
    if not provs:
        print("(no providers yet — zorua provider add <name> --base-url URL, or: zorua provider import cc-switch)")
        return 0
    print("  %-14s %-7s %-30s %-10s %s" % ("NAME", "AGENT", "ENDPOINT", "KEY", "MODELS"))
    for n, p in sorted(provs.items(), key=lambda kv: (zp.agent(kv[1]), kv[0])):
        kind = AGENT_PROVIDER[zp.agent(p)]
        mark = "●" if os.environ.get(VARS[kind]) == n else "◆" if os.environ.get(AUTO_VARS[kind][0]) == n else " "
        print("%s %-14s %-7s %-30s %-10s %s" % (mark, n, zp.agent(p), zp.endpoint(p),
                                             zp.mask(zp.secret(p)) if zp.secret(p) else "-", zp.model_summary(p)))
    return 0


def provider_show(args):
    provs = zp.load(CONFIG_DIR)
    name = args[0] if args else ""
    if name not in provs:
        err("zorua: unknown provider '%s' (providers: %s)" % (name, " ".join(provs) or "none"))
        return 1
    p = provs[name]
    print("agent=%s" % zp.agent(p))
    print("key_storage=%s" % zp.key_storage(p))
    items = p["env"] if zp.agent(p) == "claude" else {k: v for k, v in p.items() if k != "kind"}
    for var, val in sorted((k, v) for k, v in items.items() if k not in ("models", "key_ref", "key_var")):
        print("%s=%s" % (var, zp.shown(var, val)))
    for alias, mid in zp.models(p).items():
        print("model %s = %s" % (alias, mid))
    return 0


def provider_get(args):
    """JSON view for front ends; the legacy --reveal flag is always masked too."""
    names = [a for a in args if not a.startswith("-")]
    provs = zp.load(CONFIG_DIR)
    if len(names) != 1 or names[0] not in provs or set(args) - set(names) - {"--reveal"}:
        err("usage: zorua provider get <name> [--reveal]   (providers: %s)" % (" ".join(provs) or "none"))
        return 1
    print(json.dumps(zp.document(provs[names[0]], "--reveal" in args), ensure_ascii=False))
    return 0


def provider_put(args):
    """Replace one provider with the JSON document on stdin (the shape `provider get` prints)."""
    provs = zp.load(CONFIG_DIR)
    name = args[0] if len(args) == 1 else ""
    if name not in provs:
        err("usage: zorua provider put <name> < document.json   (providers: %s)" % (" ".join(provs) or "none"))
        return 1
    try:
        doc = json.loads(sys.stdin.read())
        old = zp.resolve_key(name, provs[name])
        new = zp.from_document(old, doc)
    except ValueError as e:
        err("zorua provider put: %s" % e)
        return 1
    zp.backup(CONFIG_DIR)
    provs[name] = zp.store_key(name, new, zp.secret(new))
    zp.save(CONFIG_DIR, provs)
    print("updated provider: %s -> %s" % (name, zp.endpoint(new)))
    return 0


def provider_check(args):
    """Is the endpoint reachable and the key accepted? One name prints that provider's result; no name
    (or several) checks all of them (those) in parallel. --json prints the result(s) and exits 0 (for front ends)."""
    names = [a for a in args if not a.startswith("-")]
    provs = zp.load(CONFIG_DIR)
    if any(n not in provs for n in names) or set(args) - set(names) - {"--json"}:
        err("usage: zorua provider check [name ...] [--json]   (providers: %s)" % (" ".join(provs) or "none"))
        return 1
    as_json = "--json" in args
    if len(names) == 1:
        res = zp.check_stored(names[0], provs[names[0]])
        res["checked_at"] = int(time.time())
        if as_json:
            print(json.dumps(res, ensure_ascii=False))
            return 0
        print("%s  %s  %dms  via %s%s" % (res["status"], res["detail"], res["ms"], res["via"],
                                         "" if res["http"] is None else "  (HTTP %d)" % res["http"]))
        return 1 if res["status"] == "fail" else 0
    order = sorted(dict.fromkeys(names) or provs, key=lambda n: (zp.agent(provs[n]), n))
    if not order:
        print("{}" if as_json else "(no providers yet — zorua provider add <name> --base-url URL, or: zorua provider import cc-switch)")
        return 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {n: ex.submit(zp.check_stored, n, provs[n]) for n in order}
        results = {n: dict(futs[n].result(), checked_at=int(time.time())) for n in order}
    if as_json:
        print(json.dumps(results, ensure_ascii=False))
        return 0
    mark = {"ok": "✓", "warn": "!", "fail": "✗"}
    print("  %-14s %-7s %-6s %7s  %s" % ("NAME", "AGENT", "STATUS", "TIME", "DETAIL"))
    for n, res in results.items():
        print("%s %-14s %-7s %-6s %5dms  %s" % (mark[res["status"]], n, zp.agent(provs[n]), res["status"],
                                              res["ms"], res["detail"]))
    bad = sum(r["status"] == "fail" for r in results.values())
    if bad:
        sys.stdout.flush()
        err("%d of %d provider(s) failed; details: zorua provider check <name>" % (bad, len(results)))
    return 1 if bad else 0


def provider_models(args):
    provs = zp.load(CONFIG_DIR)
    name = args[0] if args else ""
    if name not in provs:
        err("usage: zorua provider models <provider> [add <model-id> [alias] | rm <alias> | fetch]")
        return 1
    p = provs[name]
    action, rest = (args[1] if len(args) > 1 else "ls"), args[2:]
    if action in ("ls", "list"):
        cat = zp.models(p)
        if not cat:
            print("(no models yet — zorua provider models %s add <model-id> [alias], or: fetch)" % name)
        for alias, mid in cat.items():
            print("  %-24s %s" % (alias, mid))
        return 0
    if action == "add":
        if not rest:
            err("usage: zorua provider models %s add <model-id> [alias]" % name)
            return 1
        mid, alias = rest[0], (rest[1] if len(rest) > 1 else "")
        cat = zp.models(p)
        if alias:
            if not re.match(r"^[A-Za-z0-9_.-]+$", alias) or alias in cat:
                err("zorua: alias must be new and match [A-Za-z0-9_.-]: %s" % alias)
                return 1
            cat[alias] = mid
            p["models"] = cat
        elif not zp.add_models(p, [mid]):
            err("zorua: model already listed: %s" % mid)
            return 1
    elif action == "rm":
        cat = zp.models(p)
        if not rest or rest[0] not in cat:
            err("zorua: unknown alias '%s' (models: %s)" % (rest[0] if rest else "", " ".join(cat) or "none"))
            return 1
        del cat[rest[0]]
        p["models"] = cat
    elif action == "fetch":
        try:
            ids = zp.fetch_models(zp.resolve_key(name, p))
        except RuntimeError as e:
            err("zorua: could not fetch models: %s" % e)
            return 1
        n = zp.add_models(p, ids)
        print("%s offers %d model(s); %d new" % (name, len(ids), n))
    else:
        err("usage: zorua provider models <provider> [add <model-id> [alias] | rm <alias> | fetch]")
        return 1
    zp.save(CONFIG_DIR, provs)
    return provider_models([name])


def provider_import(args):
    if not args or args[0] != "cc-switch":
        err("usage: zorua provider import cc-switch [--dry-run] [--force] [--db PATH]")
        return 1
    dry, force, db = False, False, os.path.join(HOME, ".cc-switch", "cc-switch.db")
    rest = args[1:]
    i = 0
    while i < len(rest):
        if rest[i] == "--dry-run":
            dry = True
        elif rest[i] == "--force":
            force = True
        elif rest[i] == "--db" and i + 1 < len(rest):
            db = rest[i + 1]
            i += 1
        else:
            err("zorua provider import: unknown argument %s" % rest[i])
            return 1
        i += 1
    if not os.path.exists(db):
        err("zorua: cc-switch database not found: %s" % db)
        return 1
    try:
        found = [(d, {"env": e}) for d, e in zp.cc_switch_providers(db)] + zp.cc_switch_codex_providers(db)
        for _, prov in found:
            if zp.agent(prov) == "claude":
                zp.add_models(prov, zp.env_models(prov["env"]))
    except Exception as e:
        err("zorua: cannot read %s: %s" % (db, e))
        return 1
    provs = zp.load(CONFIG_DIR)
    added = 0
    imported = []
    for display, prov in found:
        name = zp.sanitize_name(display)
        if not valid_name(name):
            name += "-provider"
        if zp.agent(prov) == "codex" and name in provs and zp.agent(provs[name]) == "claude":
            name += "-codex"          # cc-switch lets both agents share a name; Zorua names are unique
        taken = "claude_provider" if name in provs and zp.agent(provs[name]) == "claude" else \
                "codex_provider" if name in provs else kind_of(name)
        if taken and not (is_provider(taken) and force):
            print("skip   %-14s already exists (%s)%s" % (name, taken, "; --force to replace" if is_provider(taken) else ""))
            continue
        print("%-6s %-14s %-7s %s  %s" % ("would" if dry else "import", name, zp.agent(prov), zp.endpoint(prov),
                                          zp.mask(zp.secret(prov))))
        provs[name] = prov        # in memory even for --dry-run, so later entries see earlier names
        added += 1
        imported.append(name)
    if added and not dry:
        for name in dict.fromkeys(imported):
            p = provs[name]
            provs[name] = zp.store_key(name, p, zp.secret(p))
        zp.save(CONFIG_DIR, provs)
    if not found:
        print("no custom providers with their own key found in cc-switch")
    elif not dry:
        print("imported %d provider(s); cc-switch was only read, nothing in it changed" % added)
    return 0


def provider_set_key(args):
    provs = zp.load(CONFIG_DIR)
    if len(args) != 1 or args[0] not in provs:
        err("usage: zorua provider set-key <name>")
        return 1
    name = args[0]
    key = read_key("", "")
    if not key:
        err("zorua: no API key given")
        return 1
    provs[name] = zp.store_key(name, provs[name], key)
    zp.save(CONFIG_DIR, provs)
    print("updated key: %s (%s)" % (name, zp.key_storage(provs[name])))
    return provider_check([name])


def provider_migrate_keys(args):
    if args not in ([], ["--dry-run"]):
        err("usage: zorua provider migrate-keys [--dry-run]")
        return 1
    names = zp.migrate_keys(CONFIG_DIR, "--dry-run" in args)
    for name in names:
        print("%s %s -> macOS Keychain" % ("would migrate" if args else "migrated", name))
    if not names:
        print("no plaintext provider keys to migrate")
    return 0


def cmd_provider(args):
    sub = args[0] if args else "ls"
    rest = args[1:]
    if sub in ("ls", "list"):
        return provider_ls()
    if sub == "add":
        return provider_add(rest)
    if sub in ("rm", "remove"):
        return provider_rm(rest)
    if sub == "show":
        return provider_show(rest)
    if sub == "set-key":
        return provider_set_key(rest)
    if sub == "migrate-keys":
        return provider_migrate_keys(rest)
    if sub == "check":
        return provider_check(rest)
    if sub == "get":
        return provider_get(rest)
    if sub == "put":
        return provider_put(rest)
    if sub in ("models", "model"):
        return provider_models(rest)
    if sub == "import":
        return provider_import(rest)
    err("usage: zorua provider [ls | add <name> --base-url URL ... | show <name> | get <name> | put <name> | check [name ...] | rm <name> | models <name> ... | import cc-switch]")
    return 1


def cmd_binds():
    rows = read_tsv(BINDING_FILE)
    if not rows:
        print("(no project bindings yet — run 'zorua bind <name>' inside a project directory)")
        return 0
    pwd = cwd()
    here = {binding_for(pwd, k) for k in KINDS} - {""}
    print(" account       project directory (* active here)")
    for n, p in rows:
        active = n in here and (pwd == p or pwd.startswith(p.rstrip("/") + "/"))
        tag = {"claude": "  (claude)", "claude_provider": "  (claude provider)",
               "codex_provider": "  (codex provider)"}.get(kind_of(n), "")
        print(" %s%-12s %s%s" % ("* " if active else "  ", n, p, tag))
    return 0


def hook_script():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "zorua_statusline.py")


def _shell_path(path):
    """Quote a path for the status-line command; keep it portable as "$HOME/..." when under HOME."""
    if path.startswith(HOME + "/"):
        return '"$HOME/%s"' % path[len(HOME) + 1:].replace('"', '\\"')
    return shlex.quote(path)


def wrap_status_command(orig):
    """Status-line command that relays through zorua_statusline.py but falls back to
    `orig` if the relay (or python3) is missing, e.g. on another machine or after
    zorua was uninstalled, so the status line never breaks."""
    script = _shell_path(hook_script())
    guard = "[ -f %s ] && command -v python3 >/dev/null 2>&1" % script
    if not orig:
        return "if %s; then python3 %s; fi" % (guard, script)
    return ("__zorua_orig=%s; if %s; then python3 %s -- \"$__zorua_orig\"; else sh -c \"$__zorua_orig\"; fi"
            % (shlex.quote(orig), guard, script))


def unwrap_status_command(cmd):
    """If `cmd` is our relay, return the wrapped original command ('' if none); else None.

    Understands the current format and the first (0.3.1) one: python3 <script> -- <orig>.
    """
    for prefix in ("__zorua_orig=", "__cx_orig="):
        if cmd.startswith(prefix):
            try:
                tok = shlex.split(cmd)[0]
            except (ValueError, IndexError):
                return None
            tok = tok[len(prefix):]
            return tok[:-1] if tok.endswith(";") else tok
    if cmd.startswith("if [ -f ") and ("zorua_statusline.py" in cmd or "cx_statusline.py" in cmd) and cmd.rstrip().endswith("; fi"):
        return ""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return None
    if len(parts) >= 2 and os.path.basename(parts[1]) in ("zorua_statusline.py", "cx_statusline.py"):
        rest = parts[2:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        return rest[0] if rest else ""
    return None


def shadow_files(name, with_cwd=True):
    """Project settings that define a status line of their own for Claude account `name`.

    Claude Code gives <dir>/.claude/settings{,.local}.json priority over the account's own
    settings.json for sessions started in <dir>, so a status line there hides the relay. Looks in
    $HOME, the current directory and the directories bound to the account. -> [{"file", "dir",
    "command", "relay"}], where relay says the file already goes through the relay.
    """
    home = os.path.realpath(registry("claude")[name])
    dirs = [HOME] + ([os.getcwd()] if with_cwd else []) + [d for n, d in read_tsv(BINDING_FILE) if n == name]
    seen, out = set(), []
    for d in dirs:
        for fn in ("settings.local.json", "settings.json"):
            p = os.path.realpath(os.path.join(d, ".claude", fn))
            if p in seen or os.path.dirname(p) == home:
                continue
            seen.add(p)
            try:
                with open(p) as f:
                    sl = json.load(f).get("statusLine")
            except Exception:
                continue
            cmd = (sl.get("command") or "") if isinstance(sl, dict) else ""
            if cmd.strip():
                out.append({"file": p, "dir": os.path.dirname(os.path.dirname(p)), "command": cmd,
                            "relay": unwrap_status_command(cmd) is not None})
    return out


def hook_one(action, name, dry=False, yes=False, path=None):
    """Install/remove/status the relay in the account's settings.json, or in `path` (a shadowing file)."""
    home = registry("claude")[name]
    path = path or os.path.realpath(os.path.join(home, "settings.json"))
    try:
        with open(path) as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}
    except ValueError as e:
        err("zorua: cannot parse %s: %s" % (path, e))
        return 1
    sl = settings.get("statusLine") if isinstance(settings.get("statusLine"), dict) else {}
    cur = sl.get("command", "") or ""
    orig = unwrap_status_command(cur)
    cache = claude_usage_cache(home)

    if action == "status":
        print("settings: %s" % path)
        print("relay:    %s" % ("installed" if orig is not None else "not installed"))
        print("wraps:    %s" % ((orig or "(nothing)") if orig is not None else (cur or "(no status line configured)")))
        if cache and cache.get("updated_at"):
            print("cache:    updated %ds ago" % int(time.time() - cache["updated_at"]))
        else:
            print("cache:    none yet (use claude once with the relay installed)")
        for s in shadow_files(name):
            if s["relay"]:
                print("also in:  %s (sessions started in %s)" % (s["file"], s["dir"]))
            else:
                print("SHADOWED: %s defines its own status line, which hides the relay in sessions started in %s" % (s["file"], s["dir"]))
                print("          fix: zorua hook install %s --shadows   (wraps that status line too; a backup is made)" % name)
        return 0

    if action == "install":
        if orig is not None:
            new_cmd = wrap_status_command(orig)
            if new_cmd == cur:
                print("already installed for %s" % name)
                return 0
            print("updating the relay command for %s to the current (portable, self-healing) form" % name)
            new_sl = dict(sl, type="command", command=new_cmd)
        else:
            new_cmd = wrap_status_command(cur)
            new_sl = dict(sl, type="command", command=new_cmd)
        if orig is None and not cur:
            print("note: '%s' has no status line. Installing adds a minimal one (model, context, 5h/7d)." % name)
            print("      Claude Code hides most footer keyboard hints (esc to interrupt, ? for shortcuts)")
            print("      while any status line is configured. 'zorua hook remove %s' undoes this." % name)
    else:
        if orig is None:
            print("not installed for %s" % name)
            return 0
        new_sl = dict(sl)
        if orig:
            new_sl["command"] = orig
        else:
            new_sl = None

    print("%s: %s" % (path, "statusLine.command"))
    print("  - %s" % (cur or "(none)"))
    print("  + %s" % ((new_sl or {}).get("command") or "(statusLine removed)"))
    if dry:
        print("(dry run, nothing written)")
        return 0
    if action == "install" and not cur and not yes:
        if not sys.stdin.isatty():
            err("zorua: refusing to add a status line without confirmation (re-run with --yes)")
            return 1
        if not confirm("Add a minimal status line to '%s'?" % name, "n"):
            print("cancelled")
            return 1
    import shutil
    if os.path.exists(path):
        backup = "%s.zorua-bak-%d" % (path, int(time.time()))
        shutil.copy2(path, backup)
        print("backup: %s" % backup)
    if new_sl is None:
        settings.pop("statusLine", None)
    else:
        settings["statusLine"] = new_sl
    tmp = path + ".zorua.tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if os.path.exists(path):
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
    print("%s for %s" % ("installed" if action == "install" else "removed", name))
    return 0


def cmd_hook(args):
    """zorua hook install|remove|status [<claude account>|--all] [--dry-run] [--yes]"""
    usage = "usage: zorua hook install|remove <claude-account> [--shadows] [--dry-run] [--yes] | zorua hook remove --all | zorua hook status [<claude-account>]"
    dry, yes, every, shadows = "--dry-run" in args, "--yes" in args, "--all" in args, "--shadows" in args
    args = [a for a in args if a not in ("--dry-run", "--yes", "--all", "--shadows")]
    if not args or args[0] not in ("install", "remove", "status", "refresh") or len(args) > 2:
        err(usage)
        return 1
    action = args[0]
    names = [n for n, _ in claude_accounts()]
    if action == "refresh":
        # Re-write the relay command of accounts that already use it (after an upgrade or move).
        for name in names:
            if not hook_installed(name):
                continue
            home = registry("claude")[name]
            try:
                with open(os.path.realpath(os.path.join(home, "settings.json"))) as f:
                    cur = (json.load(f).get("statusLine") or {}).get("command", "") or ""
            except Exception:
                continue
            if cur != wrap_status_command(unwrap_status_command(cur) or ""):
                print("[%s]" % name)
                hook_one("install", name, False, True)
        return 0
    if len(args) == 1:
        if action == "status" or (action == "remove" and every):
            targets = names
        else:
            err(usage)
            return 1
    else:
        if kind_of(args[1]) != "claude":
            err("zorua: '%s' is not a Claude account (zorua add --claude <name>)" % args[1])
            return 1
        targets = [args[1]]
    rc = 0
    for i, name in enumerate(targets):
        if len(args) == 1:
            print("[%s]" % name)
        if action == "remove" and every and not hook_installed(name):
            print("not installed")
            continue
        rc = hook_one(action, name, dry, yes) or rc
        if action in ("install", "remove") and rc == 0:
            # Status lines in project settings outrank the account's, so the relay must be there too.
            for s in shadow_files(name):
                if (action == "install") == (not s["relay"]):
                    if shadows:
                        print("[%s: %s]" % (name, s["file"]))
                        rc = hook_one(action, name, dry, yes, s["file"]) or rc
                    elif action == "install":
                        print("warning: %s defines its own status line, which hides the relay in sessions started in %s;"
                              " run 'zorua hook install %s --shadows' to wrap it too" % (s["file"], s["dir"], name))
    if not targets:
        print("(no Claude accounts)")
    return rc


def hook_installed(name):
    """True when the account's status-line command is our relay."""
    home = registry("claude")[name]
    try:
        with open(os.path.realpath(os.path.join(home, "settings.json"))) as f:
            sl = json.load(f).get("statusLine")
    except Exception:
        return False
    return isinstance(sl, dict) and unwrap_status_command(sl.get("command", "") or "") is not None


HELP = """  zorua                         list accounts, emails, plan and subscription expiry
  zorua setup                   interactive first-run wizard (adopt homes, sign in, add, bind)
  zorua usage [-v]              live limits (5h/7d bars); -v = detailed blocks (also: zorua ls -v)
                                add --expiry (or ZORUA_EXPIRY=1) to show Codex subscription end dates;
                                hidden by default because they come from a cached token and can be stale
  zorua tag <name> [+tag ...] [-tag ...]  view/edit account or provider labels
  zorua pick --kind codex|claude [--needs tags] [--avoid tags] [--model ID-or-prefix] [--json]
                                recommend by remaining 7d then 5h quota; shared emails use one pool
  zorua doctor [name ...] [--json]  read-only login, permissions, proxy, hooks and provider checks
  zorua use <name>              switch this shell to <name> (sets CODEX_HOME or CLAUDE_CONFIG_DIR)
  zorua use -                   switch this shell back to default
  zorua <name> [args]           one-shot: run codex (or claude, for a Claude account) under that
                                account, e.g.  zorua work exec "..."
  zorua login <name>            run codex login (or claude auth login) for one account
  zorua off                     clear the switch
  zorua off providers           clear only the providers, keep the accounts
  zorua add <name>              create a Codex account (new CODEX_HOME + sign-in)
                                [--home DIR] [--no-login] [--device-auth]
  zorua add --claude <name>     create a Claude Code subscription account (own
                                CLAUDE_CONFIG_DIR ~/.claude-<name>; sign-in via claude auth login)
                                new accounts start from <config dir>/claude-settings.json if present
  zorua rm <name> [--purge]     unregister (keeps data unless confirmed/--purge)
  zorua provider add <name> --base-url URL [--key K | --key-env VAR] [--api-key]
                                [--model ROLE=ID]... [--env VAR=VALUE]...
                                add a third-party Claude Code endpoint (key is prompted if omitted)
  zorua provider add <name> --codex --base-url URL --model ID [--wire-api responses]
                                add a third-party Codex endpoint
  zorua provider set-key <name> rotate a key (hidden input or stdin), then check the endpoint
  zorua provider migrate-keys [--dry-run]
                                back up providers.json (0600), then move plaintext keys to Keychain
  zorua provider ls|show|rm     list / show (keys masked) / remove providers
  zorua provider models <name> [add <model-id> [alias] | rm <alias> | fetch]
                                a provider can hold many models; fetch asks its endpoint for the list
  zorua use <provider>:<model>  switch provider and pick a model of it in one go (alias, id or prefix)
  zorua model [alias | -]       list / pick / clear the model of the active provider
  zorua provider import cc-switch [--dry-run] [--force]
                                copy custom Claude and Codex providers out of cc-switch (read-only)
  zorua use <provider>          this shell's plain `claude` (or `codex`) runs on the provider; one
                                provider per agent can be active at once. Also: zorua <provider>
                                [args] and zorua bind
  zorua bind [name]             bind current directory (default: current account)
  zorua unbind [dir]            remove a directory binding (default: current dir)
  zorua binds                   list project bindings
  zorua hook install <claude>   relay Claude Code's status-line rate_limits into a cache so
                                zorua usage can show 5h/7d (also: hook remove <name>|--all,
                                hook status [name], hook refresh, --dry-run; an account with no
                                status line needs --yes or a confirmation). A status line in
                                <dir>/.claude/settings.json hides the relay in sessions started in
                                <dir>; hook status says so and --shadows wraps that one too
  zorua prompt                  print the prompt marker (also in $ZORUA_PROMPT_TEXT)
  zorua version                 print Zorua version
"""


def cmd_help():
    print("Zorua — parallel multi-account manager for Codex CLI and Claude Code (command: zorua)")
    print(HELP)
    print("  accounts: %s" % " ".join(homes()))
    print("  files:    %s" % ACCOUNT_FILE)
    print("            %s" % BINDING_FILE)
    return 0


def cmd_setup(st):
    print("\033[1m Zorua setup\033[0m" if sys.stdout.isatty() else " Zorua setup")
    print()
    import shutil
    codex = shutil.which("codex")
    print(" ✓ codex found: %s" % codex if codex else
          " ! codex not found on PATH — install the Codex CLI first (accounts can still be registered)")
    print(" ✓ python3 %s (this tool's core)" % sys.version.split()[0])
    print()

    h = homes()
    registered = set(h.values())
    try:
        entries = sorted(os.listdir(HOME))
    except OSError:
        entries = []
    for e in entries:
        d = os.path.join(HOME, e)
        if not (e.startswith(".codex-") and os.path.isdir(d) and os.path.isfile(os.path.join(d, "auth.json"))):
            continue
        if d in registered:
            continue
        name = e[len(".codex-"):]
        if not valid_name(name) or name in homes():
            print(" - skipping %s: '%s' is not a usable account name (register it with: zorua add <name> --home %s --no-login)"
                  % (short(d), name, short(d)))
            continue
        if confirm(" Found signed-in home %s (%s). Register as '%s'?" % (short(d), account_email(d), name), "y"):
            write_tsv(ACCOUNT_FILE, accounts() + [(name, d)])

    for n, p in accounts():
        if os.path.isfile(os.path.join(p, "auth.json")):
            continue
        if confirm(" Account '%s' is not signed in. Sign in now?" % n, "y"):
            dev = confirm("   Use the headless device-code flow instead of the browser?", "n")
            run_codex_login(p, dev)

    while confirm(" Add a new account?", "n"):
        name = ask("   Account name (letters, digits, - _): ")
        if not name:
            break
        dev = confirm("   Use the headless device-code flow?", "n")
        cmd_add([name] + (["--device-auth"] if dev else []))

    names = [n for n, _ in accounts()]
    if cwd() != HOME and len(names) > 1:
        print()
        name = ask(" Bind %s to an account (%s), or Enter to skip: " % (short(cwd()), "/".join(names)))
        if name:
            cmd_bind([name], st)

    print()
    render(False, False)
    if confirm(" Show live plan limits now (zorua usage)?", "n"):
        render(True, False)
    print()
    print(" Done. Try:  zorua use <name> && codex     (zorua help for everything)")
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv):
    if argv[:1] == ["doctor"]:
        return cmd_doctor(argv[1:])
    init_registry()
    st = State()
    sub = argv[0] if argv else "ls"
    rest = argv[1:]
    rc = 0
    if sub in ("ls", "list", "status"):
        rc = cmd_ls(rest)
    elif sub == "usage":
        rc = cmd_ls(rest, usage=True)
    elif sub == "tag":
        rc = cmd_tag(rest)
    elif sub == "pick":
        rc = cmd_pick(rest)
    elif sub == "use":
        rc = cmd_use(rest, st)
    elif sub in ("off", "reset", "unset"):
        rc = cmd_off(st, rest)
    elif sub == "login":
        rc = cmd_login(rest)
    elif sub == "setup":
        rc = cmd_setup(st)
    elif sub == "add":
        rc = cmd_add(rest)
    elif sub == "rm":
        rc = cmd_rm(rest)
    elif sub == "bind":
        rc = cmd_bind(rest, st)
    elif sub == "unbind":
        rc = cmd_unbind(rest, st)
    elif sub == "binds":
        rc = cmd_binds()
    elif sub in ("version", "-v", "--version"):
        print("Zorua %s" % VERSION)
    elif sub in ("help", "-h", "--help"):
        rc = cmd_help()
    elif sub == "hook":
        rc = cmd_hook(rest)
    elif sub == "provider":
        rc = cmd_provider(rest)
    elif sub == "model":
        rc = cmd_model(rest, st)
    elif sub == "launch":
        if not rest or rest[0] not in zp.AGENTS:
            err("usage: zorua launch claude|codex [args]   (used by the claude / codex shell functions)")
            return 1
        fn = launch_claude if rest[0] == "claude" else launch_codex
        kind = AGENT_PROVIDER[rest[0]]
        return fn(os.environ.get(VARS[kind], ""), rest[1:], os.environ.get(MODEL_VAR[kind], ""))
    elif sub == "prompt":
        print(st.prompt_info()[2])
    elif sub == "apply":
        apply_binding(st, rest[0] if rest else cwd())
    elif sub == "names":
        print("\n".join(all_names()))
    else:
        sub, _, msel = sub.partition(":")
        kind = kind_of(sub)
        if msel and not is_provider(kind):
            err("zorua: only providers have models; '%s' is not a provider" % sub)
            return 1
        if is_provider(kind):
            prov = zp.load(CONFIG_DIR)[sub]
            if msel and zp.resolve_model(prov, msel) is None:
                err("zorua: unknown model '%s' for %s (models: %s)" % (msel, sub, " ".join(zp.models(prov)) or "none"))
                return 1
            fn = launch_claude if PROVIDER_AGENT[kind] == "claude" else launch_codex
            return fn(sub, rest, msel)
        if kind:
            home = registry(kind)[sub]
            EMIT.flush()
            _flush_before_exec()
            if kind == "claude":
                env, argv = claude_clean_env(home), ["claude"] + rest
            else:
                env, argv = dict(os.environ, CODEX_HOME=home), ["codex"] + rest
            try:
                os.execvpe(argv[0], argv, env)
            except FileNotFoundError:
                err("zorua: %s not found on PATH" % argv[0])
                return 127
        err("zorua: unknown command/account '%s' (zorua help)" % sub)
        rc = 1
    EMIT.flush()
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (RuntimeError, OSError) as e:
        err("zorua: %s" % e)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
