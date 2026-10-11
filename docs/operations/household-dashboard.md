# Household dashboard (wall tablet)

The household dashboard is a view of the household for a tablet on the
wall. It is served by Social
Home on its **own port** (default `8100`), separate from the main app
(`8099`), and is unlocked with one household-wide **dashboard password** —
the tablet never holds a member's account.

It is **off by default**. Design notes:
[architecture.md](../architecture.md#household-dashboard-own-port); API:
[api.md](../api.md#hfs--household-dashboard).

## Enabling it

1. As a household admin, set a dashboard password (at least 8
   characters) — `PUT /api/admin/dashboard/password`.
2. Enable the dashboard — `PUT /api/admin/dashboard` with
   `{"enabled": true}` (and, outside the Home Assistant App, optionally a
   `port`). The listener starts at once; `GET /api/admin/dashboard`
   reports `running: true` and the `url` tablets should open.
3. On the tablet, either open that `url` and type the password, or —
   while the dashboard is running — create a one-time setup link (`POST /api/admin/dashboard/setup-link`,
   valid 10 minutes, shown as a QR code) and open it on the tablet — it
   signs the tablet in for a year without typing the password.

If the port is already taken by another program, enabling (or changing
the port) is refused with 409 `DASHBOARD_PORT_IN_USE` and nothing changes.
If the port is taken at startup, Social Home starts anyway and logs a
warning; the admin view shows `running: false`.

## The address tablets use

Social Home guesses the address from the host name you reached it under
plus the dashboard port (for example `http://homeassistant.local:8100`).
(Under the Home Assistant App the host name comes from the
`X-Forwarded-Host` header HA's ingress proxy sets.) When the admin view
reports `url_host_allowed: false` — the dashboard would refuse that host
name — or the guess is otherwise wrong (a reverse proxy, a different host
name, TLS in front), set the real one as the dashboard **address**
(`{"address": "https://dashboard.example.lan"}`); it is then used for the
displayed URL and for setup links, and its host name is added to the
dashboard's `Host` allow-list.

## Home Assistant App

Inside the Home Assistant App the dashboard always listens on container
port `8100` (the port can't be changed there; HA Supervisor owns the host
port mapping). The App's manifest must declare `8100/tcp` so Supervisor
publishes it — that lives in the separate Home Assistant App repository
and is a follow-up there. Until the mapping exists, or when you map it to
a different host port, set the dashboard **address** to what the tablet
must open.

## Keep it on the home network

- **Do not port-forward the dashboard to the internet.** It is meant for
  devices on your LAN.
- **It is plain HTTP by default.** The password and the session cookie
  cross your network unencrypted. If the dashboard must be reachable
  beyond a trusted LAN, put it behind a TLS reverse proxy and set the
  dashboard **address** to the public `https://` URL — sign-ins through
  that address get a `Secure` session cookie. Tablets on the LAN can keep
  opening the plain `http://` LAN address; their cookie is not marked
  `Secure` (a browser would drop it over HTTP).
- **A reverse proxy must preserve the `Host` header.** Sign-in forms are
  accepted only when the browser's `Origin` matches the `Host` the
  dashboard receives, so a proxy that rewrites `Host` makes every login
  fail with 403. With nginx: `proxy_set_header Host $host;`.
- **Cookies are shared across ports on the same host.** Browsers don't
  isolate cookies by port, so any other web app served under the same host
  name (for example Home Assistant on `:8123`) receives the dashboard's
  cookie too. The cookie is `HttpOnly` and `SameSite=Strict`, and it only
  unlocks the dashboard, but use a dedicated host name if that matters to
  you.
- **Behind a reverse proxy, all tablets share one throttle bucket.** Login
  throttling keys on the connecting address (never on `X-Forwarded-For`,
  which anyone can set), so every client behind the proxy counts as one:
  five wrong passwords from any of them lock all of them out for up to 15
  minutes. Beyond that, over 30 failed logins in 15 minutes from anywhere
  hold every client back.
- **Only a few host names are answered.** Requests for host names that are
  not an IP address, `localhost`, `homeassistant`, this machine's own name,
  a home-network suffix (`.local`, `.lan`, `.home`, `.internal`,
  `.home.arpa`) or the configured address are refused (421) — protection
  against DNS rebinding.

## Privacy lock (guests, cleaners)

When someone who shouldn't see the household's people is around, tap
the 🔒 button on the tablet. It switches to a neutral lock screen: the
household name, the time and the date — no names, pictures, presence,
events or chores. Reloading the page or restarting the browser keeps it
locked (the lock lives in that tablet's sign-in cookie, so it is per
tablet: locking the hallway tablet doesn't lock the kitchen one).

To unlock, tap **🔒 PIN** and type the PIN on the keypad.

- **Set a PIN first.** The 🔒 button only appears once a household
  admin has set a PIN — `PUT /api/admin/dashboard/pin` with
  `{"pin": "4829"}` (4 to 6 digits).
- **The PIN is not the dashboard password.** It's a short code for the
  people who live there; the password stays with whoever sets tablets
  up. Setting or changing the PIN signs no tablet out.
- **Five wrong PINs sign the tablet out.** After five wrong tries within
  15 minutes the next one signs that tablet out, and it needs the
  dashboard password (or a fresh setup link) again. This is counted per
  tablet, so tablets behind one reverse proxy don't use up each other's
  tries.
- **That sign-out is remembered in memory only.** The signed-out cookie
  (and a copy of it) stays refused until Social Home restarts. To be
  sure a lost or copied tablet cookie is dead for good, use **"Sign out
  all tablets"** (`DELETE /api/admin/dashboard/sessions`) — that one
  survives restarts.
- **Removing the PIN** (`DELETE /api/admin/dashboard/pin`) unlocks every
  locked tablet and hides the 🔒 button.
- It is a privacy screen, not a safe: anyone who knows the dashboard
  password can still sign a tablet in.

## Signing tablets out

- **"Sign out all tablets"** (`DELETE /api/admin/dashboard/sessions`)
  ends every tablet session at once.
- **Changing the dashboard password** does the same.
- A lost or stolen tablet: do either of the above, then sign the
  remaining tablets in again (password or a fresh setup link).
