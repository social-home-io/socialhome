---
name: haos-ingress-ui
description: Drives the built Social Home SPA end-to-end in headless Chrome behind a simulated Home Assistant ingress (SH_MODE=haos) — a fake HA Supervisor + Core, an ingress look-alike reverse proxy under /api/hassio_ingress/<token>/ that stamps X-Ingress-Path / X-Hass-Source / X-Remote-User-*, and the real backend — and fails if any request, WebSocket or navigation escapes the ingress prefix, the haos setup hands out a token, the live WebSocket / router / media upload / iframe embed break, or a missing ingress user shows anything but IngressAuthFailed. One `verify` command boots everything, runs every check, writes a report + screenshots and exits non-zero on failure; CI runs it on every PR (job `haos-ingress-ui`). Use for any change to SPA URLs, routing, fetch / XHR / WebSocket paths, auth or the cold-start probe, the setup wizard, iframe / embed behaviour, `<base href>` / SpaIndexView, HaIngressStrategy or the haos platform adapter.
---

# haos-ingress-ui

Under the Home Assistant App the SPA is served at
`/api/hassio_ingress/<token>/`, inside an iframe, with no bearer token —
the Supervisor's proxy adds the auth headers. Anything that resolves a URL
against the origin instead of `document.baseURI` works in standalone and
silently breaks here (#303, the `window.location.href = '/'` bounce, …).
`tsc` and Vitest can't see that; this skill can.

## When to run it

Before opening a PR that touches any of:

- a URL the SPA builds: `api.*` calls, raw `fetch` / `XMLHttpRequest`,
  `new WebSocket`, `<img src>`, `<a href>`, icon / manifest refs
  (`client/src/baseUrl.ts`, `api.ts`, `ws.ts`);
- routing and navigation: `router.ts`, `IngressLocationProvider`,
  `window.location.*`, redirects after setup / logout;
- auth: the cold-start `/api/me` probe, `isAuthed`, token handling,
  `IngressAuthFailed`, the "Session expired" toast;
- the setup wizard (`features/setup/`), the onboarding tour;
- iframe / embed behaviour;
- backend: `socialhome/routes/spa.py` (`SpaIndexView` `<base href>`),
  `socialhome/auth.py` (`HaIngressStrategy`), `socialhome/platform/`.

CI runs it on every PR anyway; running it locally first saves a round
trip and gives you the screenshots.

## Run it

From the repo root, with the project's Python (it needs the backend's
dependencies — aiohttp etc.):

```bash
python .claude/skills/haos-ingress-ui/harness.py verify --out /tmp/haos-ingress-ui
```

What `verify` does:

1. **Chrome** — `--chrome PATH`, else `$CHROME_PATH`, else
   `$PUPPETEER_EXECUTABLE_PATH`, else the first of `chromium`,
   `chromium-browser`, `google-chrome`, `google-chrome-stable`
   (`/usr/bin/chromium` in the devcontainer, the preinstalled Google
   Chrome on GitHub runners).
2. **SPA bundle** — `--build=auto` (default) reuses `socialhome/static`
   when it is newer than everything under `client/src` (+ `index.html`,
   `package.json`, lockfile, vite config) and runs `pnpm exec vite build`
   otherwise. `--build=always` / `--build=never` force it.
3. **Check deps** — `npm ci` in `checks/` when `node_modules` is missing
   or older than `package-lock.json` (puppeteer-core only; no browser
   download).
4. **Stack** — three processes on free ports (`--supervisor-port`,
   `--backend-port`, `--proxy-port` pin them; `0` = free):
   - `fake_supervisor.py` — Supervisor (`/addons/self/info`,
     `/discovery`) + Core through `/core` (REST + the WebSocket auth
     handshake, `config/auth/list` with alice = owner, bob, carol).
   - `run_backend.py` — `python -m socialhome` in `SH_MODE=haos` with a
     fresh temp data dir. Patches two things from *outside* the
     package: `HaClient`'s hard-coded `http://supervisor/core` →
     the fake, and the per-user rate limiter → off (the walk reloads
     the SPA far faster than a person would).
   - `ingress_proxy.py` — the HA Core + Supervisor ingress hop:
     `/api/hassio_ingress/<token>/<path>` → backend `/<path>`, with
     `X-Hass-Source: core.ingress`, `X-Ingress-Path`,
     `X-Remote-User-Id/-Name/-Display-Name` (client copies stripped),
     `X-Forwarded-*`, WebSockets proxied. Anything outside the prefix is
     logged as a **violation** and answered 404.
