# Zorua — parallel multi-account manager for the OpenAI Codex CLI (zsh wrapper)
# https://github.com/szupzj18/zorua
#
# Thin shell layer: all logic lives in zorua_core.py (next to this file). This
# wrapper runs it, then sources whatever shell statements it requested
# (CODEX_HOME, auto-binding state, prompt marker), and adds the cd hook,
# right-prompt marker and tab completion.

typeset -g ZORUA_CORE="${ZORUA_CORE:-${${(%):-%x}:A:h}/zorua_core.py}"
typeset -g ZORUA_AUTO_ACTIVE="" _ZORUA_PRE_AUTO_HOME="" ZORUA_AUTO_CLAUDE="" _ZORUA_PRE_AUTO_CLAUDE="" ZORUA_AUTO_GROK="" _ZORUA_PRE_AUTO_GROK="" ZORUA_AUTO_CLAUDE_PROVIDER="" _ZORUA_PRE_AUTO_CLAUDE_PROVIDER="" ZORUA_AUTO_CODEX_PROVIDER="" _ZORUA_PRE_AUTO_CODEX_PROVIDER="" ZORUA_PROMPT_KIND="" ZORUA_PROMPT_NAME="" ZORUA_PROMPT_TEXT=""
typeset -g _ZORUA_BINDINGS="${ZORUA_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/zorua}/bindings.tsv"

if ! (( $+commands[python3] )); then
  zorua() { print "zorua: python3 is required (Zorua core is written in Python 3.8+)" >&2; return 1 }
  return 0
fi

# Run the core, then apply the shell changes it asked for.
_zorua_run() {
  local f rc
  f=$(mktemp "${TMPDIR:-/tmp}/zorua.XXXXXX") || return 1
  ZORUA_SHELL=zsh ZORUA_EVAL_FILE=$f ZORUA_AUTO_ACTIVE=$ZORUA_AUTO_ACTIVE ZORUA_PRE_AUTO_HOME=$_ZORUA_PRE_AUTO_HOME \
    ZORUA_AUTO_CLAUDE=$ZORUA_AUTO_CLAUDE ZORUA_PRE_AUTO_CLAUDE=$_ZORUA_PRE_AUTO_CLAUDE \
    ZORUA_AUTO_GROK=$ZORUA_AUTO_GROK ZORUA_PRE_AUTO_GROK=$_ZORUA_PRE_AUTO_GROK \
    ZORUA_AUTO_CLAUDE_PROVIDER=$ZORUA_AUTO_CLAUDE_PROVIDER ZORUA_PRE_AUTO_CLAUDE_PROVIDER=$_ZORUA_PRE_AUTO_CLAUDE_PROVIDER \
    ZORUA_AUTO_CODEX_PROVIDER=$ZORUA_AUTO_CODEX_PROVIDER ZORUA_PRE_AUTO_CODEX_PROVIDER=$_ZORUA_PRE_AUTO_CODEX_PROVIDER \
    python3 "$ZORUA_CORE" "$@"
  rc=$?
  if [[ -s $f ]]; then source "$f"; fi
  rm -f "$f"
  _zorua_rprompt
  return $rc
}

zorua() { _zorua_run "$@" }

# While a provider is active (zorua use <provider>), plain `claude` / `codex` runs on it.
claude() {
  if [[ -n $ZORUA_CLAUDE_PROVIDER ]]; then
    ZORUA_SHELL=zsh python3 "$ZORUA_CORE" launch claude "$@"
  else
    command claude "$@"
  fi
}
codex() {
  if [[ -n $ZORUA_CODEX_PROVIDER ]]; then
    ZORUA_SHELL=zsh python3 "$ZORUA_CORE" launch codex "$@"
  else
    command codex "$@"
  fi
}

_zorua_rprompt() {
  case $ZORUA_PROMPT_KIND in
    "")     RPROMPT="" ;;
    auto)   RPROMPT="%F{yellow}${ZORUA_PROMPT_TEXT}%f" ;;
    *)      RPROMPT="%F{cyan}${ZORUA_PROMPT_TEXT}%f" ;;
  esac
}

# cd hook: only spawn the core when a binding could matter.
_zorua_apply_binding() {
  if [[ -s $_ZORUA_BINDINGS || -n $ZORUA_AUTO_ACTIVE || -n $ZORUA_AUTO_CLAUDE || -n $ZORUA_AUTO_GROK || -n $ZORUA_AUTO_CLAUDE_PROVIDER || -n $ZORUA_AUTO_CODEX_PROVIDER ]]; then
    _zorua_run apply "$PWD"
  fi
}

if [[ -o interactive ]]; then
  if [[ -z ${precmd_functions[(r)_zorua_rprompt]} ]]; then
    precmd_functions+=(_zorua_rprompt)
  fi
  if [[ -z ${chpwd_functions[(r)_zorua_apply_binding]} ]]; then
    chpwd_functions=(_zorua_apply_binding $chpwd_functions)
  fi
fi
# Initial state: honor an inherited CODEX_HOME and any binding for $PWD.
_zorua_run apply "$PWD"

_zorua() {
  local -a names
  names=(${(f)"$(ZORUA_SHELL=zsh python3 $ZORUA_CORE names 2>/dev/null)"})
  if (( CURRENT == 2 )); then
    _alternative \
      'subcommands:zorua command:(ls usage pick tag doctor setup use login off add rm bind unbind binds hook provider prompt version help)' \
      "accounts:codex account:($names)"
  elif (( CURRENT == 3 )); then
    case $words[2] in
      use|login|bind|tag|doctor) _wanted accounts expl 'account or provider' compadd -- $names ;;
      rm) _wanted accounts expl 'codex account' compadd -- ${names:#default} ;;
      usage) _wanted opts expl 'usage option' compadd -- -v -r --remaining --used --expiry --json --full mode ;;
    esac
  elif (( CURRENT == 4 )) && [[ $words[2] == usage && $words[3] == mode ]]; then
    _wanted modes expl 'usage mode' compadd -- used remaining
  fi
}
(( $+functions[compdef] )) && compdef _zorua zorua
return 0
