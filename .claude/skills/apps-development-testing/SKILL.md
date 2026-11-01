---
name: apps-development-testing
description: How to build, test, and release Social Home Apps — embedded JS extension apps (e.g. chess) that install into Social Home and play cross-household over federation. Use when creating a new app, changing the @socialhome/app-sdk, debugging an app that won't load in the sandbox, or cutting a release of the socialhome-apps repo. Covers the manifest, the mandatory single-inlined-bundle shape for the sandboxed iframe, the SDK API, local single- and two-instance (federated) testing with a headless browser, and the CalVer release flow that publishes the catalog Social Home installs from.
---

# Social Home Apps — development & testing

Social Home Apps are small JS apps that a household admin installs from a catalog. They run in a **sandboxed iframe**, store per-user data, and message their counterpart on another household over federation. The platform lives in the `socialhome` repo (`routes/apps.py`, `services/app_*`, `federation/app_framing.py`); the **apps themselves live in a separate repo: `social-home-io/socialhome-apps`** (`packages/app-sdk`, `apps/<id>/`, `scripts/build-catalog.mjs`, `.github/workflows/`).

## The one rule that breaks everything if ignored

**An app bundle MUST be a single, fully-inlined `index.html`** (JS + CSS + icon all inline). The iframe is `sandbox="allow-scripts"` → **opaque origin (`"null"`)**. Any external sub-resource (`<script src>`, `<link href>`, `<img src>`) is fetched from the real Social Home origin and is either CORS-blocked (module scripts, from origin `null`) or fails CSP `'self'` (the document origin is `null`, not the serving origin). Symptom: the iframe shows only the static HTML header, `app.js`/CSS never load, console shows a CORS error or a blocked-by-CSP error. Fix: inline everything. The chess `build.mjs` is the reference — esbuild → IIFE string → inlined `<script>`, CSS read into `<style>`, icon inlined; only `index.html` + `manifest.json` ship in the tarball.

Corollary: the SDK only talks to the host via `postMessage` (no `fetch`; CSP is `connect-src 'none'`). Apps never get the bearer token.

## App layout

```
apps/<app_id>/
  manifest.json     # app_id, name, version (CalVer), description, entry, icon, capabilities
  package.json      # "build" script -> node build.mjs ; dep "@socialhome/app-sdk": "workspace:*"
  build.mjs         # esbuild bundle -> SINGLE inlined dist/index.html (+ dist/manifest.json)
  src/main.ts       # UI glue (imports sh from @socialhome/app-sdk)
  src/<logic>.ts    # pure, unit-tested logic
  public/           # index.html template, style.css, icon.svg (inlined at build time)
```

`manifest.json` fields the backend reads: `entry` (relative HTML, e.g. `index.html`; no `..`), `icon`, `capabilities` (`storage` / `federation`). `app_id`/`name`/`version`/`description` feed the catalog. **`version` is CalVer `YYYY.M.D[.N]`** (matching Social Home core; bump `.N` for same-day re-releases). Make the **icon a self-contained `data:` URI** so it renders in the host's Browse/Installed cards (a relative path 404s there).

## SDK (`@socialhome/app-sdk`) — runs in the iframe

```ts
import { sh } from '@socialhome/app-sdk'        // null outside a browser; non-null in the iframe
await sh.context()                               // { appId, selfUserId, permissions?, launch? }  (no token)
await sh.store.get/set/delete/list(...)          // per-user KV (cleared on uninstall)
await sh.peers()                                 // [{ instance_id, display_name }] confirmed households
await sh.people.pick({ max: 1, suggest? })       // Social Home's invite sheet -> [{ ...target, display_name, sessionId }] ([] = cancel)
await sh.contacts()                              // only people picked / played with in this app — never the roster
await sh.federation.send(sessionId, target, payload)  // only sessions Social Home knows for that person (else ShError 'unknown_session')
sh.onMessage(({ sessionId, fromInstance, payload }) => {...})  // in-game messages
sh.onSession(({ sessionId, fromInstance, payload }) => {...})  // live invites (news only) + closes
```

**SDK 2.0 is modular — an app's bundle carries only what it uses.** Group games and engines join the client at start-up: `const client = withDraw(withGroups(sh!))` (`@socialhome/app-sdk/group` → `client.group`, `@socialhome/app-sdk/draw` → `client.engines.draw`). A new SDK feature goes into its own module with a `with<Name>(client)`, never onto `SocialHomeClient`: otherwise every app's bundle changes, the version guard bumps every app and every household gets an update for nothing (`packages/app-sdk/src/modules.test.ts` guards it).

