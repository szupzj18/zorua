#!/bin/sh
# Zorua installer (POSIX sh — run it with sh, bash or zsh)
#
#   curl -fsSL https://raw.githubusercontent.com/szupzj18/zorua/main/install.sh | sh
#
# Or from a clone:
#   sh install.sh
#
# Installs the Python core plus the zsh/bash/fish wrappers into
# ~/.zorua and wires up the shells it finds (zsh, bash, fish).
#
# Environment:
#   ZORUA_HOME  install destination (default: ~/.zorua)
#   ZORUA_REF   version to install when downloading: a tag like v0.7.1 (default: main)

set -e

REPO_OWNER="szupzj18"
REPO_NAME="zorua"
BRANCH="${ZORUA_REF:-main}"      # branch, tag (e.g. v0.7.1) or commit to install from
INSTALL_DIR="${ZORUA_HOME:-${CX_HOME:-$HOME/.zorua}}"
FILES="zorua_core.py zorua_providers.py zorua_statusline.py zorua_tui.py zorua.zsh zorua.bash zorua.fish"
# Not in older releases (zorua_tui.py came after v0.7.1): a pinned ZORUA_REF installs without them.
OPTIONAL_FILES="zorua_tui.py"
MARK_BEGIN="# >>> zorua >>>"
MARK_END="# <<< zorua <<<"
# Zorua used to be called codex-switch; clean up what the old installer left behind.
LEGACY_DIR="$HOME/.codex-switch"
LEGACY_BEGIN="# >>> codex-switch >>>"
LEGACY_END="# <<< codex-switch <<<"

say() { printf '%s\n' "$*"; }

# --- python3 (the core is Python 3.8+) --------------------------------------
if command -v python3 >/dev/null 2>&1; then
  if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    say "warning: python3 is older than 3.8; Zorua needs Python 3.8+" >&2
  fi
else
  say "warning: python3 not found. Zorua needs Python 3.8+ — install it, then open a new shell." >&2
fi

# --- copy files --------------------------------------------------------------
mkdir -p "$INSTALL_DIR"
script_dir=$(cd "$(dirname "$0")" 2>/dev/null && pwd) || script_dir=""
if [ -n "$script_dir" ] && [ -f "$script_dir/zorua_core.py" ]; then
  for f in $FILES; do cp "$script_dir/$f" "$INSTALL_DIR/$f"; done
  say "installed from local clone: $script_dir"
else
  for f in $FILES; do
    url="https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/$BRANCH/$f"
    say "downloading $url"
    if ! curl -fsSL "$url" -o "$INSTALL_DIR/$f.part"; then
      rm -f "$INSTALL_DIR/$f.part"
      case " $OPTIONAL_FILES " in
        *" $f "*) say "skipped $f (not in $BRANCH; 'zorua tui' needs a newer release)"; continue ;;
      esac
      say "download failed (private repo? clone it and run 'sh install.sh')" >&2
      exit 1
    fi
    mv "$INSTALL_DIR/$f.part" "$INSTALL_DIR/$f"
  done
fi
chmod +x "$INSTALL_DIR/zorua_core.py" "$INSTALL_DIR/zorua_statusline.py"

# Files from before the command was renamed from cx to zorua.
renamed_cmd=0
for f in cx_core.py cx_statusline.py; do
  if [ -f "$INSTALL_DIR/$f" ]; then rm -f "$INSTALL_DIR/$f"; renamed_cmd=1; fi
done

# --- migrate from the codex-switch name ----------------------------------------
strip_legacy_block() {
  rc="$1"
  if [ -f "$rc" ] && grep -qF "$LEGACY_BEGIN" "$rc"; then
    tmp="$rc.zorua.tmp"
    awk -v b="$LEGACY_BEGIN" -v e="$LEGACY_END" '
      $0 == b { skip=1; next }
      $0 == e { skip=0; next }
      skip != 1 { print }
    ' "$rc" > "$tmp" && mv "$tmp" "$rc"
    say "migrated: removed the old codex-switch block from $rc"
  fi
}

