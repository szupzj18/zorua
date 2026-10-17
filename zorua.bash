# Zorua — parallel multi-account manager for the OpenAI Codex CLI (bash wrapper)
# https://github.com/szupzj18/zorua
#
# Thin shell layer: all logic lives in zorua_core.py (next to this file).
# Needs bash 3.2+. The prompt marker is exposed as $ZORUA_PROMPT_TEXT, e.g.:
#   PS1='$ZORUA_PROMPT_TEXT \u@\h:\w\$ '

ZORUA_CORE="${ZORUA_CORE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/zorua_core.py}"
ZORUA_AUTO_ACTIVE="" _ZORUA_PRE_AUTO_HOME="" ZORUA_AUTO_CLAUDE="" _ZORUA_PRE_AUTO_CLAUDE="" ZORUA_AUTO_CLAUDE_PROVIDER="" _ZORUA_PRE_AUTO_CLAUDE_PROVIDER="" ZORUA_AUTO_CODEX_PROVIDER="" _ZORUA_PRE_AUTO_CODEX_PROVIDER="" ZORUA_PROMPT_KIND="" ZORUA_PROMPT_NAME="" ZORUA_PROMPT_TEXT=""
_ZORUA_BINDINGS="${ZORUA_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/zorua}/bindings.tsv"

if ! command -v python3 >/dev/null 2>&1; then
  zorua() { echo "zorua: python3 is required (Zorua core is written in Python 3.8+)" >&2; return 1; }
  return 0 2>/dev/null || exit 0
fi

_zorua_run() {
  local f rc
  f=$(mktemp "${TMPDIR:-/tmp}/zorua.XXXXXX") || return 1
  ZORUA_SHELL=bash ZORUA_EVAL_FILE=$f ZORUA_AUTO_ACTIVE=$ZORUA_AUTO_ACTIVE ZORUA_PRE_AUTO_HOME=$_ZORUA_PRE_AUTO_HOME \
    ZORUA_AUTO_CLAUDE=$ZORUA_AUTO_CLAUDE ZORUA_PRE_AUTO_CLAUDE=$_ZORUA_PRE_AUTO_CLAUDE \
    ZORUA_AUTO_CLAUDE_PROVIDER=$ZORUA_AUTO_CLAUDE_PROVIDER ZORUA_PRE_AUTO_CLAUDE_PROVIDER=$_ZORUA_PRE_AUTO_CLAUDE_PROVIDER \
    ZORUA_AUTO_CODEX_PROVIDER=$ZORUA_AUTO_CODEX_PROVIDER ZORUA_PRE_AUTO_CODEX_PROVIDER=$_ZORUA_PRE_AUTO_CODEX_PROVIDER \
    python3 "$ZORUA_CORE" "$@"
  rc=$?
  if [ -s "$f" ]; then . "$f"; fi
  rm -f "$f"
  return $rc
}

zorua() { _zorua_run "$@"; }

# While a provider is active (zorua use <provider>), plain `claude` / `codex` runs on it.
claude() {
  if [ -n "${ZORUA_CLAUDE_PROVIDER:-}" ]; then
    ZORUA_SHELL=bash python3 "$ZORUA_CORE" launch claude "$@"
  else
    command claude "$@"
  fi
}
codex() {
  if [ -n "${ZORUA_CODEX_PROVIDER:-}" ]; then
    ZORUA_SHELL=bash python3 "$ZORUA_CORE" launch codex "$@"
  else
    command codex "$@"
  fi
}

# bash has no chpwd hook: check $PWD from PROMPT_COMMAND instead.
_zorua_prompt_hook() {
  if [ "$PWD" != "${_ZORUA_LAST_PWD:-}" ]; then
    _ZORUA_LAST_PWD=$PWD
    if [ -s "$_ZORUA_BINDINGS" ] || [ -n "$ZORUA_AUTO_ACTIVE" ] || [ -n "$ZORUA_AUTO_CLAUDE" ] || [ -n "$ZORUA_AUTO_CLAUDE_PROVIDER" ] || [ -n "$ZORUA_AUTO_CODEX_PROVIDER" ]; then
      _zorua_run apply "$PWD"
    fi
  fi
}

case $- in
  *i*)
    if [[ "$(declare -p PROMPT_COMMAND 2>/dev/null)" == "declare -a"* ]]; then
      case " ${PROMPT_COMMAND[*]} " in
        *" _zorua_prompt_hook "*) ;;
        *) PROMPT_COMMAND=(_zorua_prompt_hook "${PROMPT_COMMAND[@]}") ;;
      esac
    else
      case ";${PROMPT_COMMAND:-};" in
        *";_zorua_prompt_hook;"*) ;;
        *) PROMPT_COMMAND="_zorua_prompt_hook${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
      esac
    fi
    ;;
esac
_zorua_run apply "$PWD"

_zorua_complete() {
  local cur=${COMP_WORDS[COMP_CWORD]} names
  names=$(ZORUA_SHELL=bash python3 "$ZORUA_CORE" names 2>/dev/null)
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=($(compgen -W "ls usage pick tag setup use login off add rm bind unbind binds hook provider prompt version help $names" -- "$cur"))
  else
    case ${COMP_WORDS[1]} in
      use|login|bind|rm|tag) COMPREPLY=($(compgen -W "$names" -- "$cur")) ;;
    esac
  fi
}
complete -F _zorua_complete zorua