**Invites are played from Social Home, not the app (D12).** An invite waits in the person's "Needs you"; **Play** opens `/apps/<id>?session=<sid>`, the host accepts that one invite and `sh.context()` returns `launch: { sessionId, invite? }` — start/open that game. Opened any other way, `launch` is `{}` and nothing is accepted. `launch` missing entirely = an older host: fall back to `federation.pendingSessions()` + seating live invites. Two-household check: invite twice, Play the first from Needs you → the app opens that game, and the second invite is still in Needs you. See `docs/protocol/apps.md#launch-context`.
Route inbound by `sessionId` (supports many concurrent games) and identify the sender by `fromInstance`. The host relays a backend `app.message` WS frame into the iframe; outbound goes over the dedicated `fed-app-v1` channel with an `APP_MESSAGE`/HTTPS fallback.

**The host shows the frame only once the app speaks.** `AppHost` keeps the iframe invisible until the app's first bridge request (`app.ready`, or any RPC such as `sh.context()`): after 8 s it says "taking longer than usual" (Try again), and it gives up with "didn't start" after 20 s, or 5 s after the frame's `load` event if the app never asked anything (a bundle error page, a script that crashed). So call `sh.context()` (or post `{id, method: 'app.ready'}`) as soon as the script runs — an app that does its first RPC late shows a spinner meanwhile.

## Build & unit test (in socialhome-apps)

```sh
pnpm install                       # approves esbuild's build script (pnpm-workspace allowBuilds)
pnpm build                         # SDK then every app -> apps/*/dist/index.html
pnpm test                          # vitest: SDK + each app's logic
pnpm tsc --noEmit                  # (per package) types
RELEASE_TAG=YYYY.M.D pnpm catalog  # dry-run: builds tarballs + catalog.json (verifies packaging)
```
CI (`.github/workflows/ci.yml`) runs all of this on every push/PR.

## Local end-to-end test against a real Social Home (in the socialhome repo)

Boot Social Home standalone; it serves the built SPA and fetches the catalog from the public release (or point `apps_catalog_url` at a local mirror):

```sh
SH_MODE=standalone SH_CONFIG=/tmp/sh-dev/socialhome.toml python -m socialhome   # :8099
```
Minimal `socialhome.toml`: `[server] listen_port=8099` · `[storage] data_dir="/tmp/sh-dev"` · `[standalone] external_url="http://127.0.0.1:8099"`. First run needs setup: `POST /api/setup/standalone {username,password}` returns a bearer (login is rate-limited to 5/15min — fetch ONE token and reuse it; restart the process to reset the limiter). Install: `POST /api/apps {app_id}` (downloads + sha256-verifies + unpacks). Open the SPA at `/apps`.

### Two households (real federation)

Run a second instance on `:8100` with its own data dir + `external_url=http://127.0.0.1:8100`, then pair them (the §11 handshake — three calls):
```
POST :8099/api/pairing/initiate            -> qr
POST :8100/api/pairing/accept   body=qr    -> { token, verification_code }
POST :8099/api/pairing/confirm  body={token, verification_code}
```
After pairing, `sh.peers()` (`GET /api/apps/<id>/peers`) lists the other household. Install the app on both, then drive two browser sessions: A "New game" → Social Home's invite sheet (outside the iframe; Send ignores taps for 600 ms) → B gets the invite in Needs you → Play → both send moves → `onMessage` syncs. Invites go through Social Home only (D3): the bridge refuses `app.openSession` (`use_people_pick`), and `app.send` on a session id Social Home doesn't know for that person is `unknown_session`. App messages use the HTTPS-inbox fallback when WebRTC can't establish, so localhost play works without a TURN server. (`.claude/skills/federation-demo` automates multi-household pairing if you need more.)

### Visual / optical verification (headless Chromium)