# --- wire up shells ----------------------------------------------------------
# add_block <rc file> <source line> <shell>: append a marked block once.
add_block() {
  rc="$1"; line="$2"
  mkdir -p "$(dirname "$rc")"
  touch "$rc"
  if grep -qF "$MARK_BEGIN" "$rc"; then
    say "source block already present, left untouched: $rc"
  elif grep -qF "zorua.$3" "$rc"; then
    say "warning: $rc already references zorua.$3 without marker block;"
    say "         add this line manually if needed:"
    say "         $line"
  else
    {
      printf '\n%s\n' "$MARK_BEGIN"
      printf '%s\n' "$line"
      printf '%s\n' "$MARK_END"
    } >> "$rc"
    say "added source block to $rc"
  fi
}

wired=""
ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"
strip_legacy_block "$ZSHRC"
strip_legacy_block "$HOME/.bashrc"
OLD_FISH="${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/codex-switch.fish"
if [ -f "$OLD_FISH" ] && grep -qF "$LEGACY_BEGIN" "$OLD_FISH"; then
  rm -f "$OLD_FISH"
  say "migrated: removed the old fish conf.d/codex-switch.fish"
fi
if command -v zsh >/dev/null 2>&1 || [ -f "$ZSHRC" ]; then
  add_block "$ZSHRC" "source \"$INSTALL_DIR/zorua.zsh\"" zsh
  wired="$wired zsh"
fi

BASHRC="$HOME/.bashrc"
use_bash=0
if [ -f "$BASHRC" ]; then use_bash=1; fi
case "${SHELL:-}" in */bash) use_bash=1 ;; esac
if [ "$use_bash" = 1 ]; then
  add_block "$BASHRC" ". \"$INSTALL_DIR/zorua.bash\"" bash
  wired="$wired bash"
  if [ -f "$HOME/.bash_profile" ] && ! grep -q 'bashrc' "$HOME/.bash_profile"; then
    say "note: ~/.bash_profile does not load ~/.bashrc; login shells (macOS Terminal) need:"
    say "      [ -f ~/.bashrc ] && . ~/.bashrc"
  fi
fi

FISH_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/fish"
FISH_CONF="$FISH_DIR/conf.d/zorua.fish"
if command -v fish >/dev/null 2>&1 || [ -d "$FISH_DIR" ]; then
  mkdir -p "$FISH_DIR/conf.d"
  if [ -f "$FISH_CONF" ] && grep -qF "$MARK_BEGIN" "$FISH_CONF"; then
    say "source block already present, left untouched: $FISH_CONF"
  else
    {
      printf '%s\n' "$MARK_BEGIN"
      printf 'source "%s/zorua.fish"\n' "$INSTALL_DIR"
      printf '%s\n' "$MARK_END"
    } > "$FISH_CONF"
    say "added source block to $FISH_CONF"
  fi
  wired="$wired fish"
fi

if [ -z "$wired" ]; then
  say "no zsh/bash/fish found; source one of these from your shell's rc file:"
  say "  $INSTALL_DIR/zorua.zsh | .bash | .fish"
fi

# Claude accounts that used the usage relay still point at the old install path.
if command -v python3 >/dev/null 2>&1; then
  python3 "$INSTALL_DIR/zorua_core.py" hook refresh || true
fi
if [ "$renamed_cmd" = 1 ] || [ -d "$LEGACY_DIR" ]; then
  say ""
  say "note: the command is now 'zorua' (it used to be 'cx'). Open a new terminal; the old name is gone."
fi
if [ -d "$LEGACY_DIR" ] && [ "$INSTALL_DIR" != "$LEGACY_DIR" ]; then
  say ""
  say "note: the old install directory $LEGACY_DIR is no longer used."
  say "      Your accounts and bindings were carried over (the old config is kept as a backup);"
  say "      delete it when you are happy: rm -rf $LEGACY_DIR ~/.config/codex-switch"
fi

if command -v codex >/dev/null 2>&1; then
  say "detected codex: $(codex --version 2>/dev/null || echo 'unknown version')"
else
  say ""
  say "note: codex CLI not found on PATH. Install it first:"
  say "      curl -fsSL https://chatgpt.com/codex/install.sh | sh"
fi

say ""
say "done (shells:${wired:- none}). Open a new terminal, then run: zorua setup   (or: zorua help)"
