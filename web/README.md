# Zorua web

A local dashboard for Zorua: accounts, 5h/7d usage, providers and directory bindings, with account management and a provider editor. Next.js and Tailwind CSS.
It is optional and separate from the Python program; it is not part of the release tarball.

- Data comes from `zorua usage --json` (run by the server, never by the browser). Tokens and provider keys are not in that output, and emails arrive masked (`jon***h@gmail.com`; start the server with `ZORUA_FULL_EMAIL=1` to show them whole). Stored provider keys always stay masked; enter a replacement in the editor to rotate a key.
- The overview starts with a **Needs attention** strip: accounts that are not signed in or report a usage error, limits at 80% or more, Claude accounts with no usage data yet, and failed provider checks; each item opens that account or provider. A Claude usage cache older than 10 minutes is flagged (no session is open to refresh it), and a window that has reset since the last report is drawn as an empty dashed bar instead of vanishing. A Claude account whose relay is hidden by a project status line is listed too. The same items put a dot next to the account or provider in the sidebar and a count in the tab title, `(2) Zorua …`. Rows have **copy command** (`zorua use <name>`; for a binding, `cd <dir> && zorua bind <name>`), because a shell can only be switched from the shell itself.
- Keyboard: **⌘K** (or **Ctrl+K**, or **/** outside a field) opens a command palette: jump to any account or provider, add one, refresh, check all providers, copy `zorua use <name>`, switch theme, compact rows. Single keys work outside fields: `r` refresh, `g o` overview, `a`/`p`/`b` add, `t` theme, `d` density, `?` the list. Below the `lg` breakpoint the sidebar is a drawer behind the menu button.
- Layout: a sidebar (overview, accounts, providers) and a content area. The overview shows accounts and providers in two columns; a provider or account opens as a page (`#/provider/<name>`, `#/account/<name>`). Dark by default, light follows the system; the **auto / light / dark** switch in the sidebar overrides it.
- Results are cached for 30 s and refreshed in the background, so the page opens at once. The refresh button forces a reload (at most one per 5 s).

## What you can do

| | runs |
|---|---|
| add an account (Codex or Claude Code), optionally start the sign-in | `zorua add [--claude] <name> --no-login`, then `zorua login <name>` |
| sign in again | `zorua login <name>`: the CLI opens your browser; the page shows the link it printed and the result |
| remove an account; optionally delete its data directory | `zorua rm <name> [--purge]` |
| add or remove a provider (key typed once, stored by zorua in its own `providers.json`) | `zorua provider add / rm` |
| view and edit a provider: base URL, key and the five model slots up front, the model catalog and all other env variables in collapsible sections. There is no save button: a field is saved when you leave it (a masked key is kept as is; stored keys cannot be exported) | `zorua provider get`, `zorua provider put` (keeps `providers.json.bak`) |
| check a provider (reachable? key accepted? latency); the last result is shown on its row and page, **check all** runs every provider | `zorua provider check <name> --json` (results are kept in the server's memory, not on disk) |
| bind or unbind a directory | `zorua bind <name>` run inside that directory, `zorua unbind <dir>` |

Safety:

- The server binds to `127.0.0.1` and refuses requests whose `Host` is not loopback.
- Changes are `POST /api/action` only and need the same `Origin`, a JSON body and an `x-zorua-web` header, which a page on another site cannot send.
- Commands are started without a shell, with fixed argument lists; names, URLs and paths are validated first. Keys travel in an environment variable, not in arguments, and are scrubbed from error messages.
- Deleting data needs the account name typed, and is refused unless the directory is one Zorua created (`~/.codex-<name>`, `~/.claude-<name>`).
- A sign-in that does not finish is stopped after 10 minutes.

## Run

Needs Node 20+ and a Zorua with `--json` (`~/.zorua/zorua_core.py`).

```sh
cd web
npm ci
npm run build
npm start            # http://127.0.0.1:4747
```

`npm run dev` starts the development server on the same port. Set `ZORUA_CORE` to use another `zorua_core.py`.

## Tests

```sh
npm run typecheck
npm run build
npm test            # API tests: the built server, fake claude/codex CLIs, an isolated HOME
npm run test:ui     # browser tests: the built server on a fake core (tests/ui/fixture_core.py), driven by Chrome
```

`test:ui` also runs axe-core over the main screens (dark, light, phone width) and needs a Chrome (set `CHROME_PATH`, or it looks in the usual places); without one it is skipped locally and fails in CI. It starts its own server on port 4949 (`ZW_UI_PORT`).

## Live demo

The landing page embeds the dashboard running on made-up data, with no server: the same app, exported as static files, where `/api/*` is answered in the browser from an in-memory copy of some example accounts (`demo/overlay/app/demo.tsx`). Nothing is sent anywhere and a reload starts over.

```sh
cd web
npm ci
sh demo/build.sh          # writes ../docs/demo (GitHub Pages serves docs/); or pass another directory
```

The build runs on a copy in `web/.demo-build` without `app/api` and with `demo/overlay` on top, so the real app is not changed. CI builds the demo on every change and fails when `docs/demo/` is older than the dashboard. `docs/demo/` is committed, so rebuild it and commit it whenever the dashboard changes. Set `ZORUA_DEMO_BASE` if the demo is served from another path (default `/zorua/demo`).

## Keep it running (macOS)

```sh
sh launchd/zorua-web.sh install     # builds, then installs a LaunchAgent: starts now and at login
sh launchd/zorua-web.sh status
sh launchd/zorua-web.sh uninstall
```

A LaunchAgent does not inherit your shell's proxy variables, so `install` takes the proxy from `ZORUA_WEB_PROXY` (or your current `HTTPS_PROXY`) and writes it into the plist; it refuses to install without one, so usage requests never go out directly.
`ZORUA_WEB_PROXY=http://127.0.0.1:7897 sh launchd/zorua-web.sh install`
To change it later, edit `EnvironmentVariables` in `~/Library/LaunchAgents/org.zorua.web.plist` and reinstall.
Log: `~/Library/Logs/zorua-web.log`.

## Tests

```sh
npm run typecheck    # generates Next's route types first, so it works on a fresh checkout
npm run build
npm test             # = sh tests/run.sh: isolated HOME and fake claude/codex; never touches your real accounts
```

CI runs the same three on Node 20 and 22 (the `web` job), next to the shell smoke tests on Linux and macOS.