5. **Checks** — `checks/ingress.mjs` (puppeteer-core), in order (see
   the next section).
6. **Report** — `<out>/report.md` (table + every failure's detail),
   `results.json`, `requests.json` (every browser request + the proxy
   log), `screenshots/*.png` (`FAIL-*.png` for failures), `logs/`
   (backend, proxy, supervisor). Exit 0 all green, 1 a check failed,
   2 setup failed (Chrome / build / a process didn't come up).
7. **Teardown** — every process group is killed, also on Ctrl-C /
   SIGTERM; the backend data dir is deleted (`--keep-data` keeps it).

`--only a,b` runs just those checks (the `setup-*` steps and the
`request-audit` always run). Takes ~45 s locally, a couple of minutes on
a CI runner.

To poke at the stack by hand:

```bash
python .claude/skills/haos-ingress-ui/harness.py up --out /tmp/haos-ingress-ui
# open http://127.0.0.1:<proxy>/frame  — the HA-like iframe page
# switch the signed-in HA user: http://127.0.0.1:<proxy>/_proxy/user?name=bob|carol|none
```

## The checks

| Check | Fails when |
|---|---|
| `setup-haos-flow` | the haos wizard (welcome → household name → submit) doesn't land in the app under the prefix, `POST /api/setup/haos/complete` returns a `token`, or `localStorage.sh_token` is set |
| `setup-seed-content` | seeding (posts, an image, a DM, a space, an album; bob + carol provisioned from HA) fails — setup, not a UI finding |
| `walk-routes-desktop` / `-mobile` | any route in `client/src/router.ts` (params filled from the seed; unfillable ones listed in the report) loaded cold at 1280×720 / 390×844: `document.baseURI` or `location` leaves the prefix, IngressAuthFailed or the error boundary shows, `#main` is missing, or the page throws |
| `ws-under-prefix-and-live` | the WebSocket doesn't open at `<prefix>/api/ws` through the proxy, or bob's new post doesn't appear live on alice's feed |
| `ws-nested-route-cold-load` | a cold load of `<prefix>/dms/<id>` hands `new WebSocket` a relative URL (Firefox resolves it against the page, `<prefix>/dms/api/ws`, ignoring `<base href>` — Chrome doesn't, so the raw constructor argument is asserted), the socket opens outside `<prefix>/api/ws`, or bob's DM doesn't appear live |
| `router-keeps-prefix` | in-app link clicks, reload, back, back, forward don't land on `<prefix>/spaces`, `/calendar`, `/calendar`, `/spaces`, `/`, `/spaces`; then the Momentum tab links `/momentum` → `/momentum/yours` and Back to `/momentum` |
| `media-load-and-upload` | feed images aren't loaded from under the prefix, or the gallery's raw-XHR upload posts elsewhere / fails |
| `iframe-embed` | the app doesn't boot inside the HA-like iframe, or in-iframe navigation moves the top frame |
| `ingress-401-auth-failed` | with no `X-Remote-User-*` the SPA shows anything but IngressAuthFailed: a password field, or any toast (there was no session to expire) |
| `request-audit` | over the whole run: any request outside the prefix, any foreign origin, any `Authorization` header, any 5xx through the proxy |
| `supervisor-contract` | the backend never read `/addons/self/info` or listed HA users over the Core WebSocket, or the fake rejected its Supervisor token |

Every wait is a wait-for-condition with a timeout — no fixed sleeps, no
retries. "Network idle" is computed from raw CDP events for the current
document only (see the comment in `newPage`): puppeteer's own
`waitForNetworkIdle` hangs forever on a fetch Chrome cancelled with the
old document during the post-setup reload.

## Reading a failure

Start with the first `FAIL` in the console or `report.md`.

- **"N request(s) escaped the ingress prefix during this check: GET
  /api/…"** — the root cause in almost every case. Some code built an
  origin-relative URL. Find it (`grep -rn "'/api/<path>" client/src`) and
  route it through the `api` client / `addBase()` / `basePath`
  (CLAUDE.md, "Frontend (SPA) & ingress"). Follow-on timeouts in the same
  run ("the SPA to paint a screen …") are usually the same bug: the app
  can't boot when `/api/instance/config` 404s.
- **"location left the prefix → /"** — a `window.location.href = '/…'`
  or `.assign` / `.replace` with an absolute path. Use `basePath` /
  `addBase`.
- **"… returned a token"** / **"localStorage sh_token"** — a haos
  endpoint or the SPA started carrying a bearer token; haos returns
  `{username}` only.
- **"IngressAuthFailed shown"** on a route — ingress auth got lost: the
  backend didn't accept the headers (`HaIngressStrategy`, middleware
  order) or the user isn't provisioned.
- **"the network to go idle … still in flight: <url>"** — that request
  never finished; check `logs/proxy.log` / `logs/backend.log` for it.
- **`supervisor-contract`** — the backend changed which HA endpoints it
  calls. If it's a new, *real* HA endpoint, teach `fake_supervisor.py`
  (cite the HA source you verified the shape against — never guess);
  `unmocked` calls are listed in the report.
- **Setup failed (exit 2)** — read `logs/<process>.log`; a backend that
  doesn't come up in 90 s usually means a startup exception.

The screenshots (`walk-<viewport>-<route>.png`, `FAIL-<check>.png`) show
what the page looked like; on CI they're in the `haos-ingress-ui-report`
artifact of the run.

## Adding a check

1. Add a `await check('<name>', async () => { … return { problems } })`
   block to `checks/ingress.mjs`. Push human-readable strings onto
   `problems` (empty = pass) or throw; wrap waits in
   `step('<what you wait for>', …)` so a timeout names the condition.
2. Use `newPage('desktop' | 'mobile')` for a fresh context (it records
   every request for the audit), `settled(page)` after a navigation,
   `asUser(user, method, path, { json })` to seed through the backend,
   `proxyCtl('/_proxy/user?name=…')` to switch the HA user — and restore
   `alice` in a `finally`.
3. UI copy comes from `T('<i18n key>')` (the English catalogue), never a
   hard-coded string.
4. Wait on conditions (`page.waitForFunction`, `waitFor(desc, fn)`),
   never on time.
5. Add a row to the table above and run `verify` a few times in a row
   to be sure it's deterministic.

## Fidelity — what the fakes are verified against

The fakes copy HA's real contracts; the citations are in each file's
docstring:

- `homeassistant/components/hassio/ingress.py` (core `dev` @ 5dacceaa) —
  `_init_header`: `X-Hass-Source`, `X-Ingress-Path`, `X-Forwarded-*`,
  dropped request headers; WebSocket forwarding.
- `supervisor/api/ingress.py` (supervisor `main` @ 143aa6e6) —
  `_init_header`: `X-Remote-User-Id/-Name/-Display-Name`, client copies
  stripped; upstream URL `http://<ip>:<ingress_port>/<path>`.
- `supervisor/api/proxy.py` — the Core WebSocket auth handshake the
  Supervisor answers itself; `supervisor/api/middleware/security.py` —
  401 / 403 on a missing / unknown token; `supervisor/api/apps.py` +
  `discovery.py` — response shapes.
- `homeassistant/components/config/auth.py` `_user_info` — the
  `config/auth/list` row; `websocket_api/messages.py` — result / error
  frames (an unknown command gets `unknown_command`, like real Core).

Not emulated: the Supervisor's `ingress_session` cookie (the user is
whoever `/_proxy/user` selects) and Core's response compression.
