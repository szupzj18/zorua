#!/usr/bin/env zsh
# Zorua smoke test — runs entirely inside a temporary HOME.
# It never touches the real ~/.codex*, Zorua registry, or invokes the real codex.
#
#   zsh tests/smoke.zsh

emulate -L zsh
set -e
setopt pipe_fail

ROOT="${0:A:h:h}"
SCRIPT="$ROOT/zorua.zsh"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export HOME="$TMP/home"
export ZDOTDIR="$HOME"
export XDG_CONFIG_HOME="$TMP/config"
mkdir -p "$HOME"
export ZORUA_CONFIG_DIR="$XDG_CONFIG_HOME/zorua"
unset ZORUA_TEST_KEYCHAIN GROK_HOME ZORUA_AUTO_GROK GROK_AUTH GROK_AUTH_PATH
mkdir -p "$TMP/safety-bin"
cp "$ROOT/tests/fake-security" "$TMP/safety-bin/security"
chmod +x "$TMP/safety-bin/security"
export PATH="$TMP/safety-bin:$PATH"

n=0
ok() { n=$((n + 1)); print -P -- "%F{green}ok%f $1"; }
die() { print -P -- "%F{red}FAIL%f $1" >&2; exit 1; }
contains() {
  print -rn -- "$1" | grep -Fq -- "$2" || die "$3
expected substring: $2
--- got ---
$1"
}
not_contains() {
  ! print -rn -- "$1" | grep -Fq -- "$2" || die "$3
unexpected substring: $2
--- got ---
$1"
}
fails() { "$@" >/dev/null 2>&1 || return 0; die "expected failure but it succeeded: $*"; }

# ---- 1. Build two signed-in homes before sourcing (first-run discovery) ----

mkjwt() {
  python3 - "$1" <<'PY'
import base64, json, sys
def b64(o):
    return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
print(b64({"alg": "none"}) + "." + b64({"email": sys.argv[1]}) + ".sig")
PY
}

mkdir "$HOME/.codex-work" "$HOME/.codex-3" "$HOME/.codex-bad name"
AUTH_FMT='{"tokens":{"id_token":"%s"}}'
print -f "$AUTH_FMT" "$(mkjwt work@example.com)" > "$HOME/.codex-work/auth.json"
print -f "$AUTH_FMT" "$(mkjwt three@example.com)" > "$HOME/.codex-3/auth.json"
# ~/.codex-bad name has no auth.json AND an invalid account name; skip both ways

# ---- 2. Source under temp HOME: seeds default + discovers work/3 -------------

source "$SCRIPT"
out=$(zorua ls)
contains "$out" "default" "first-run seeding"
contains "$out" "work" "first-run discovery"
contains "$out" "3" "first-run numeric names"
contains "$out" "w***k@example.com" "JWT email decode (masked)"
contains "$out" "th***e@example.com" "JWT email decode (second account)"
not_contains "$out" "work@example.com" "emails masked by default"
contains "$(zorua ls --full)" "work@example.com" "ls --full shows the whole email"
not_contains "$out" "bad name" "invalid names skipped by discovery"
ok "first-run seeds default and discovers valid homes"

python3 - "$HOME/.codex-work/auth.json" <<'PY'
import json, sys, base64
b = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
claims = {"email": "work@example.com", "https://api.openai.com/auth": {
    "chatgpt_plan_type": "pro", "chatgpt_subscription_active_until": "2030-01-02T00:00:00+00:00"}}
json.dump({"tokens": {"id_token": b({"alg": "none"}) + "." + b(claims) + ".sig"}}, open(sys.argv[1], "w"))
PY
out=$(zorua ls)
contains "$out" "pro" "plan decode"
not_contains "$out" "2030-01-02" "expiry is hidden by default"
contains "$(zorua ls --expiry)" "2030-01-02" "--expiry shows the subscription end date"
contains "$(ZORUA_EXPIRY=1 zorua ls)" "2030-01-02" "ZORUA_EXPIRY=1 shows it too"
not_contains "$out" $'\033' "no ANSI colors when not a TTY"
out=$(zorua ls -v)
contains "$out" "~/.codex-work" "verbose view shows home paths"
not_contains "$out" "2030-01-02" "verbose view hides expiry by default"
contains "$(zorua ls -v --expiry)" "exp 2030-01-02" "verbose view shows expiry on request"
out=$(ZORUA_COLOR=always zorua ls)
contains "$out" $'\033[' "ZORUA_COLOR=always forces colors"
ok "zorua ls shows plan; expiry only on request"

# ---- 3. add / use ------------------------------------------------------------

fails zorua add "bad name"
fails zorua add work
SIDE="$TMP/side-home"
mkdir "$SIDE"
fails zorua add dup --no-login --home "$HOME/.codex-work"   # already registered
zorua add side --no-login --home "$SIDE"
out=$(zorua ls)
contains "$out" "side" "add registers new account"

