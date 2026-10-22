#!/usr/bin/env fish
# Zorua smoke test for the fish wrapper — runs inside a temporary HOME.
#
#   fish tests/smoke.fish

set -l root (dirname (dirname (realpath (status filename))))
set -l tmp (mktemp -d)
set -gx HOME $tmp/home
set -gx XDG_CONFIG_HOME $tmp/config
set -gx ZORUA_CONFIG_DIR $tmp/config/zorua
set -e ZORUA_TEST_KEYCHAIN
mkdir -p $tmp/safety-bin
cp $root/tests/fake-security $tmp/safety-bin/security
chmod +x $tmp/safety-bin/security
set -gx PATH $tmp/safety-bin $PATH
set -e CODEX_HOME
set -e GROK_HOME
set -e GROK_AUTH
set -e GROK_AUTH_PATH
mkdir -p $HOME

set -g n 0
function ok
    set -g n (math $n + 1)
    echo "ok $argv"
end
function die
    echo "FAIL $argv" >&2
    rm -rf $tmp
    exit 1
end
function contains_str
    # contains_str <haystack> <needle> <label>
    string match -q -- "*$argv[2]*" $argv[1]; or die "$argv[3] (expected '$argv[2]')"
end

set -l jwt (python3 -c '
import base64, json
b = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
claims = {"email": "work@example.com", "https://api.openai.com/auth": {
    "chatgpt_plan_type": "pro", "chatgpt_subscription_active_until": "2030-01-02T00:00:00+00:00"}}
print(b({"alg": "none"}) + "." + b(claims) + ".sig")')
mkdir -p $HOME/.codex-work
printf '{"tokens":{"id_token":"%s"}}' $jwt > $HOME/.codex-work/auth.json

source $root/zorua.fish

set -l out (zorua ls | string collect)
contains_str $out "w***k@example.com" "discovers signed-in home"
string match -q "*work@example.com*" -- $out; and die "email should be masked by default"
contains_str (zorua ls --full | string collect) work@example.com "ls --full shows the whole email"
string match -q "*2030-01-02*" -- $out; and die "expiry should be hidden by default"
contains_str (zorua ls --expiry | string collect) 2030-01-02 "expiry column on request"
ok "first run + ls"

contains_str (zorua version | string collect) "Zorua" version
ok version

set -l side $tmp/side-home
mkdir $side
zorua add side --no-login --home $side >/dev/null
contains_str (zorua ls | string collect) side "add registers"
zorua add side 2>/dev/null; and die "duplicate add should fail"
zorua use side >/dev/null
test "$CODEX_HOME" = "$side"; or die "use did not set CODEX_HOME ($CODEX_HOME)"
test "$ZORUA_PROMPT_TEXT" = "[codex:side]"; or die "prompt text: $ZORUA_PROMPT_TEXT"
zorua use - >/dev/null
test -z "$CODEX_HOME"; or die "use - did not clear"
ok "add / use / prompt marker"

set -l proj $tmp/proj/sub/deeper
mkdir -p $proj
cd $tmp/proj
zorua bind side >/dev/null
test "$CODEX_HOME" = "$side"; or die "bind did not apply"
cd $HOME
test -z "$CODEX_HOME"; or die "leaving bound dir did not restore ($CODEX_HOME)"
cd $proj
test "$CODEX_HOME" = "$side"; or die "entering subdir did not switch"
test "$ZORUA_AUTO_ACTIVE" = side; or die "auto state"
cd $HOME
begin
    test -z "$CODEX_HOME"; and test -z "$ZORUA_AUTO_ACTIVE"
end; or die "state not cleared on leave"
ok "bind + cd hook"

zorua rm side </dev/null >/dev/null
test -d $side; or die "rm without --purge must keep data"
zorua add side --no-login --home $side >/dev/null
zorua rm side --purge >/dev/null
test -d $side; and die "--purge must delete"
ok "rm / --purge"

mkdir $tmp/bin
printf '#!/bin/sh\necho "FAKE_HOME=$CODEX_HOME"\necho "KEY=${ZORUA_CODEX_KEY:-unset}"\necho "ARGS=$*"\n' > $tmp/bin/codex
chmod +x $tmp/bin/codex
set -l one (begin; set -lx PATH $tmp/bin $PATH; fish -c "source $root/zorua.fish; zorua work hello-world"; end | string collect)
contains_str $one "FAKE_HOME=$HOME/.codex-work" "one-shot home"
contains_str $one "ARGS=hello-world" "one-shot args"
ok one-shot

mkdir $tmp/cbin
cp $root/tests/fake-claude $tmp/cbin/claude
set -l oldpath $PATH
set -gx PATH $tmp/cbin $PATH
zorua add --claude alt >/dev/null; or die "add --claude failed"
contains_str (zorua ls | string collect) "cl***e@example.com" "claude account listed"
zorua use alt >/dev/null 2>&1
test "$CLAUDE_CONFIG_DIR" = "$HOME/.claude-alt"; or die "use alt did not set CLAUDE_CONFIG_DIR ($CLAUDE_CONFIG_DIR)"
test "$ZORUA_PROMPT_TEXT" = "[claude:alt]"; or die "prompt text: $ZORUA_PROMPT_TEXT"
zorua use - >/dev/null
test -z "$CLAUDE_CONFIG_DIR"; or die "use - did not clear CLAUDE_CONFIG_DIR"
zorua rm alt --purge >/dev/null
set -gx PATH $oldpath
ok "claude accounts"

# ---- grok accounts (fake grok; its token is expired, so usage never calls out) ----
mkdir $tmp/gbin
cp $root/tests/fake-grok $tmp/gbin/grok
set -l gpath $PATH
set -gx PATH $tmp/gbin $PATH
zorua add --grok gx >/dev/null; or die "add --grok failed"
test -f $HOME/.grok-gx/auth.json; or die "add --grok must sign in through grok login"
contains_str (zorua ls | string collect) "gro***r@example.com" "grok account listed"
contains_str (zorua usage | string collect) "token expired (run grok once to refresh)" "usage asks for grok instead of calling out"
contains_str (zorua usage -r | string collect) "7D LEFT" "usage -r labels the remaining columns"
zorua usage mode remaining >/dev/null
contains_str (zorua usage mode | string collect) "remaining" "usage mode prints the saved default"
zorua usage mode used >/dev/null
zorua use gx >/dev/null 2>&1
test "$GROK_HOME" = "$HOME/.grok-gx"; or die "use gx did not set GROK_HOME ($GROK_HOME)"
test "$ZORUA_PROMPT_TEXT" = "[grok:gx]"; or die "prompt text: $ZORUA_PROMPT_TEXT"
zorua use - >/dev/null
test -z "$GROK_HOME"; or die "use - did not clear GROK_HOME"
mkdir -p $tmp/gproj
cd $tmp/gproj
zorua bind gx >/dev/null
test "$GROK_HOME" = "$HOME/.grok-gx" -a "$ZORUA_AUTO_GROK" = gx; or die "grok bind did not apply"
printf '' > $XDG_CONFIG_HOME/zorua/bindings.tsv    # no bindings left: only ZORUA_AUTO_GROK makes the hook run
cd $HOME
test -z "$GROK_HOME" -a -z "$ZORUA_AUTO_GROK"; or die "leaving must restore GROK_HOME ($GROK_HOME)"
zorua rm gx --purge >/dev/null
test -d $HOME/.grok-gx; and die "rm --purge must delete the grok home"
set -gx PATH $gpath
ok "grok accounts"

# ---- providers ----------------------------------------------------------------
mkdir -p $HOME/.claude
echo '{"env":{"ANTHROPIC_BASE_URL":"http://127.0.0.1:1","ANTHROPIC_AUTH_TOKEN":"PROXY_MANAGED","ANTHROPIC_DEFAULT_SONNET_MODEL":"internal","CLAUDE_CODE_MAX_OUTPUT_TOKENS":"64000"},"permissions":{"deny":["Bash(rm:*)"]}}' > $HOME/.claude/settings.json
set -gx PATH $tmp/cbin $PATH
zorua provider add glm --base-url https://api.example.com/anthropic/ --key sk-glm-secret-123456 --model sonnet=glm-4.6 >/dev/null; or die "provider add failed"
echo sk-kimi-secret-123456 | zorua provider add kimi --base-url https://kimi.example.com --api-key --model default=k3 >/dev/null; or die "provider add (stdin key) failed"
zorua provider add glm --base-url https://x.example.com --key k 2>/dev/null; and die "duplicate provider should fail"
zorua provider add ls --base-url https://x.example.com --key k 2>/dev/null; and die "reserved name should fail"
set -l listing (begin; zorua provider ls; zorua provider show glm; zorua; end | string collect)
contains_str $listing api.example.com "provider endpoint shown"
string match -q "*sk-glm-secret-123456*" -- $listing; and die "key leaked in listing"
zorua use glm >/dev/null
test "$ZORUA_CLAUDE_PROVIDER" = glm; or die "use glm did not set ZORUA_CLAUDE_PROVIDER"
test "$ZORUA_PROMPT_TEXT" = "[claude-provider:glm]"; or die "prompt text: $ZORUA_PROMPT_TEXT"
set -l pout (claude hi | string collect)
contains_str $pout "TOKEN=sk-glm-secret-123456" "provider key reaches claude"
contains_str $pout "BASE=https://api.example.com/anthropic" "provider base url reaches claude"
contains_str $pout "SETTINGS_PERMS=600" "settings file is private"
contains_str $pout '"ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-4.6"' "provider model beats settings.json"
zorua use kimi >/dev/null
set pout (claude hi | string collect)
contains_str $pout '"ANTHROPIC_API_KEY": "sk-kimi-secret-123456"' "api-key style provider"
contains_str $pout '"ANTHROPIC_AUTH_TOKEN": ""' "conflicting token is blanked"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert list(d)==["env"], list(d)' $XDG_CONFIG_HOME/zorua/run/kimi.settings.json; or die "generated settings must only contain env"
string match -q "*MAX_OUTPUT_TOKENS*" -- (cat $XDG_CONFIG_HOME/zorua/run/kimi.settings.json | string collect); and die "unrelated user env must be left alone"
zorua use - >/dev/null
test -z "$ZORUA_CLAUDE_PROVIDER"; or die "use - did not clear ZORUA_CLAUDE_PROVIDER"
string match -q "*--settings*" -- (claude plain | string collect); and die "claude must be untouched without a provider"
mkdir -p $tmp/pproj
cd $tmp/pproj
zorua bind glm >/dev/null
test "$ZORUA_CLAUDE_PROVIDER" = glm -a "$ZORUA_AUTO_CLAUDE_PROVIDER" = glm; or die "provider bind did not apply"
cd $tmp
test -z "$ZORUA_CLAUDE_PROVIDER"; or die "leaving the bound directory did not restore"
cd $tmp/pproj
test "$ZORUA_CLAUDE_PROVIDER" = glm; or die "re-entering the bound directory did not switch"
zorua unbind $tmp/pproj >/dev/null
cd $tmp
zorua use glm >/dev/null
zorua provider rm glm 2>/dev/null; and die "active provider must not be removable"
zorua use - >/dev/null
zorua provider rm glm >/dev/null; or die "provider rm failed"
string match -q "*glm *" -- (zorua provider ls | string collect); and die "provider rm left the entry"
set -gx PATH $oldpath
ok "providers: add, use, claude launch, bind, rm"

# ---- codex providers: one slot per agent ----------------------------------------
set -gx PATH $tmp/bin $PATH
echo sk-ds-secret-123456 | zorua provider add ds --codex --base-url https://api.deepseek.com --model deepseek-v4-flash >/dev/null; or die "codex provider add failed"
zorua provider add ds2 --codex --base-url https://x.example.com --key k 2>/dev/null; and die "codex provider without --model should fail"
zorua provider add ds2 --codex --api-key --base-url https://x.example.com --model m --key k 2>/dev/null; and die "--api-key is Claude-only"
set -l plist (begin; zorua provider ls; zorua provider show ds; end | string collect)
string match -q "*sk-ds-secret-123456*" -- $plist; and die "codex key leaked in listing"
zorua use kimi >/dev/null; zorua use ds >/dev/null
test "$ZORUA_CODEX_PROVIDER" = ds -a "$ZORUA_CLAUDE_PROVIDER" = kimi; or die "claude and codex providers must be independent"
contains_str $ZORUA_PROMPT_TEXT "codex-provider:ds" "codex provider marker"
contains_str $ZORUA_PROMPT_TEXT "claude-provider:kimi" "claude provider marker"
set -l cout (codex exec hi | string collect)
contains_str $cout "KEY=sk-ds-secret-123456" "codex key travels in the environment"
contains_str $cout 'model_provider="zorua_ds"' "codex provider selected via -c"
contains_str $cout 'model="deepseek-v4-flash"' "codex model via -c"
string match -q "*ARGS=*sk-ds-secret*" -- $cout; and die "key must not appear in argv"
zorua use - >/dev/null
test -z "$ZORUA_CODEX_PROVIDER" -a -z "$ZORUA_CLAUDE_PROVIDER"; or die "use - must clear both provider slots"
string match -q "*model_provider*" -- (codex exec hi | string collect); and die "codex must be untouched without a provider"
zorua provider rm ds >/dev/null; or die "codex provider rm failed"
ok "codex providers: independent slot, env key, -c overrides"
set -gx PATH $oldpath

# ---- provider models: many models per provider, one picked per shell -----------
set -gx PATH $tmp/cbin $tmp/bin $PATH
echo sk-rel-secret-123456 | zorua provider add rel --base-url https://relay.example.com --model default=m/default --model opus="m/opus[1M]" >/dev/null; or die "provider add (models) failed"
contains_str (zorua provider models rel | string collect) "m/opus[1M]" "ids with a [1M] suffix are kept verbatim"
zorua provider models rel add vendor/extra-model extra >/dev/null; or die "models add failed"
zorua provider models rel add vendor/x extra 2>/dev/null; and die "duplicate alias should fail"
contains_str (zorua provider ls | string collect) "3 models" "model count in the provider list"
zorua use rel:extra >/dev/null
test "$ZORUA_CLAUDE_PROVIDER" = rel -a "$ZORUA_CLAUDE_MODEL" = extra; or die "use <provider>:<model> did not set both"
contains_str $ZORUA_PROMPT_TEXT "claude-provider:rel/extra" "model in the prompt marker"
contains_str (claude hi | string collect) '"ANTHROPIC_MODEL": "vendor/extra-model"' "picked model reaches claude"
zorua use rel:nope 2>/dev/null; and die "unknown model should fail"
test "$ZORUA_CLAUDE_MODEL" = extra; or die "a failed pick must not change the selection"
zorua model - >/dev/null
test -z "$ZORUA_CLAUDE_MODEL"; or die "model - did not clear the pick"
contains_str (claude hi | string collect) '"ANTHROPIC_MODEL": "m/default"' "provider's own default without a pick"
zorua use rel:extra >/dev/null
echo sk-oth-secret-123456 | zorua provider add oth --base-url https://o.example.com --model default=o/m >/dev/null
zorua use oth >/dev/null
test -z "$ZORUA_CLAUDE_MODEL"; or die "switching provider must drop the previous model pick"
echo sk-dx-secret-123456 | zorua provider add dx --codex --base-url https://api.dx.example.com --model dx-flash >/dev/null
zorua provider models dx add dx-pro pro >/dev/null
zorua use dx:pro >/dev/null
contains_str (codex exec hi | string collect) 'model="dx-pro"' "codex picked model"
zorua use - >/dev/null
set -gx PATH $oldpath
ok "provider models: catalog and pick per shell"

rm -rf $tmp
echo "All $n fish smoke checks passed."
