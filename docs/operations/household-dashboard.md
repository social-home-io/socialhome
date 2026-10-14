# Household dashboard (wall tablet)

The household dashboard is a view of the household for a tablet on the
wall. It is served by Social
Home on its **own port** (default `8100`), separate from the main app
(`8099`), and is unlocked with one household-wide **dashboard password** —
the tablet never holds a member's account.

It is **off by default**. Design notes:
[architecture.md](../architecture.md#household-dashboard-own-port); API:
[api.md](../api.md#hfs--household-dashboard).

Everything below is set up in the app under **Admin › Dashboard** (household
admins only). Each task also names the admin API call it makes — a
*headless / scripting* reference for setting a dashboard up without the
app.

## Enabling it

1. Open **Admin › Dashboard** and, under **Password**, choose **Set
   password** (at least 8 characters). *Headless:*
   `PUT /api/admin/dashboard/password`.
2. Flip the **Household dashboard** switch on. Outside the Home Assistant
   App you can change the **Port** (default `8100`) and **Save port**.
   The listener starts at once; the tab shows "Running on port 8100" and,
   under **Open on the tablet**, the address tablets should open.
   *Headless:* `PUT /api/admin/dashboard` with `{"enabled": true}` (and
   optionally a `port`); `GET /api/admin/dashboard` reports
   `running: true` and the `url`.
3. On the tablet, either open that address and type the password, or —
   while the dashboard is running — choose **Show sign-in QR** and scan
   the code with the tablet's camera: it signs the tablet in for a year
   without typing the password (the code works once and is valid for 10
   minutes; **New sign-in link** makes another). *Headless:*
   `POST /api/admin/dashboard/setup-link`.

If the port is already taken by another program, enabling (or changing
the port) is refused — the tab says so (*headless:* 409
`DASHBOARD_PORT_IN_USE`) and nothing changes. If the port is taken at
startup, Social Home starts anyway and logs a warning; the tab shows
"Switched on, but not running" (`running: false`).

## The address tablets use

Social Home guesses the address from the host name you reached it under
plus the dashboard port (for example `http://homeassistant.local:8100`).
(Under the Home Assistant App the host name comes from the
`X-Forwarded-Host` header HA's ingress proxy sets.) When the tab warns
"This address won't open the dashboard" (*headless:*
`url_host_allowed: false`) or the guess is otherwise wrong (a reverse
proxy, a different host name, TLS in front), choose **Edit address**
under **Open on the tablet** and enter the real one (for example
`https://dashboard.example.lan`; **Use the best guess** goes back). It is
then used for the displayed address and for sign-in QR codes, and its
host name is added to the dashboard's `Host` allow-list. *Headless:*
`PUT /api/admin/dashboard` with `{"address": "https://dashboard.example.lan"}`.

## Home Assistant App

Inside the Home Assistant App the dashboard always listens on container
port `8100` (the **Port** field in **Admin › Dashboard** is read-only there;
HA Supervisor owns the host port mapping). The App publishes it on host
port `8100` by default — nothing listens there until you switch the
dashboard on, so there is nothing to set up in Home Assistant.

Social Home reads the mapped port from Supervisor and shows the right
address automatically (for example `http://homeassistant.local:8100`).
If port `8100` is already used on your Home Assistant host, pick another
host port under **Settings › Apps › Social Home › Network**, save, and
restart the App when Home Assistant asks — the address follows. If you
clear the host port there, **Admin › Dashboard** asks you to turn it back
on (the API reports `host_port_mapped: false`).
App releases that don't offer the dashboard port yet have no such entry
under **Network**: the admin view then reports `host_port_mapped: null`
and the address uses `8100` — update the App, or set the dashboard
**address** to whatever reaches the tablet.
If tablets must use a different name or a reverse proxy, set the
dashboard **address** — it always wins over the guess.

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

## What the tablet shows

- **Home** has up to three tabs at the top — **Overview**, **Messages**
  and **Notes** — while household messages are on and/or notes are
  turned on (with neither, there is no tab bar). Tapping 🏠 in the rail
  always brings back Overview, and so does leaving the tablet alone on
  Messages or Notes for the "Back to Home after" time. The Messages tab
  shows a small number when there are messages under 15 minutes old
  ("new" — the tablet can't know who has read what). At night the tabs
  are hidden and the tablet always wakes on Overview.
- **Overview** (`/`): the time and date with who's home and who's away
  under it (a zone name like "Work" at most — never a map or
  coordinates), the next thing coming up today (with whose it is and
  "in 28 min"), then **Today**:
  one lane per person from 08:00 to 22:00 (stretched for earlier / later
  plans) with their calendar events, a striped school block from their
  timetable and a "now" line; under it who is at school right now and
  today's timetable changes (⚠ cancelled, 🔁 changed, + added). Last, the
  chores due today (✓ done, ☐ to do, ⚠ "2 days late"), the shopping list
  (the first six and "+ 5 more") and the first three household sticky
  notes ("Notes · 6" with **See all ›** when there are more). Empty
  columns are left out; a day with nothing on it says "Nothing planned
  today 🎈". With more than six people — and on a phone — the lanes become
  one list in time order; with one person there is no rail and no "who's
  home". A bar too short for its title shows just an icon (its leading
  emoji, a guess from its words, else 🔹). When nothing is
  left today, the "Next" box shows tomorrow's first item instead.
- **Messages** (`/messages`, part of *Show household messages*): the
  household chat of the last day, newest first under **Today** and
  **Yesterday**, each with the sender's face and name, "new" and the
  time it was sent. A long message is cut short with "🔊 tap to hear the
  rest"; tap a row to hear the whole message, tap again to stop. With
  nothing in the last day it says "No messages in the last day".
- **Notes** (`/notes`): every household sticky note on a flat board in
  its own colour; "No notes yet" when there are none.
- **Map** (`/map`): who is where — the household's zones (Admin ›
  Zones; on Home Assistant mirrored from HA's zones) with everyone in a
  zone in one bubble of photos at the zone, and anyone outside every
  zone at their own position (the same ~11 m precision as the app). The
  **Who's where** list beside it says each person's place and how long
  ago it was updated; after an hour a face fades ("last seen 2 h ago"),
  after a day the person drops off. Tap a name or a bubble to zoom to
  them; "Show everyone", the same tap again or a minute without a touch
  goes back. The tab only appears while someone shares their location
  (or is in a known zone); it is hidden at night and behind the 🔒
  privacy lock — **lock the tablet when guests are around**, since the
  map shows where family members are. Who appears: every shown member
  (Admin › Dashboard › People) with a location — child-protected minors
  too, like everyone else (the owner's decision); a person you hide
  there is never on the map, and with the household's presence feature
  off there is no Map at all. Personal blocks between members don't
  apply on the wall, which has no viewer of its own. A phone that Home
  Assistant reports as unavailable is left off the map rather than
  shown as away. Map tiles are fetched by Social
  Home (the tablet never contacts a map server); the map credits
  OpenStreetMap. Wall panels get the same map as one picture, drawn by
  Social Home from the same tiles — a few hundred kilobytes, fetched
  again only when someone moves (or a minute after the tile server was
  unreachable, when the missing squares show plain).
- **A person** (tap their face in the rail): what they're doing now and
  next, their school day with rooms and teachers (a cancelled lesson is
  struck through *and* says "Cancelled"), today's and tomorrow's plans
  and their chores. After the "Back to Home after" time without a touch
  (setting `idle_return_minutes`, default 3, `0` = never) the tablet goes
  back to Home.
- **A person in picture view** — for children who can't read yet.
  Pictures are what the child reads: a big analog clock with the real
  time; a **NOW** card (thick frame) ➜ a **NEXT** card, each one
  big emoji; a coloured wedge on the clock (and a small pie on the NEXT
  card) shows the time left until NEXT starts — like a kitchen "time
  timer", it shrinks as the minute hand eats it and is a full disc from
  an hour out. Under it **my day**: every event and lesson as an emoji
  tile under 🌅 morning, ☀️ afternoon, 🌇 evening and 🌙 night — past ones
  faded, the running one framed, a cancelled lesson grey with a big ✕.
  Then tomorrow (one 🌙 per sleep, then tomorrow's first things — 🎒 for
  school), today's jobs (emoji with ☐ / ✓) and who's home (faces under
  🏠 and 🚗). An empty day is one big 🎈. At night it shows the analog
  clock, 🌙 and the icon of the morning's first thing. The icon is the
  lesson's own timetable icon, else the emoji a title starts with
  ("🏊 Swimming"), else a guess from the title's words in English,
  German, Spanish, French or Dutch ("Zähneputzen" → 🦷), else a neutral
  🔹 — start a title with an emoji to pick it yourself. Under every
  picture sits a small, muted **helper caption** so a grown-up or an
  older sibling can explain it: the title under a tile, the chore's name
  under a job, the title and time on NOW / NEXT ("Swimming · 15:00"), one
  short line of a message, and at night "Tomorrow · School 07:30"; each
  section has a tiny label (Now, Next, My day, Tomorrow, My jobs, Who's
  home). They are always on — there is no setting — and stay quiet next
  to the emoji. Every tile still tells a screen reader its real title
  and time (the captions are hidden from it, so nothing is read twice).

  Picture view is **on by default for child-protected minors** and off
  for everyone else ("Default (child account)" next to the switch). An
  admin turns it on or off per person with the **Picture view** switch
  under **People on the dashboard** in **Admin › Dashboard**; **Reset to
  default** goes back to the default. An open tablet switches on its next
  refresh. *Headless:* `PUT /api/admin/dashboard` with
  `{"people": {"<user_id>": {"picture": true}}}` (`false` = reader page,
  `null` = back to the default); `GET /api/admin/dashboard` lists each
  person's stored `picture` and the resolved `picture_effective`.
- **Household messages** (off by default — Admin › Dashboard › Display ›
  *Show household messages*, `show_household_messages`; needs the
  household chat turned on). Overview shows a **Messages** card right
  under "Next": the last three messages of the household chat from the
  past six hours — the sender's face, the text (two lines at most) and
  when (with **See all ›** to the Messages tab, which lists the whole
  last day); one
  under 15 minutes old is marked "new" with "4 min ago". A photo, video
  or file says "📷 Photo" / "🎬 Video" / "📎 File" (its caption isn't
  shown), a voice note shows its transcript (or "🎤 Voice message"). Only
  the household chat — never DMs, space chats or the feed — never a
  shared location, and never messages between a child and someone a
  guardian blocked for them: the blocked person's messages don't show,
  and neither do the child's while that person is in the household. A picture-view
  page shows up to three message faces with a 💬 next to NOW ➜ NEXT
  (a new one's 💬 sits on a coloured circle), each with one short line of
  the text under it. Tap a message to have it
  read aloud ("Anna says: …") — only on a tap, never on its own, a second
  tap stops it. Speech never leaves the tablet: only a voice installed
  on the tablet itself is used, never an online one — a tablet without an
  on-device voice for the dashboard's language shows no 🔊, and a tap does
  nothing. Nothing is shown at night, on a
  locked tablet or on a reader person page, and the card disappears when
  there's nothing new.

  **What parents should know.** Anyone standing at the tablet — guests,
  the cleaner, a babysitter — can read these messages, so keep private
  things in a DM. To take a message off the wall, delete it in the app;
  it disappears with the tablet's next refresh (within 30 seconds).
  Reading a message on the tablet doesn't mark it read for anyone — if
  it's urgent, call.
- **Mentions** (part of *Show household messages*). Write `@here` in the
  household chat and Home shows a banner on top of every tab — the
  sender's face and name, the message and a big **✓ Got it** — for up
  to an hour. While the banner is up, that message isn't repeated in the
  Messages card or on the Messages tab. Tapping
  Got it on any tablet clears it on all of them and adds a ✅ reaction to
  the message in the chat, so you see it was seen. Write `@leo` and
  Leo's face in the left rail (or the face strip on a phone) gets a dot,
  and his page shows the message on top with **Got it** — in picture
  view as your face with a 💬 (tap to hear it) and a big ✓. That ✅ is
  credited to Leo: it's his page. It clears when Leo taps it or after an
  hour. Mentions of someone hidden on the dashboard don't show. A new
  mention plays a short, soft tone on the tablet (*Play a sound for
  mentions*, `mention_sound`, on by default) — never at night, and only
  once the tablet's browser allows sound (a first tap on the screen).
- **Night** (between `night_start` and `night_end`, default 22:00–06:00):
  only a dim clock, the date and the first thing of the morning. Tap the
  screen to see everything (and the 🔒 button) for a minute.
- **When Social Home can't be reached** the last page stays up with a
  banner "Can't reach Social Home — showing info from 14:02" and the
  "in 28 min"-style times hidden; it recovers on its own (↻ reloads now).
- With **Keep screen on** (`keep_screen_on`) the browser is asked to keep
  the screen awake; some kiosk browsers also need their own setting.

**Display settings.** Under **Display** in **Admin › Dashboard**: the
tablet's **Language**, **Back to Home after** (`idle_return_minutes`),
**Night mode** with its start and end, **Keep screen on**
(`keep_screen_on`) and **Show status text**. *Headless:*
`PUT /api/admin/dashboard` with those fields.

**Privacy reminders.** Everything on these pages is visible to anyone
standing in front of the tablet: event titles, chores, the shopping list,
sticky notes, who is home and timetables. Hide a person who shouldn't be
on the wall — switch **On dashboard** off for them under **People on the
dashboard** in **Admin › Dashboard** (*headless:*
`people: {"<id>": {"show": false}}`) — they then have no page and their presence, events, chores and timetables are left out of
Home. DMs, notifications, space chats, event locations and coordinates
are never shown; the household chat only when you turn on *Show household
messages* (see above), and never a hidden person's messages. Use the privacy lock below when visitors are around.

## Privacy lock (guests, cleaners)

When someone who shouldn't see the household's people is around, tap
the 🔒 button on the tablet. It switches to a neutral lock screen: the
household name, the time and the date — no names, pictures, presence,
events or chores. Reloading the page or restarting the browser keeps it
locked (the lock lives in that tablet's sign-in cookie, so it is per
tablet: locking the hallway tablet doesn't lock the kitchen one).

To unlock, tap **🔒 PIN** and type the PIN on the keypad.

- **Set a PIN first.** The 🔒 button only appears once a household
  admin has set a PIN — **Set PIN** under **Privacy lock** in
  **Admin › Dashboard** (4 to 6 digits). *Headless:*
  `PUT /api/admin/dashboard/pin` with `{"pin": "4829"}`.
- **The PIN is not the dashboard password.** It's a short code for the
  people who live there; the password stays with whoever sets tablets
  up. Setting or changing the PIN signs no tablet out.
- **Five wrong PINs sign the tablet out.** After five wrong tries within
  15 minutes the next one signs that tablet out, and it needs the
  dashboard password (or a fresh setup link) again. After each wrong PIN
  the keypad says how many tries are left ("Tries left: 3"), like a wall
  panel does. This is counted per
  lock on each tablet — tablets behind one reverse proxy don't use up
  each other's tries, and neither reloading, re-tapping 🔒 on a locked
  tablet, changing network address nor flooding the login page from
  other addresses starts the count over.
- **That sign-out is remembered in memory only.** The signed-out cookie
  (and any copy of it) stays refused until Social Home restarts; after a
  restart it is a *locked* tablet again — still behind the PIN, with
  five fresh tries. Likewise a copy of a tablet's cookie taken before it
  was locked works unlocked again after a restart. To be
  sure a lost or copied tablet cookie is dead for good, use **Sign out
  all tablets** in **Admin › Dashboard** (*headless:*
  `DELETE /api/admin/dashboard/sessions`) — that one survives restarts.
- **Remove PIN** in **Admin › Dashboard** (*headless:*
  `DELETE /api/admin/dashboard/pin`) unlocks every locked tablet and hides
  the 🔒 button.
- It is a privacy screen, not a safe: anyone who knows the dashboard
  password can still sign a tablet in.

## Wall panels (ESPHome)

Besides tablets, the dashboard can feed small always-on **wall panels** —
for example an ESPHome display. A panel doesn't sign in with the
password; it pairs once with a short code and then uses its own device
token.

1. Turn the dashboard on (a panel can only ask for a code while it runs).
2. Start pairing on the panel: it shows a **6-character code**, valid for
   10 minutes.
3. In **Admin › Dashboard › Wall panels**, choose **Add a panel**, type the
   code (case doesn't matter) and give the panel a name, e.g. *Kitchen*.
   *Headless:* `POST /api/admin/dashboard/displays` with `{code, name}`.
4. The panel picks up its token by itself and starts working.

The list shows each panel's model, firmware and when it was last seen.
**Rename** changes the name; **Remove** unpairs a panel at once (pair it
again with a new code to use it again). **Sign out all tablets** and a new
dashboard password unpair every panel too.

### How a panel finds Social Home

While the dashboard is on, Social Home announces it on your home network
(mDNS / Bonjour, service `_socialhome-dash._tcp`), so a panel can find
it by itself — you don't type an address. The announcement says only
"a Social Home dashboard is here": a short random-looking id, the port
and the API version. It never carries your household's name, anyone's
name or what the dashboard shows. It stops as soon as you switch the
dashboard off, and follows the host's address within a minute when it
changes (e.g. a new DHCP lease).

- **Firewall:** the host must allow multicast DNS — UDP port `5353` to and
  from `224.0.0.251` (and `ff02::fb` for IPv6) — on the home network. If
  it doesn't, panels can still use the address shown in **Admin ›
  Dashboard**.
- **Docker:** run the container with `network_mode: host` (or
  `--network host`). On Docker's default bridge network the announcement
  never leaves the container: the log says it announced the container's
  internal address (`172.17.x.x` or similar), but no panel can see it —
  the dashboard itself keeps working, and panels need the address typed in.
- **Home Assistant App:** the App doesn't announce itself — it runs on
  Home Assistant's internal network, where multicast doesn't reach your
  home network. Connect the panel from Home Assistant (below), or enter
  the address shown in **Admin › Dashboard** on the panel (for example
  `homeassistant.local:8100`).
- **Bound to one address:** if `listen_host` is a specific address only
  that address is announced; `127.0.0.1` announces nothing.
- **Changed IP address:** the addresses are read when the dashboard
  starts. If the host gets a new address while running, switch the
  dashboard off and on (or restart Social Home).

### Connect a panel from Home Assistant

With Social Home running alongside Home Assistant (`ha` mode or the Home
Assistant App), you don't type a code at all:

1. **Adopt the panel in Home Assistant**: Settings › Devices & services ›
   ESPHome — the panel shows up as discovered once it's on your Wi-Fi.
2. In **Admin › Dashboard › Wall panels › Found in Home Assistant**, press
   **Connect** next to it. Social Home asks Home Assistant to pass the
   panel the dashboard's address and a one-time claim, sealed so only
   that panel can read them; the panel pairs
   itself within seconds and shows up in the list above, named as in Home
   Assistant.

- **Set the dashboard address** (Admin › Dashboard) if Connect says so:
  Social Home sends the panel the address you set, or else the one it
  guesses from how you reached Social Home — a guess that is `localhost`
  or a public internet address is refused. On the Home Assistant App, the
  dashboard port must be turned on (Settings › Apps › Social Home ›
  Network).
- **Offline in Home Assistant**: Home Assistant can't reach the panel —
  check it's on. **Update the panel's firmware**: its firmware is too old
  for Connect (or its *Social Home node* or *Social Home key* entity is
  disabled in Home Assistant — enable it); pair it with a code meanwhile.
- **Connect again** on a connected panel replaces its pairing (e.g. after
  a factory reset). The old pairing keeps working until the panel uses
  its new one for the first time — usually seconds later — and is then
  removed; until then Admin › Dashboard lists the panel twice (the new
  entry "never seen"). A privacy lock on the panel stays on. **Remove**
  unpairs the panel together with a re-connect still waiting, and stops
  it being listed as connected.
- **What Home Assistant sees.** Home Assistant's recorder keeps every
  action's data, so Social Home never sends it anything readable: the
  claim and the dashboard address are sealed to the panel's own key (the
  *Social Home key* entity — its public half; the private half never
  leaves the panel). Home Assistant, its recorder and its history only
  ever hold that ciphertext and the panel's public key. The claim works
  once, for that panel (its MAC and key) only, for 10 minutes, and
  **Sign out all tablets** cancels it. The panel's long-lived token never
  passes through Home Assistant. The seal hides the request; it doesn't
  prove who sent it: whoever can run the panel's actions in Home
  Assistant can also send it a connect request of their own (or reflash
  it), so Connect trusts the panel Home Assistant shows you — as before.
- **If a claim got out anyway** (it can't be read from Home Assistant,
  but say the panel itself was compromised): spending it before the
  panel does also needs the panel's MAC and public key (both visible in
  Home Assistant) and gives a token for a new display named after the
  panel. That alone doesn't break the panel: its current pairing keeps
  working, and the next **Connect** drops the unused new display. Only
  when that token is *used* does it take the panel's place — the panel's
  own token then stops working and it shows "not paired". If a panel
  unexpectedly says "not paired", remove the unknown display and Connect
  again.

Five wrong codes in 15 minutes hold pairing back for a while — check the
code on the panel. This limit is separate from tablet sign-in: failed
password tries on the dashboard never hold pairing back, and wrong codes
never hold a tablet's sign-in back. At most 20 panels can be paired.

**Privacy lock on a panel.** A panel locks with the same household PIN as
the tablets (it needs a PIN to be set) and then shows only the lock
screen until someone types the PIN on it. Each panel locks on its own,
and the lock is stored on the server, so restarting the panel — or Social
Home — doesn't unlock it. **Five wrong PINs unpair the panel**: after five
wrong tries within 15 minutes, the next try signs it out (like a tablet)
and it has to be paired again with a new code. A locked panel shows
**Locked** in **Admin › Dashboard › Wall panels**; **Unlock** there lifts
the lock without the PIN (*headless:* `PATCH
/api/admin/dashboard/displays/{id}` with `{"locked": false}`). **Remove
PIN** unlocks every panel too; setting a new PIN afterwards doesn't lock
them again.

**Got it on a panel.** A panel that shows household-chat mentions can
send "Got it" like the tablet — the same rules (only what the wall shows
right now, never at night or while locked) and the same limit of 30 per
5 minutes, per panel.

> **Firmware comes later.** This release adds pairing and device tokens
> only; the panel firmware and the data the panels show arrive in a
> later release. The panel API is documented in
> [api.md](../api.md#hfs--household-dashboard).

## Signing tablets out

- **Sign out all tablets** in **Admin › Dashboard** ends every tablet
  session at once. *Headless:* `DELETE /api/admin/dashboard/sessions`.
- **Change password** under **Password** does the same. *Headless:*
  `PUT /api/admin/dashboard/password`.
- Both also unpair every wall panel — pair them again with a new code.
- A lost or stolen tablet: do either of the above, then sign the
  remaining tablets in again (password or a fresh setup link).
- **Social Home can sign every tablet out by itself.** It remembers
  locked and signed-out tablet sessions in a list of up to 10,000 until
  each one's cookie would have expired anyway. Should that list ever
  fill up with sessions that could still come back — only possible
  with thousands of correct password or PIN entries — it changes the
  tablets' cookie key instead of forgetting one: every tablet then
  needs the dashboard password again, wall panels stay paired, and the
  log says `revocation list full of live sessions`.
