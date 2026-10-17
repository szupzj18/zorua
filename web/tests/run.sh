#!/bin/sh
# Start the built dashboard in an isolated HOME with fake claude/codex CLIs, then run api_test.py.
# Needs `npm run build` first. Never touches your real accounts.
set -e
WEB="$(cd "$(dirname "$0")/.." && pwd)"
CORE="${ZORUA_CORE:-$WEB/../zorua_core.py}"
PORT="${ZW_PORT:-4848}"
ROOT="$(mktemp -d)"
trap 'kill "$PID" 2>/dev/null || true; rm -rf "$ROOT"' EXIT
mkdir -p "$ROOT/home" "$ROOT/cfg" "$ROOT/bin" "$ROOT/proj"

cat > "$ROOT/bin/claude" <<'EOF'
#!/bin/sh
case "$1 $2" in
  "auth status")
    if [ -f "$CLAUDE_CONFIG_DIR/.fake-login" ]; then echo '{"loggedIn":true,"authMethod":"claude.ai","email":"fake@example.com","subscriptionType":"pro"}'
    else echo '{"loggedIn":false,"authMethod":"none"}'; fi ;;
  "auth login") echo "Open https://claude.ai/oauth/authorize?code=fake to sign in"; sleep 3; touch "$CLAUDE_CONFIG_DIR/.fake-login" ;;
esac
EOF
cat > "$ROOT/bin/codex" <<'EOF'
#!/bin/sh
echo "Visit https://auth.openai.com/oauth/authorize?fake=1"; sleep 2; exit 1
EOF
cp "$WEB/../tests/fake-security" "$ROOT/bin/security"
chmod +x "$ROOT/bin/claude" "$ROOT/bin/codex" "$ROOT/bin/security"
unset ZORUA_TEST_KEYCHAIN

HOME="$ROOT/home" ZORUA_CONFIG_DIR="$ROOT/cfg" ZORUA_CORE="$CORE" PATH="$ROOT/bin:$PATH" \
  node "$WEB/node_modules/next/dist/bin/next" start "$WEB" -H 127.0.0.1 -p "$PORT" >"$ROOT/server.log" 2>&1 &
PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
  curl -fs "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break
  sleep 1
done
ZW_ROOT="$ROOT" ZW_BASE="http://127.0.0.1:$PORT" python3 "$WEB/tests/api_test.py"