zorua use side
[[ $CODEX_HOME == $SIDE ]] || die "zorua use side did not set CODEX_HOME"
zorua use -
[[ -z ${CODEX_HOME:-} ]] || die "zorua use - did not clear CODEX_HOME"
ok "zorua add / zorua use works"

# ---- 4. bind in non-interactive shell (direct apply on bind) -----------------

PROJ="$TMP/proj/sub/deeper"
mkdir -p "$PROJ"
cd "$TMP/proj"
zorua bind side
[[ $CODEX_HOME == $SIDE ]] || die "bind did not apply immediately"
print -rn -- "$RPROMPT" | grep -q "codex:side:auto" || die "auto marker missing: $RPROMPT"
ok "bind applies immediately with auto marker"

# ---- 5. chpwd auto-switch tested in a real interactive child shell ----------

cd "$HOME"
child=$(env -u CODEX_HOME ZDOTDIR="$HOME" HOME="$HOME" XDG_CONFIG_HOME="$TMP/config" \
  zsh -ic "
    source '$SCRIPT'
    whence -w _zorua | grep -q function || { echo FAIL_COMPLETION; exit 1 }
    cd '$PROJ'
    [[ \$CODEX_HOME == '$SIDE' ]] || { echo FAIL_ENTER; exit 1 }
    print -rn -- \"\$RPROMPT\" | grep -q 'codex:side:auto' || { echo FAIL_MARKER; exit 1 }
    cd '$TMP/proj'
    [[ \$CODEX_HOME == '$SIDE' ]] || { echo FAIL_PREFIX; exit 1 }
    cd '$HOME'
    [[ -z \${CODEX_HOME:-} ]] || { echo FAIL_LEAVE; exit 1 }
    [[ -z \$ZORUA_AUTO_ACTIVE ]] || { echo FAIL_LEAVE_STATE; exit 1 }
    echo CHPWD_OK
  " 2>&1)
contains "$child" "CHPWD_OK" "interactive chpwd auto-switch
$child"
ok "chpwd switches on cd in, keeps on prefix match, restores on leave"

# ---- 6. binds / unbind -------------------------------------------------------

cd "$PROJ"
out=$(zorua binds)
contains "$out" "side" "binds lists account"
contains "$out" "$TMP/proj" "binds lists path"
zorua unbind "$TMP/proj"
[[ -z ${CODEX_HOME:-} ]] || die "unbind did not restore CODEX_HOME"
ok "binds / unbind works"

# ---- 7. rm keeps data by default, then --purge deletes -----------------------

cd "$HOME"
echo n | zorua rm side >/dev/null
[[ -d $SIDE ]] || die "rm should keep data directory without --purge"
out=$(zorua ls)
not_contains "$out" "side" "rm unregisters"
zorua add side --no-login --home "$SIDE"
zorua rm side --purge
[[ ! -d $SIDE ]] || die "--purge did not delete data directory"
ok "rm keeps data then --purge deletes"
fails zorua rm default
ok "default account is protected"

# ---- 8. one-shot invocation through a fake codex -----------------------------

mkdir "$TMP/bin"
cat > "$TMP/bin/codex" <<'SH'
#!/bin/sh
echo "FAKE_CODEX_HOME=$CODEX_HOME"
echo "KEY=${ZORUA_CODEX_KEY:-unset}"
echo "ARGS=$*"
SH
chmod +x "$TMP/bin/codex"
out=$(PATH="$TMP/bin:$PATH" zorua work hello-world)
contains "$out" "FAKE_CODEX_HOME=$HOME/.codex-work" "one-shot sets CODEX_HOME"
contains "$out" "ARGS=hello-world" "one-shot forwards arguments"
print -rn -- "$out" | grep -qx "FAKE_CODEX_HOME=$HOME/.codex-work" \
  || die "CODEX_HOME was not the only/expected value
$out"
ok "one-shot zorua <account> invokes codex with correct CODEX_HOME and args"

# ---- 10. setup wizard (scripted answers) --------------------------------------

mkdir -p "$HOME/.codex-adopt"
print -r -- '{"tokens":{"id_token":"x.eyJlbWFpbCI6ImFkb3B0QGV4YW1wbGUuY29tIn0.s"}}' > "$HOME/.codex-adopt/auth.json"
# answers: register adopt=y, sign in default=n, add another=n, show usage=n
out=$(printf 'y\nn\nn\nn\n' | zorua setup 2>&1)
contains "$out" "Zorua setup" "setup banner"
contains "$(<"$XDG_CONFIG_HOME/zorua/accounts.tsv")" "adopt	$HOME/.codex-adopt" "setup registers discovered home"
out=$(zorua setup </dev/null 2>&1) || die "setup must not fail on EOF"
ok "zorua setup adopts existing homes and survives EOF"

# ---- 11. claude account through the zsh wrapper -------------------------------

mkdir -p "$TMP/cbin"
cp "$ROOT/tests/fake-claude" "$TMP/cbin/claude"
PATH="$TMP/cbin:$PATH" zorua add --claude alt >/dev/null
contains "$(PATH="$TMP/cbin:$PATH" zorua ls)" "cl***e@example.com" "claude account listed"
zorua use alt >/dev/null 2>&1
[[ $CLAUDE_CONFIG_DIR == $HOME/.claude-alt ]] || die "zorua use alt did not set CLAUDE_CONFIG_DIR"
print -rn -- "$RPROMPT" | grep -q "claude:alt" || die "claude marker missing: $RPROMPT"
zorua use - >/dev/null
[[ -z ${CLAUDE_CONFIG_DIR:-} ]] || die "zorua use - did not clear CLAUDE_CONFIG_DIR"
zorua rm alt --purge >/dev/null
ok "claude accounts work through the zsh wrapper"

# claude-settings.json template seeds new Claude accounts and never overwrites
TPL="$XDG_CONFIG_HOME/zorua/claude-settings.json"
print -r -- '{"env":{"HTTPS_PROXY":"http://127.0.0.1:1"}}' > $TPL
PATH="$TMP/cbin:$PATH" zorua add --claude seeded >/dev/null
cmp -s $TPL $HOME/.claude-seeded/settings.json || die "template not copied into new claude account"
mkdir -p $HOME/.claude-kept && print -r -- '{"keep":1}' > $HOME/.claude-kept/settings.json
PATH="$TMP/cbin:$PATH" zorua add --claude kept >/dev/null
[[ "$(<$HOME/.claude-kept/settings.json)" == '{"keep":1}' ]] || die "existing settings.json must not be overwritten"
rm -f $TPL
PATH="$TMP/cbin:$PATH" zorua add --claude plain >/dev/null
[[ ! -e $HOME/.claude-plain/settings.json ]] || die "no template: no settings.json must be created"
zorua rm seeded --purge >/dev/null; zorua rm kept --purge >/dev/null; zorua rm plain --purge >/dev/null
ok "add --claude seeds settings.json from claude-settings.json template"

# ---- 11b. grok account through the zsh wrapper (fake grok; its token is expired, so no network) ----

mkdir -p "$TMP/gbin"
cp "$ROOT/tests/fake-grok" "$TMP/gbin/grok"
PATH="$TMP/gbin:$PATH" zorua add --grok gx >/dev/null
[[ -f $HOME/.grok-gx/auth.json ]] || die "add --grok must sign in through grok login under GROK_HOME"
out=$(zorua ls)
contains "$out" "gro***r@example.com" "grok account listed with a masked email"
contains "$out" " Grok" "grok accounts get their own section"
contains "$(zorua usage)" "token expired (run grok once to refresh)" "usage asks for grok instead of calling out"
contains "$(zorua usage -r)" "7D LEFT" "usage -r labels the remaining columns"
zorua usage mode remaining >/dev/null
contains "$(zorua usage mode)" "remaining" "usage mode prints the saved default"
contains "$(zorua usage)" "7D LEFT" "saved default applies"
zorua usage mode used >/dev/null
zorua use gx >/dev/null 2>&1
[[ $GROK_HOME == $HOME/.grok-gx ]] || die "zorua use gx did not set GROK_HOME"
print -rn -- "$RPROMPT" | grep -q "grok:gx" || die "grok marker missing: $RPROMPT"
out=$(GROK_AUTH_PATH=/elsewhere PATH="$TMP/gbin:$PATH" zorua gx -p hi)
contains "$out" "FAKE_GROK_HOME=$HOME/.grok-gx" "one-shot sets GROK_HOME"
contains "$out" "GROK_AUTH_PATH=unset" "one-shot drops GROK_AUTH_PATH"
contains "$out" "ARGS=-p hi" "one-shot forwards arguments"
zorua use - >/dev/null
[[ -z ${GROK_HOME:-} ]] || die "zorua use - did not clear GROK_HOME"
mkdir -p "$TMP/gproj"; cd "$TMP/gproj"
zorua bind gx >/dev/null
[[ $GROK_HOME == $HOME/.grok-gx && $ZORUA_AUTO_GROK == gx ]] || die "grok bind did not apply"
: > "$XDG_CONFIG_HOME/zorua/bindings.tsv"      # no bindings left: only ZORUA_AUTO_GROK makes the hook run
cd "$HOME"; _zorua_apply_binding
[[ -z ${GROK_HOME:-} && -z $ZORUA_AUTO_GROK ]] || die "the cd hook must restore GROK_HOME after an auto switch"
zorua rm gx --purge >/dev/null
[[ ! -d $HOME/.grok-gx ]] || die "rm --purge must delete the grok home"
ok "grok accounts work through the zsh wrapper"

# ---- 11c. agy: one global sign-in, shown only (the token file is never read) ----

[[ $(zorua ls) != *Antigravity* ]] || die "no agy section before agy was set up"
mkdir -p "$HOME/.gemini/antigravity-cli"; : > "$HOME/.gemini/antigravity-cli/antigravity-oauth-token"
out=$(zorua ls)
contains "$out" "agy   (current sign-in)  –" "agy row shows the current sign-in"
contains "$out" "zorua shows it but does not switch it" "agy note says it is not switched"
contains "$(zorua usage)" "agy   –     –   –   –" "agy usage is all –"
ok "agy sign-in is listed without being switched"

# ---- 12. providers ---------------------------------------------------------------

mkdir -p "$HOME/.claude"
print -r -- '{"env":{"ANTHROPIC_BASE_URL":"http://127.0.0.1:1","ANTHROPIC_AUTH_TOKEN":"PROXY_MANAGED","ANTHROPIC_DEFAULT_SONNET_MODEL":"internal","CLAUDE_CODE_MAX_OUTPUT_TOKENS":"64000"},"permissions":{"deny":["Bash(rm:*)"]}}' > "$HOME/.claude/settings.json"
out=$(zorua provider add glm --base-url https://api.example.com/anthropic/ --key sk-glm-secret-123456 --model sonnet=glm-4.6)
contains "$out" "added claude provider: glm" "provider add"
echo sk-kimi-secret-123456 | zorua provider add kimi --base-url https://kimi.example.com --api-key --model default=k3 >/dev/null
fails zorua provider add glm --base-url https://x.example.com --key k
fails zorua provider add ls --base-url https://x.example.com --key k
fails zorua provider add bad --base-url ftp://x.example.com --key k
fails zorua provider add bad --base-url https://x.example.com --key k --model nope=x
[[ $(stat -c %a "$XDG_CONFIG_HOME/zorua/providers.json" 2>/dev/null || stat -f %Lp "$XDG_CONFIG_HOME/zorua/providers.json") == 600 ]] || die "providers.json must be 0600"
out="$(zorua provider ls)$(zorua provider show glm)$(zorua)"
not_contains "$out" "sk-glm-secret-123456" "keys are masked in ls/show/list"
contains "$out" "api.example.com" "provider endpoint shown"
zorua use glm >/dev/null
[[ $ZORUA_CLAUDE_PROVIDER == glm ]] || die "use glm did not set ZORUA_CLAUDE_PROVIDER"
print -rn -- "$RPROMPT" | grep -q "claude-provider:glm" || die "provider marker missing: $RPROMPT"
out=$(PATH="$TMP/cbin:$PATH" claude hi)
contains "$out" "TOKEN=sk-glm-secret-123456" "provider key reaches claude"
contains "$out" "BASE=https://api.example.com/anthropic" "provider base url reaches claude"
contains "$out" "ARGS=--settings" "claude is launched with --settings"
contains "$out" "SETTINGS_PERMS=600" "settings file is private"
contains "$out" '"ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-4.6"' "provider model beats settings.json"
zorua use kimi >/dev/null
out=$(PATH="$TMP/cbin:$PATH" claude hi)
contains "$out" '"ANTHROPIC_API_KEY": "sk-kimi-secret-123456"' "api-key style provider"
contains "$out" '"ANTHROPIC_AUTH_TOKEN": ""' "conflicting token from settings.json is blanked"
contains "$out" '"ANTHROPIC_DEFAULT_SONNET_MODEL": ""' "conflicting model from settings.json is blanked"
# only provider-related keys are overridden: the generated settings hold nothing but an env block,
# and unrelated user variables are neither copied nor blanked
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert list(d)==["env"], list(d)' "$XDG_CONFIG_HOME/zorua/run/kimi.settings.json" || die "generated settings must only contain env"
not_contains "$(cat "$XDG_CONFIG_HOME/zorua/run/kimi.settings.json")" "MAX_OUTPUT_TOKENS" "unrelated user env is left alone"
out=$(PATH="$TMP/cbin:$PATH" zorua glm oneshot)
contains "$out" "ARGS=--settings" "one-shot provider launch"
contains "$out" "oneshot" "one-shot passes arguments"
zorua use - >/dev/null
[[ -z ${ZORUA_CLAUDE_PROVIDER:-} ]] || die "use - did not clear ZORUA_CLAUDE_PROVIDER"
out=$(PATH="$TMP/cbin:$PATH" claude plain)
not_contains "$out" "--settings" "claude is untouched without a provider"
mkdir -p "$TMP/pproj"; cd "$TMP/pproj"
zorua bind glm >/dev/null
[[ $ZORUA_CLAUDE_PROVIDER == glm && $ZORUA_AUTO_CLAUDE_PROVIDER == glm ]] || die "provider bind did not apply"
cd "$TMP"; zorua apply "$PWD"
[[ -z ${ZORUA_CLAUDE_PROVIDER:-} ]] || die "leaving the bound directory did not restore"
zorua apply "$TMP/pproj"
[[ $ZORUA_CLAUDE_PROVIDER == glm ]] || die "re-entering the bound directory did not switch"
zorua unbind "$TMP/pproj" >/dev/null
zorua use glm >/dev/null
fails zorua provider rm glm
zorua use - >/dev/null
zorua provider rm glm >/dev/null
contains "$(zorua provider ls)" "kimi" "other providers survive rm"
not_contains "$(zorua provider ls)" "glm " "provider rm"
# provider get / put: the JSON view the web dashboard edits
out=$(zorua provider get kimi)
contains "$out" '"agent": "claude"' "provider get: agent"
not_contains "$out" "sk-kimi-secret-123456" "provider get masks the key"
not_contains "$(zorua provider get kimi --reveal)" "sk-kimi-secret-123456" "provider get --reveal"
zorua provider get kimi | zorua provider put kimi >/dev/null
not_contains "$(zorua provider get kimi --reveal)" "sk-kimi-secret-123456" "a masked document keeps the stored key"
zorua provider get kimi --reveal | sed 's#"ANTHROPIC_BASE_URL": "[^"]*"#"ANTHROPIC_BASE_URL": "https://kimi2.example.com"#' | zorua provider put kimi >/dev/null
contains "$(zorua provider show kimi)" "kimi2.example.com" "provider put changes the base URL"
[[ -f "$XDG_CONFIG_HOME/zorua/providers.json.bak" ]] || die "provider put must keep providers.json.bak"
print -r -- '{"agent":"claude","env":{"ANTHROPIC_BASE_URL":"ftp://x","ANTHROPIC_AUTH_TOKEN":"abcdefghijklmnop"}}' | fails zorua provider put kimi
print -r -- '{"agent":"codex"}' | fails zorua provider put kimi
fails zorua provider get nope
ok "providers: get / put"
ok "providers: add, use, claude launch, bind, rm"

python3 - "$TMP/ccswitch.db" <<'PY'
import json, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, settings_config TEXT, sort_index INTEGER)")
rows = [
  ("a", "claude", "Super Relay", {"env": {"ANTHROPIC_BASE_URL": "https://relay.example.com", "ANTHROPIC_AUTH_TOKEN": "tok-relay-123456789", "ANTHROPIC_MODEL": "m1"}}),
  ("b", "claude", "Claude Official", {"env": {}}),
  ("c", "claude", "Proxied", {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:15721", "ANTHROPIC_AUTH_TOKEN": "PROXY_MANAGED"}}),
  ("g", "claude", "DeepSeek", {"env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic", "ANTHROPIC_AUTH_TOKEN": "tok-dsc-123456789"}}),
  ("e", "codex", "DeepSeek", {"auth": {"OPENAI_API_KEY": "tok-ds-123456789"}, "config": 'model_provider = "custom"\nmodel = "ds-flash"\n\n[model_providers.custom]\nname = "deepseek"\nbase_url = "https://api.deepseek.com"\nwire_api = "responses"\n'}),
  ("f", "codex", "OpenAI Official", {"auth": {"OPENAI_API_KEY": None}, "config": ""}),
  ("d", "pi", "pi-only", {"env": {"ANTHROPIC_BASE_URL": "https://pi.example.com", "ANTHROPIC_AUTH_TOKEN": "tok-pi-123456789"}}),
]
for i, (id_, app, name, cfg) in enumerate(rows):
    con.execute("INSERT INTO providers VALUES (?,?,?,?,?)", (id_, app, name, json.dumps(cfg), i))
con.commit()
PY
out=$(zorua provider import cc-switch --db "$TMP/ccswitch.db" --dry-run)
contains "$out" "super-relay" "import lists the custom claude provider"
contains "$out" "deepseek" "import lists the custom codex provider"
not_contains "$out" "OpenAI" "official codex entry skipped"
not_contains "$out" "tok-ds-123456789" "codex import never prints keys"
not_contains "$out" "Proxied" "proxy-managed entries skipped"
not_contains "$out" "pi-only" "other apps skipped"
not_contains "$out" "tok-relay-123456789" "import never prints keys"
[[ -z $(zorua provider ls | grep super-relay) ]] || die "dry run must not write"
zorua provider import cc-switch --db "$TMP/ccswitch.db" >/dev/null
contains "$(zorua provider show super-relay)" "ANTHROPIC_MODEL=m1" "imported env is kept"
contains "$(zorua provider show deepseek-codex)" "model=ds-flash" "imported codex provider is kept (renamed on a name clash)"
contains "$(zorua provider models deepseek-codex)" "ds-flash" "imported codex provider has its model catalog"
contains "$(zorua provider models super-relay)" "m1" "claude catalog is built from the role mapping"
contains "$(zorua provider show deepseek-codex)" "agent=codex" "imported codex agent"
contains "$(zorua provider show deepseek)" "agent=claude" "the claude provider keeps the plain name"
out=$(zorua provider import cc-switch --db "$TMP/ccswitch.db")
contains "$out" "already exists" "import skips existing names"
ok "providers: import from cc-switch"


# ---- codex providers: one slot per agent -------------------------------------------

mkdir -p "$TMP/bin"
echo sk-ds-secret-123456 | zorua provider add ds --codex --base-url https://api.deepseek.com --model deepseek-v4-flash >/dev/null
fails zorua provider add ds2 --codex --base-url https://x.example.com --key k
fails zorua provider add ds2 --codex --api-key --base-url https://x.example.com --model m --key k
out="$(zorua provider ls)$(zorua provider show ds)"
not_contains "$out" "sk-ds-secret-123456" "codex key masked"
contains "$out" "codex" "agent column"
zorua use kimi >/dev/null; zorua use ds >/dev/null
[[ $ZORUA_CODEX_PROVIDER == ds && $ZORUA_CLAUDE_PROVIDER == kimi ]] || die "claude and codex providers must be independent"
print -rn -- "$RPROMPT" | grep -q "codex-provider:ds" || die "codex provider marker missing: $RPROMPT"
print -rn -- "$RPROMPT" | grep -q "claude-provider:kimi" || die "claude provider marker missing: $RPROMPT"
out=$(PATH="$TMP/bin:$PATH" codex exec hi)
contains "$out" "KEY=sk-ds-secret-123456" "codex key travels in the environment"
contains "$out" 'model_provider="zorua_ds"' "codex provider selected via -c"
contains "$out" 'base_url="https://api.deepseek.com"' "codex base url via -c"
contains "$out" 'model="deepseek-v4-flash"' "codex model via -c"
not_contains "$(print -r -- "$out" | grep '^ARGS=')" "sk-ds-secret" "key must not appear in argv"
[[ $(print -r -- "$out" | grep '^ARGS=' | grep -o -- '-c ' | wc -l) -eq 3 ]] || die "codex provider must override exactly model_provider, model_providers.* and model"
contains "$out" "ARGS=-c" "codex flags precede the user's arguments"
out=$(PATH="$TMP/bin:$PATH" zorua ds exec hi)
contains "$out" 'model_provider="zorua_ds"' "one-shot codex provider"
zorua use - >/dev/null
[[ -z ${ZORUA_CODEX_PROVIDER:-} && -z ${ZORUA_CLAUDE_PROVIDER:-} ]] || die "use - must clear both provider slots"
out=$(PATH="$TMP/bin:$PATH" codex exec hi)
not_contains "$out" "model_provider" "codex untouched without a provider"
not_contains "$out" "KEY=sk-" "no key without a provider"
mkdir -p "$TMP/cproj"; cd "$TMP/cproj"
zorua bind ds >/dev/null
[[ $ZORUA_CODEX_PROVIDER == ds && -z ${ZORUA_CLAUDE_PROVIDER:-} ]] || die "codex provider bind should only fill the codex slot"
cd "$TMP"; zorua apply "$PWD"
[[ -z ${ZORUA_CODEX_PROVIDER:-} ]] || die "leaving the bound directory did not restore the codex slot"
zorua unbind "$TMP/cproj" >/dev/null
zorua provider rm ds >/dev/null
ok "codex providers: independent slot, env key, -c overrides, bind"

# ---- provider models: many models per provider, one picked per shell -----------------

mkdir -p "$TMP/bin"
echo sk-rel-secret-123456 | zorua provider add rel --base-url https://relay.example.com --model default=m/default --model opus="m/opus[1M]" >/dev/null
out=$(zorua provider models rel)
contains "$out" "m/default" "models seeded from the role mapping"
contains "$out" "m/opus[1M]" "ids with a [1M] suffix are kept verbatim"
contains "$out" "opus " "short alias derived from the id"
zorua provider models rel add vendor/extra-model extra >/dev/null
zorua provider models rel add vendor/other-model >/dev/null
fails zorua provider models rel add vendor/extra-model
fails zorua provider models rel add vendor/x extra
contains "$(zorua provider ls)" "4 models" "model count in the provider list"
zorua use rel:extra >/dev/null
[[ $ZORUA_CLAUDE_PROVIDER == rel && $ZORUA_CLAUDE_MODEL == extra ]] || die "use <provider>:<model> did not set provider and model"
print -rn -- "$RPROMPT" | grep -q "claude-provider:rel/extra" || die "model missing from the prompt marker: $RPROMPT"
out=$(PATH="$TMP/cbin:$PATH" claude hi)
contains "$out" '"ANTHROPIC_MODEL": "vendor/extra-model"' "picked model reaches claude"
zorua model other >/dev/null           # unambiguous prefix of an alias
[[ $ZORUA_CLAUDE_MODEL == other-model ]] || die "zorua model <prefix> failed: $ZORUA_CLAUDE_MODEL"
out=$(zorua model)
contains "$out" "● other-model" "zorua model marks the picked model"
fails zorua model nope
[[ $ZORUA_CLAUDE_MODEL == other-model ]] || die "a failed pick must not change the selection"
fails zorua use rel:nope
zorua model - >/dev/null
[[ -z ${ZORUA_CLAUDE_MODEL:-} ]] || die "model - did not clear the pick"
out=$(PATH="$TMP/cbin:$PATH" claude hi)
contains "$out" '"ANTHROPIC_MODEL": "m/default"' "without a pick the provider's own default model is used"
out=$(PATH="$TMP/cbin:$PATH" zorua rel:extra hi)
contains "$out" '"ANTHROPIC_MODEL": "vendor/extra-model"' "one-shot provider:model"
fails zorua rel:nope hi
zorua use rel:extra >/dev/null
echo sk-oth-secret-123456 | zorua provider add oth --base-url https://o.example.com --model default=o/m >/dev/null
zorua use oth >/dev/null
[[ -z ${ZORUA_CLAUDE_MODEL:-} ]] || die "switching provider must drop the previous provider's model pick"
zorua provider models rel rm extra >/dev/null
fails zorua use rel:extra
zorua use rel:other >/dev/null; zorua provider models rel rm other-model >/dev/null
out=$(PATH="$TMP/cbin:$PATH" claude hi 2>&1)
contains "$out" "not in this provider's list" "a removed model falls back with a warning"
contains "$out" '"ANTHROPIC_MODEL": "m/default"' "removed pick falls back to the default model"
# codex: the pick becomes the -c model override
echo sk-dx-secret-123456 | zorua provider add dx --codex --base-url https://api.dx.example.com --model dx-flash >/dev/null
zorua provider models dx add dx-pro pro >/dev/null
zorua use dx:pro >/dev/null
out=$(PATH="$TMP/bin:$PATH" codex exec hi)
contains "$out" 'model="dx-pro"' "codex picked model"
not_contains "$out" 'model="dx-flash"' "codex default model is replaced"
zorua model - >/dev/null
out=$(PATH="$TMP/bin:$PATH" codex exec hi)
contains "$out" 'model="dx-flash"' "codex default model without a pick"
# provider default model: persistent (providers.json), marked in the lists, key untouched
zorua provider models dx default pro >/dev/null
contains "$(zorua provider models dx default)" "pro (dx-pro)" "codex default changed"
contains "$(zorua provider models dx)" "dx-pro  (default)" "provider models marks the default"
out=$(PATH="$TMP/bin:$PATH" codex exec hi)
contains "$out" 'model="dx-pro"' "codex launch uses the saved default"
fails zorua provider models dx default -
zorua provider models dx default dx-flash >/dev/null
zorua use rel >/dev/null
zorua provider models rel default fresh/model >/dev/null
contains "$(zorua model)" "fresh/model  (default)" "a new id is listed and becomes the default"
not_contains "$(zorua model)" "m/default  (default)" "only one default is marked"
out=$(PATH="$TMP/cbin:$PATH" claude hi)
contains "$out" '"ANTHROPIC_MODEL": "fresh/model"' "saved default reaches claude"
contains "$out" '"ANTHROPIC_AUTH_TOKEN": "sk-rel-secret-123456"' "changing the default keeps the key"
fails zorua provider models rel default "bad id"
fails zorua provider models rel default fresh/model extra
zorua provider models rel add vendor/one one >/dev/null; zorua provider models rel add vendor/oneplus oneplus >/dev/null
fails zorua provider models rel default on
contains "$(zorua provider models rel default)" "fresh/model" "a failed default change keeps the old one"
zorua model opus --save >/dev/null
[[ $ZORUA_CLAUDE_MODEL == opus ]] || die "model --save must also pick the model in this shell"
contains "$(zorua provider models rel default)" "m/opus[1M]" "model --save changes the provider default"
[[ -s "$XDG_CONFIG_HOME/zorua/providers.json.bak" ]] || die "changing the default must back up providers.json"
fails zorua model - --save
fails zorua model --save
zorua model - >/dev/null
python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["model"]="claude-somewhere-9"; json.dump(d, open(p, "w"))' "$HOME/.claude/settings.json"
out=$(zorua provider models rel default - 2>&1)
contains "$out" "follows its own /model" "default - lets claude follow /model"
contains "$out" "claude-somewhere-9" "warns when the /model choice is not one of the provider's models"
out=$(PATH="$TMP/cbin:$PATH" claude hi)
python3 -c 'import json,sys; e=json.load(open(sys.argv[1]))["env"]; assert not e.get("ANTHROPIC_MODEL"), e' "$XDG_CONFIG_HOME/zorua/run/rel.settings.json" || die "no default must leave ANTHROPIC_MODEL unset"
contains "$(zorua provider models rel default)" "(none" "default shows none"
# provider-level Claude Code settings (e.g. a gateway that rejects a feature) ride along in --settings
python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["providers"]["rel"]["settings"]={"enableArtifact": False, "env": {"X": "1"}}; json.dump(d, open(p, "w"))' "$XDG_CONFIG_HOME/zorua/providers.json"
out=$(PATH="$TMP/cbin:$PATH" claude hi)
contains "$out" '"enableArtifact": false' "provider settings reach claude"
not_contains "$out" '"X": "1"' "a settings env block never replaces the provider env"
contains "$(zorua provider show rel)" "setting enableArtifact = false" "provider show lists its settings"
ok "provider default model and settings"
zorua use - >/dev/null
# fetch: ask the endpoint for its model list (a local stand-in server)
PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')
mkdir -p "$TMP/srv/v1"
print -r -- '{"data":[{"id":"srv/model-a"},{"id":"srv/model-b[1M]"},{"id":"m/default"}]}' > "$TMP/srv/v1/models"
cp "$TMP/srv/v1/models" "$TMP/srv/models"
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$TMP/srv" >/dev/null 2>&1 &
SRV=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:$PORT/models')" 2>/dev/null && break; sleep 0.5; done
echo sk-f-secret-123456 | zorua provider add fet --base-url "http://127.0.0.1:$PORT" --model default=m/default >/dev/null
out=$(zorua provider models fet fetch)
contains "$out" "offers 3 model(s); 2 new" "fetch reports what is new"
contains "$out" "model-a" "fetched model listed"
contains "$(zorua provider models fet)" "srv/model-b[1M]" "fetched ids kept verbatim"
out=$(zorua provider models fet fetch)
contains "$out" "0 new" "fetching again adds nothing"
echo sk-g-secret-123456 | zorua provider add fetx --codex --base-url "http://127.0.0.1:$PORT" --model srv/model-a >/dev/null
contains "$(zorua provider models fetx fetch)" "offers 3 model(s)" "codex fetch uses /models"
zorua provider add bad --base-url "http://127.0.0.1:1" --key k --model default=x >/dev/null
fails zorua provider models bad fetch
contains "$(zorua provider check fet)" "key accepted" "provider check: reachable endpoint"
contains "$(zorua provider check fet --json)" '"status": "ok"' "provider check --json"
contains "$(zorua provider check fetx --json)" '"status": "ok"' "provider check: codex provider"
contains "$(zorua provider check bad --json)" '"status": "fail"' "provider check --json reports failure and exits 0"
fails zorua provider check bad
fails zorua provider check nope
fails zorua provider check fet nope
zorua provider check fet fetx >/dev/null || die "provider check of several healthy providers failed"
out=$(zorua provider check 2>&1) && die "provider check (all) must fail when one provider fails"
contains "$out" "✓ fet " "provider check (all): healthy row"
contains "$out" "✗ bad " "provider check (all): failed row"
contains "$out" "provider(s) failed" "provider check (all): failure summary"
not_contains "$out" "sk-f-secret" "provider check (all) never prints a key"
out=$(zorua provider check --json)
contains "$out" '"fetx": {"status": "ok"' "provider check --json (all): keyed by name"
contains "$out" '"bad": {"status": "fail"' "provider check --json (all): includes failures, exits 0"
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null || true
# Out of credit is a failure, also when a vendor says it with 429 (Moonshot, OpenAI); a plain 429 is not.
out=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import zorua_providers as zp
p = {"env": {"ANTHROPIC_BASE_URL": "https://x", "ANTHROPIC_AUTH_TOKEN": "sk-k-secret-123456"}}
for c, b in ((402, "{\"error\":{\"message\":\"Insufficient Balance\"}}"),
             (429, "{\"error\":{\"message\":\"suspended due to insufficient balance\",\"type\":\"exceeded_current_quota_error\"}}"),
             (429, "{\"error\":{\"message\":\"slow down\"}}")):
    print(c, *zp._verdict(c, b, p))' "$ROOT")
contains "$out" "402 fail out of credit (HTTP 402): Insufficient Balance" "provider check: 402 is out of credit"
contains "$out" "429 fail out of credit (HTTP 429)" "provider check: a quota 429 is out of credit"
contains "$out" "429 warn rate limited" "provider check: a plain 429 is a warning"
ok "provider models: catalog, pick per shell, fetch, check"

# ---- 9. version --------------------------------------------------------------

out=$(zorua version)
contains "$out" "Zorua" "version output"
ok "version command"

print ""
print -P -- "%F{green}All $n smoke checks passed.%f"
