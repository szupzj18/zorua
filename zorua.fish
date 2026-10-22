# Zorua — parallel multi-account manager for the OpenAI Codex CLI (fish wrapper)
# https://github.com/szupzj18/zorua
#
# Thin shell layer: all logic lives in zorua_core.py (next to this file).
# The prompt marker is exposed as $ZORUA_PROMPT_TEXT; add it to your prompt, e.g.
#   function fish_right_prompt; echo $ZORUA_PROMPT_TEXT; end

set -g ZORUA_CORE (test -n "$ZORUA_CORE"; and echo $ZORUA_CORE; or echo (dirname (realpath (status filename)))/zorua_core.py)
set -g ZORUA_AUTO_ACTIVE ""
set -g _ZORUA_PRE_AUTO_HOME ""
set -g ZORUA_AUTO_CLAUDE ""
set -g _ZORUA_PRE_AUTO_CLAUDE ""
set -g ZORUA_AUTO_GROK ""
set -g _ZORUA_PRE_AUTO_GROK ""
set -g ZORUA_AUTO_CLAUDE_PROVIDER ""
set -g _ZORUA_PRE_AUTO_CLAUDE_PROVIDER ""
set -g ZORUA_AUTO_CODEX_PROVIDER ""
set -g _ZORUA_PRE_AUTO_CODEX_PROVIDER ""
set -g ZORUA_PROMPT_KIND ""
set -g ZORUA_PROMPT_NAME ""
set -g ZORUA_PROMPT_TEXT ""
set -g _ZORUA_BINDINGS (test -n "$ZORUA_CONFIG_DIR"; and echo $ZORUA_CONFIG_DIR; or echo (test -n "$XDG_CONFIG_HOME"; and echo $XDG_CONFIG_HOME; or echo $HOME/.config)/zorua)/bindings.tsv

if not command -q python3
    function zorua
        echo "zorua: python3 is required (Zorua core is written in Python 3.8+)" >&2
        return 1
    end
    exit 0
end

function _zorua_run
    set -l f (mktemp)
    env ZORUA_SHELL=fish ZORUA_EVAL_FILE=$f ZORUA_AUTO_ACTIVE=$ZORUA_AUTO_ACTIVE ZORUA_PRE_AUTO_HOME=$_ZORUA_PRE_AUTO_HOME \
        ZORUA_AUTO_CLAUDE=$ZORUA_AUTO_CLAUDE ZORUA_PRE_AUTO_CLAUDE=$_ZORUA_PRE_AUTO_CLAUDE \
        ZORUA_AUTO_GROK=$ZORUA_AUTO_GROK ZORUA_PRE_AUTO_GROK=$_ZORUA_PRE_AUTO_GROK \
        ZORUA_AUTO_CLAUDE_PROVIDER=$ZORUA_AUTO_CLAUDE_PROVIDER ZORUA_PRE_AUTO_CLAUDE_PROVIDER=$_ZORUA_PRE_AUTO_CLAUDE_PROVIDER \
        ZORUA_AUTO_CODEX_PROVIDER=$ZORUA_AUTO_CODEX_PROVIDER ZORUA_PRE_AUTO_CODEX_PROVIDER=$_ZORUA_PRE_AUTO_CODEX_PROVIDER \
        python3 $ZORUA_CORE $argv
    set -l rc $status
    if test -s $f
        source $f
    end
    rm -f $f
    return $rc
end

function zorua
    _zorua_run $argv
end

# While a provider is active (zorua use <provider>), plain `claude` / `codex` runs on it.
function claude
    if test -n "$ZORUA_CLAUDE_PROVIDER"
        env ZORUA_SHELL=fish python3 $ZORUA_CORE launch claude $argv
    else
        command claude $argv
    end
end

function codex
    if test -n "$ZORUA_CODEX_PROVIDER"
        env ZORUA_SHELL=fish python3 $ZORUA_CORE launch codex $argv
    else
        command codex $argv
    end
end

function _zorua_on_pwd --on-variable PWD
    if test -s $_ZORUA_BINDINGS; or test -n "$ZORUA_AUTO_ACTIVE"; or test -n "$ZORUA_AUTO_CLAUDE"; or test -n "$ZORUA_AUTO_GROK"; or test -n "$ZORUA_AUTO_CLAUDE_PROVIDER"; or test -n "$ZORUA_AUTO_CODEX_PROVIDER"
        _zorua_run apply $PWD
    end
end

_zorua_run apply $PWD

complete -c zorua -f
complete -c zorua -n '__fish_use_subcommand' -a 'ls usage pick tag doctor setup use login off add rm bind unbind binds hook provider prompt version help'
complete -c zorua -n '__fish_use_subcommand' -a '(ZORUA_SHELL=fish python3 $ZORUA_CORE names 2>/dev/null)'
complete -c zorua -n '__fish_seen_subcommand_from use login bind rm tag doctor' -a '(ZORUA_SHELL=fish python3 $ZORUA_CORE names 2>/dev/null)'
complete -c zorua -n '__fish_seen_subcommand_from usage' -a '-v -r --remaining --used --expiry --json --full mode'
complete -c zorua -n '__fish_seen_subcommand_from usage; and __fish_seen_subcommand_from mode' -a 'used remaining'