Chromium is baked into the devcontainer (`/usr/bin/chromium`, `DISPLAY=:99`); see the `chrome-devtools-setup` skill. The chrome-devtools MCP may not be connected — drive it with `puppeteer-core` instead: launch with `executablePath:"/usr/bin/chromium", args:["--no-sandbox","--disable-dev-shm-usage"]`, inject auth via `page.evaluateOnNewDocument(t => localStorage.setItem("sh_token", t), token)`, `goto(".../apps", {waitUntil:"domcontentloaded"})` (NOT `networkidle` — the persistent WS never settles), dismiss the first-run tour (click "Skip tour", not the "Skip to main content" a11y link), then screenshot desktop (~1280×800) and mobile (~390×844). The app renders inside the bundle iframe (`page.frames().find(f => f.url().includes("/bundle/"))`). Note: `evaluateOnNewDocument` also runs in the sandboxed iframe and its `localStorage` call throws a SecurityError — that's expected and confirms the sandbox isolation.

## Release (publishes the catalog Social Home installs from)

1. Bump `version` (CalVer) in each changed app's `manifest.json` and add its `CHANGELOG.json` entry, through a PR (the version guard compares built bundles). A bump nobody notices (a newer SDK, a build tool) marks its entry `"quiet": true`: Social Home lists it under Updates without a Needs-you row (`is_quiet_update`); never on a version that changes how the app looks or plays, its capabilities or engines.
2. Confirm green CI.
3. Cut the release: `gh release create YYYY.M.D --title YYYY.M.D --notes "…"` (or the Releases UI). Same-day re-release → `YYYY.M.D.N`.
4. `.github/workflows/release.yml` builds + tests, then uploads `catalog.json` + each `<app>-<version>.tgz` (**sha256-pinned**) as release assets.
5. Social Home installs fetch `https://github.com/social-home-io/socialhome-apps/releases/latest/download/catalog.json` (cached ≤24h, refreshed by the daily update check), verify each bundle's sha256, and unpack. The **Apps → Check for updates** button / per-app **Update** picks up the new version.

Adding a new app needs no workflow edits — drop `apps/<new-id>/` with a `manifest.json` + a `build` script that emits an inlined `dist/`, and the catalog script + workflows discover it.

## Age gating

An app may declare `"min_age": 13` (one of `0/13/16/18`) in `manifest.json`. This becomes the install-time default — Social Home reads it when installing and sets `installed_apps.min_age` accordingly. The gate is **admin-authoritative**: a household admin may then set it to any valid value via `PATCH /api/apps/{id}` (the admin age-restriction select), in either direction. The `update` action raises `min_age` if the catalog declares a higher value but **never lowers** an admin's explicit choice.

Social Home blocks protected minors from launching or using age-restricted apps:
- `GET /api/apps` filters out apps with `min_age > 0` from the response for protected minor accounts — they simply don't appear in the list.
- `GET /api/apps/{id}/runtime` returns **403** for an under-age protected minor. The SPA (`AppHost`) catches this and renders "This app isn't available for your account." without leaking the exact age.

The SPA does **no** client-side age *enforcement* — the server enforces all gates. Non-minor users see all enabled apps and no age-restricted behaviour. Admins see a `Minimum age` select on each app card (options Everyone / 13+ / 16+ / 18+), but **only when the household has at least one member with child protection enabled** (`GET /api/cp/protection` → any `is_minor`); otherwise the setting is hidden (no point configuring a gate nobody is subject to). The rating chip (`13+`) still shows regardless, as the app's declared rating.

## Quick failure triage
- **Iframe blank, only the header shows** → bundle isn't inlined (external script/CSS blocked by sandbox CORS/CSP). Inline it.
- **Broken icon in the app card** → manifest `icon` is a relative path; use a `data:` URI.
- **`sh` is null** → running outside a browser (tests); use `createClient(targetWindow, parentWindow)` with fakes.
- **Peer not listed** → households aren't paired/confirmed; redo the pairing handshake.
- **Move doesn't arrive** → check both are paired + the app enabled on both; the `app.message` WS frame reaches the SPA that has the app open.
- **"Chess didn't start" in the host** → the frame loaded but no bridge request came within 5 s (bundle 404 / error body, a script error before the SDK's first call). Open the bundle URL from `/api/apps/{id}/runtime` and check the console.
- **"An admin turned Chess off" / "turned off for this household"** → `/runtime` answered 403 `APP_NOT_ENABLED`; an admin can Turn on right there.
- **Age-restricted app shows "not available"** → the user is a protected minor and `min_age > 0` on that app. Admin can lower/remove the restriction via the Apps page or `PATCH /api/apps/{id}` with `{min_age: 0}`.
