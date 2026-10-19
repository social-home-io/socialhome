# Wall-panel JSON API (`/api/display/v1/`)

A wall panel — an ESPHome LVGL display such as the 1280 × 800 panels the
[esphome-dashboard](https://github.com/social-home-io/esphome-dashboard)
firmware drives — shows the same household dashboard as the wall tablet,
but draws it itself from JSON. It talks to the **dashboard port** (default
`8100`), pairs with a short code and then sends
`Authorization: Bearer <token>` on every request. The endpoint reference
(status codes, rate limits) is in [api.md](../api.md#hfs--household-dashboard).

## What the JSON is

- **The tablet's boards, spelled as data.** `socialhome/dashboard/boards.py`
  builds the view models once; the HTML templates and
  `socialhome/dashboard/display_json.py` both spell them. A panel shows
  exactly what the tablet shows — the read model has already applied
  hidden people, child protection, night mode and the household-chat
  opt-in. Nothing the tablet doesn't show is in the JSON (no event
  locations, lesson notes, shopping stores, DMs or notifications).
- **Pre-formatted, in the household's words.** Every string a person reads
  comes from the server: times in the household time zone, relative
  phrases ("in 20 min", "4 min ago"), labels from the dashboard catalog in
  the dashboard locale. The firmware is locale-free and only lays things
  out. `GET /theme` carries the static UI strings (tabs, PIN screen,
  "Got it", night, offline, and the panel's own Wi-Fi / Improv prompt /
  address (with its "found on this network" list) / pairing / connect /
  update / settings screens). Placeholders stay in braces for the
  panel to fill: `{time}` in `offline`, `{host}` in `claim_unreachable`,
  `{n}` in `pin_remaining` and `pin_wait` (`pin_last_try` and
  `pin_forgot` are the lock screen's last-try warning and forgotten-PIN
  hint). The panel keeps English
  built-ins for the setup screens it shows before it has a server.
- **A multi-day event is one row.** An agenda row carries `days` — the
  event's days as one line, "Mon 26 – Sat 31 · 6 days", the way every
  calendar in the app spells it — and `day_count`; show `days` in place
  of `time` when it is set. `time` keeps the clock span it always had, so
  older firmware keeps working (both fields are additive in v1).
- **People once, then ids.** Each board lists the shown `people` (name,
  initials, colour + ink, avatar path, where they are — never coordinates
  — and a mention badge); items reference them by id.
- **Emoji are pictures.** An emoji is `{"text", "code"}`; draw
  `GET /api/display/v1/emoji/{code}?size=N`. `code` is `null` when the
  text is not an emoji (a zone without an emoji shows its first letter).
- **Colours** are validated `#rrggbb` (or `null` for the neutral look),
  with `ink` (black / white) where text sits on them; notes carry their
  softened light / dark fill and ink, as on the tablet.

## The map picture

A panel has no map engine, so `GET /map` comes with `image`:
`{url, width, height, etag}`. Fetch
`GET /api/display/v1/map.png?w=&h=&scheme=light|dark` for the picture —
`w` 200–1920 and `h` 200–1200 (clamped), default `width` × `height`
(748 × 560, the map area of a 1280 × 800 panel with the rail). The
picture is framed exactly like the browser's Map tab, with the zone
pins, the faces and "+N", stale people faded, edge chips for anyone far
away and the tile credit already drawn in — show it as-is. `etag` is the
`ETag` the default picture (default size, light scheme) has now: a panel
showing that one can compare it and skip the request while it is
unchanged. Every size and scheme has its own `ETag` — send the one you
hold in `If-None-Match` and an unchanged map is a `304`. A picture drawn
while some tiles (or someone's photo) were unreachable has a different
`ETag` and is redrawn a minute later. `503 TILES_UNAVAILABLE` (+ `Retry-After`) means
no tile could be loaded — keep the old picture or the placeholder.
Labels in a script the bundled Roboto lacks (Hebrew, Arabic, CJK) are
left off the picture; the "Who's where" list carries every name.

## Polling and caching

Every answer has an `ETag` and `Cache-Control: private, no-cache`. Send
the tag back in `If-None-Match`; an unchanged board is a `304` with no
body. The `clock` is truncated to the minute so a board changes at most
once a minute — keep the panel's own clock on SNTP and show it in
`clock.tz` / `clock.utc_offset_minutes`. `view_key` changes when the
layout itself must be rebuilt (night mode, the rail, locale, settings,
theme colour).

Budgets per panel: 120 JSON requests and 600 images a minute, 30 "Got
it" per 5 minutes (`429` + `Retry-After`); of the images, at most 6
freshly painted map pictures a minute (a new size, scheme or map — a kept
picture or a `304` is free).

## Pairing

- **With a code:** `POST /api/display/v1/pair` → show `code`, poll
  `GET /api/display/v1/pair/{pair_id}` until `{"status": "paired",
  "token", "display_id"}` (once).
- **Connected from Home Assistant:** Home Assistant calls the firmware's
  `connect_social_home` ESPHome action with one variable, `sealed`: the
  claim and the dashboard address sealed to the panel's own X25519 key
  (Home Assistant's recorder keeps action data, so it only ever holds
  ciphertext). Open it (below), then spend the claim right away at
  `{url}/api/display/v1/pair/claim` with `{"claim", "model", "firmware"}`
  and the headers `X-Display-Mac` — the MAC exactly as ESPHome reports it
  to Home Assistant — and `X-Display-Key` — the panel's public key, the
  one it was sealed to. `200` is the poll's paired shape
  (`pair-claim.schema.json`, fixture `pair-claim.json`); `404 NOT_FOUND`
  means the claim is wrong, expired, spent, for another panel or came
  with another key — ask the admin to press Connect again. Never log the
  sealed value, the claim, the private key or the token: ESPHome device
  logs stream to Home Assistant.
- The firmware publishes two diagnostic text sensors, named exactly:
  - **`Social Home node`** — its node name (`App.get_name()`): Home
    Assistant's device registry has only the friendly name, and Social
    Home needs the node name to build the action name;
  - **`Social Home key`** — its X25519 public key, 32 bytes as unpadded
    base64url (43 characters). Generate the keypair once and keep it in
    flash (NVS); make a new one on "Forget server" and factory reset.
    Without this sensor Social Home lists the panel as *needs update*.

### The sealed connect request

`sealed` is compact JSON (bytes as unpadded base64url — reject padding,
other alphabets, wrong lengths and stray low bits in the last character:
only the canonical encoding is valid):

```json
{"ct": "…", "epk": "<32 bytes>", "nonce": "<12 bytes>", "suite": "x25519-hkdf-sha256-aes256gcm"}
```

1. Check `suite` first: anything but `x25519-hkdf-sha256-aes256gcm`
   (missing included) is refused — no default, no fallback.
2. `shared = X25519(panel_private, epk)`; refuse an all-zero result.
3. `key = HKDF-SHA256(ikm = shared, salt = epk ‖ panel_pub, info =
   "socialhome-panel-claim-v1", L = 32)`.
4. `plaintext = AES-256-GCM-open(key, nonce, ct, aad = suite ‖ panel_pub)`
   (`ct` is ciphertext ‖ 16-byte tag, at most 2048 bytes; `suite` as
   ASCII, `panel_pub` the 32 raw bytes).
5. `plaintext` is `{"claim": "<43 chars>", "url": "<dashboard address>"}`
   — both strings, else refuse.

Every failure is one silent refusal (log no detail). The shared vectors
in `v1/fixtures/sealed-claim-vectors.json` — valid cases with their
intermediate values, invalid ones that must be refused, and key strings
to accept or refuse — are what the firmware's host tests open; Social
Home builds them from fixed keys (`tests/dashboard/test_claim_seal.py`).
- Send `X-Display-Mac` (plus `X-Display-Firmware` / `X-Display-Model`) on
  every request: an already-paired panel's display learns its MAC once
  that way, so Admin › Dashboard can show it as connected.

## Lock, unlock and "Got it"

- `POST /api/display/v1/lock` — locks the panel (only while the household
  has a PIN: else `409 DASHBOARD_PIN_REQUIRED`) and answers `200` with the
  lock screen (`lock.schema.json`). The lock is stored server-side: a
  reboot keeps it.
- `POST /api/display/v1/unlock` `{"pin": "1234"}` — the PIN way out; a
  locked panel reaches it (and `/unlock/ha` below). `200` `unlock.schema.json` (also when it wasn't locked);
  `403 DISPLAY_PIN_WRONG` with `params.remaining` (fixture
  `pin-wrong.json`) — show it as "Wrong PIN" with the lock body's
  `strings.tries_left` (`{n}` = `remaining`), or `strings.last_try` when
  `remaining` is `1` (it is never `0`); the fifth wrong PIN in 15 minutes
  unpairs the panel: `401 DISPLAY_SIGNED_OUT` (forget the token, pair
  again). `429` + `Retry-After` only asks to wait. Like the tablet, show
  `strings.forgot_pin` under the PIN pad, draw the lock screen with the
  night palette (and dim) while `is_night` is true, and close a PIN pad
  left untouched for a minute.
- `POST /api/display/v1/unlock/ha` (no body; one sent is ignored) — Home
  Assistant lifts the lock without the PIN: the panel calls it when its
  "Privacy lock" entity is unlocked from Home Assistant. Only while an
  admin turned on "Home Assistant may unlock wall panels" (off by
  default): the lock body's and every board's `settings.ha_unlock` say
  whether it is on (`ha_unlock` reflects the admin setting
  `panel_unlock_from_ha`) — else `403 DISPLAY_HA_UNLOCK_OFF` (the panel stays
  locked; checked whether or not the panel is locked). `200`
  `unlock.schema.json`, also when it wasn't locked (or no PIN is set —
  nothing to lift). A locked panel reaches it (no `423`). A lifted lock
  keeps the panel's wrong-PIN count (it does not refill the PIN budget —
  earlier wrong PINs still count until they age out of the 15-minute
  window); every one is logged and written to the auth audit log. 6 calls per 5 minutes per panel (refused ones count),
  then `429` + `Retry-After`.
- `POST /api/display/v1/ack` `{"message_id", "person_id"}` — send a
  mention's `ack` object as it came. `200` `ack.schema.json`;
  `404 NOT_FOUND` when the wall no longer shows that mention (refresh the
  board). 30 per 5 minutes per panel.

## Status codes a panel handles

| Status | Meaning | What the panel does |
|---|---|---|
| `404 NOT_FOUND` from `/pair/claim` | Wrong, expired, spent or another panel's claim, or not the key it was sealed to | Show "press Connect in Social Home again", fall back to setup |
| `401` | No / unknown token (also after "Sign out all tablets" or removal; `DISPLAY_SIGNED_OUT` after too many wrong PINs) | Forget the token, pair again |
| `403 DISPLAY_PIN_WRONG` | `POST /unlock` with the wrong PIN (`params.remaining` tries left) | Say "Wrong PIN", stay locked |
| `403 DISPLAY_HA_UNLOCK_OFF` | `POST /unlock/ha` while no admin allows Home Assistant to unlock wall panels | Stay locked; report the lock entity as still locked |
| `409 DASHBOARD_PIN_REQUIRED` | `POST /lock` while the household has no PIN | Hide the lock button (`settings.pin_set`) |
| `404 TAB_OFF` | The tab doesn't show (chat off, stickies off, nobody located) | Go to Overview |
| `404 NOT_FOUND` | Unknown or hidden person | Go to Overview |
| `409 NIGHT` | A tab other than Overview at night | Go to Overview |
| `423` | The panel is privacy-locked | Show the lock screen from the body (`lock.schema.json`); `GET /emoji/1f512` (the PIN button's 🔒) still answers |
| `429` | Over the request / ack / Home Assistant unlock budget, or the PIN can't be checked now | Wait `Retry-After` seconds |
| `503 TILES_UNAVAILABLE` | `map.png` without any map tile | Keep the old picture, retry after `Retry-After` |

## Versioning

- The path carries the major version (`/api/display/v1/`) and every JSON
  body carries `"api_version": 1`.
- **Additive changes keep v1**: a new field, a new optional section, a new
  enum value the schema documents. Panels must ignore fields they don't
  know. The schemas here are strict (`additionalProperties: false`) so a
  new field can't ship without being documented — they describe what the
  server sends, they don't ask panels to reject extras.
- **Breaking changes move to `/api/display/v2/`**: removing or renaming a
  field, changing a type or a meaning. v1 then stays served until the
  firmware that needs it is gone.

## Contract files

- [`v1/*.schema.json`](./v1/) — JSON Schema (draft 2020-12), one per
  endpoint plus `common.schema.json` (shared definitions) and
  `error.schema.json` / `lock.schema.json` / `unlock.schema.json` /
  `ack.schema.json` / `pair-claim.schema.json`, and
  `sealed-claim.schema.json` — the `connect_social_home` action's
  `sealed` variable (not an endpoint).
- [`v1/fixtures/`](./v1/fixtures/) — real answers for a seeded household
  (four people, a school day, the chat on, mentions, people on the map).
  `tests/dashboard/test_display_api.py` validates every endpoint's real
  payload against its schema and compares it with these fixtures;
  regenerate them after an intended change with
  `SH_UPDATE_DISPLAY_FIXTURES=1 pytest tests/dashboard/test_display_api.py`.
  The firmware repository pins the same fixtures for its screenshot tests.
  `v1/fixtures/sealed-claim-vectors.json` is no answer but the sealed
  connect request's test vectors (see above), built by
  `tests/dashboard/test_claim_seal.py` (same regeneration switch).

## Map attribution and font

The map picture carries its tile provider's credit (© OpenStreetMap
contributors) in its bottom-right corner, as the licence requires —
don't crop it. Its text is drawn with **Roboto** (SIL Open Font License
1.1, `socialhome/dashboard/fonts/OFL.txt`), the font the firmware uses.

## Emoji attribution

The emoji images are **Twemoji** (<https://github.com/jdecked/twemoji>,
v17.0.3, 72 × 72 PNGs), © Twitter, Inc and other contributors, © jdecked
and other contributors, licensed under
[CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/). Only the subset
the dashboard can show is bundled (`socialhome/dashboard/static/emoji/`,
licence note alongside); `scripts/fetch_twemoji.py` refreshes it. Nothing
is fetched at runtime. Panels showing these images should credit Twemoji
(e.g. on their about / settings screen).
