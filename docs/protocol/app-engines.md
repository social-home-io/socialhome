# Social Home Apps — app engines (v_78)

**Summary.** An *app engine* is a Social Home feature an app calls at its
group tables: the app asks and hears the state; every household at the
table does the work itself (including the cryptography); anything private
shows only in Social Home's own screens. The first engine is the **gift
draw** (`draw`) — Gift draw / Wichteln / Lootjes trekken / Père Noël secret /
Amigo invisible — where every person is drawn one other person to give a
gift to, and nobody, not even the organiser or the host household, sees who
drew whom. The design already names a later engine: a "deal" (each person
secretly gets one of N cards or roles).

**Scope.** Group tables ([`app-groups.md`](./app-groups.md)) — picked tables
over the pairing, space tables over `SPACE_APP_SESSION`. Capability v_78
(`FederationCapability.MIN_FOR_APP_ENGINES`).

## The engine namespace

```
socialhome/app_engines/
  registry.py      AppEngine (Protocol) + AppEngineRegistry: the one gate
  domain.py        VERB_ENGINE, ENGINE_SUITE_FIELD, EngineCall, the gate's errors
  draw/
    crypto.py      the gift draw's cryptography (pure functions)
    domain.py      bounds, steps, settings, coded errors (pure)
    service.py     AppDrawService: the hub's rounds and each household's steps
socialhome/routes/app_engines.py              one route family, dispatched by the registry
socialhome/repositories/app_engine_state_repo.py   app_engine_state (migration 0138)
client/src/features/apps/engines/             engineBridge.ts (engine.<id>.*) + draw/ (the result sheet)
```

An engine (`AppEngine`) has an `engine_id`, the `suites` its wire shapes may
carry, the `steps` it sends between households, and `on_inbound` / `call` /
`table_changed` / `person_left` / `stop`.

**The gate.** An app declares the engines it uses in its manifest:

```json
{ "tables": true, "players": { "min": 3, "max": 30 }, "engines": ["draw"] }
```

(`engines`: up to 8 distinct ids, `[a-z][a-z0-9_]{0,31}`; an unknown id is
kept — a newer engine — but never callable.) Core lets an app call only an
engine it **declares** and this Social Home **has**: the route answers
`403 APP_ENGINE_NOT_DECLARED` / `409 APP_ENGINE_UNSUPPORTED`, the bridge
`engine_not_declared` / `engine_not_supported`. An inbound step is taken only
for an engine this household has, one of its suites and steps, and a table
whose app — **as installed here** — declares it (never the sender's word).
The Apps page and the consent dialog say what an engine does in Social
Home's own words ("Runs gift draws with Social Home — Social Home draws the
names in each household and shows everyone whom they give to in its own
screen. Gift draw never learns who gives to whom.").

**On the wire.** No new event type: every engine shares ONE sealed
`APP_SESSION` verb, `engine`, between a table's hub and each player's
household:

```
{verb: "engine", engine: "draw", engine_suite: "x25519-hkdf-aesgcm256",
 step, gen, round, …}
```

**Routes.** `GET /api/apps/{app}/groups/{table}/engines/{engine}` (the
state as the app may know it) and `GET|POST …/engines/{engine}/{action}`
(the engine's own actions). **Storage.** `app_engine_state` — one
KEK-wrapped blob per (table, engine), gone with its table. **Bridge.**
`engine.<id>.<call>`, events `{type: 'engine:event', engine, event}`,
`app.context.engines` = the engines the app may call.

## The gift draw (`engine: "draw"`)

### What people do

The organiser opens a table in the Gift draw app, sets a name, a budget, the
exchange day and the couples who mustn't draw each other; everyone joins and
writes a wish list (≤ 500 characters); the organiser taps **Draw names**.
The households draw by themselves; each person gets one row "Your gift draw
is ready — see who you're giving to", which opens Social Home's own sheet:
"You're giving a gift to Ben (Familie Okafor)", Ben's wish list, the budget
and the day, "Only you can see this. Ben won't know it's you."

### What holds, and against whom

Threat model: an honest-but-curious organiser, participants and host
household (the hub, which passes every message on); a tampering app; a
cheating household that deviates — it must be **detected**, and must not be
able to steer a result.

**What the draw guarantees:** no other participant, no organiser through
Social Home, no app, no GFS and no relay learns who gives to whom — each
person learns only their own recipient. **What it doesn't:** a household's
*server* (its admin reading its own database) holds its people's box keys
and results; a box at circle position `j` gives to `j + 1` and receives
from `j − 1`, so when two of its people sit two places apart it can work
out the pair between them — who gives to one of its own people. Households
that collude pool what they know. This is a documented limit, like the one-
and two-household cases (owner decision 2026-11-01); the app doesn't point
it out. `tests/protocol/test_app_engine_draw.py` pins it
(`test_accepted_limit_*`), so a change that closes it is noticed.

Chance (circle uniform) that a household with *m* of the *n* people can work
out at least one pair whose giver is in another household:

| n people | m of them in one household | chance |
|---|---|---|
| 6 | 2 | 40 % |
| 8 | 3 | 57 % |
| 12 | 2 | 18 % |
| 12 | 4 | 64 % |
| 20 | 4 | 47 % |
| 30 | 5 | 50 % |

| Party | Learns |
|---|---|
| A participant (incl. the organiser) | the people, the round's box keys `S` (random), the circle of boxes, **their own recipient's card** |
| Their household (its server) | the same for each of its own people (it holds their keys, as it holds their DMs) — and, from those, sometimes who gives to one of its own people (above) |
| The hub (as relay) | the above for its own people, plus which household sent which sealed card — cards are key-private and all 1 KiB. `tests/protocol/test_app_engine_draw.py` pins that everything the hub stores and sees, with random bytes blanked, is identical whoever draws whom |
| A mixing household | its own shuffle's input and output; **the circle doesn't follow any shuffle** (commit-reveal order) |
| A non-member relay household, the GFS | ciphertext, sizes, timing |
| The app | the people, the state, the person's own wish list — never a result |
| A recipient | nothing about their giver |

### One round

```mermaid
sequenceDiagram
    autonumber
    participant H as Hub (organiser's household)
    participant X as Each participant household (×h, the hub too)
    H->>X: start {people, identities, settings, because}
    X->>H: key {mixer pub, sig}
    H->>X: mixers {keys in mixing order}
    Note over X: per person: seed → box key, nonce, onion (deterministic);<br/>commit H(ctx,person,box,nonce); commit H(ctx,household,r)
    X->>H: onions {onions, commits, r_commit, sig}
    loop each household in mixing order
        H->>X: mix {k, batches | list + prev signature}
        Note over X: take only the previous mixer's signed output,<br/>peel, check, shuffle, sign (in, hash of every out entry) — once per round<br/>input doesn't peel? → open {seeds, stuck: k} (a void)
        X->>H: mixed {k, list, in, sig}
    end
    H->>X: list {S, batches, chain of per-entry hashes}
    Note over X: check the chain and every signature; every key in S usable?<br/>own boxes in S? else open {seeds} (void) / fail {missing, seed}
    X->>H: reveal {r}
    H->>X: reveals {every r} → circle = S sorted by H(ctx ‖ R ‖ box)
    Note over X: seal each own card to the box before it (its giver)
    X->>H: commit {card hashes, sig}  (before any card is sent)
    H->>X: commits {every household's}
    X->>H: cards {sealed cards}
    H->>X: cards_all (exactly the committed cards)
    Note over X: exactly one usable card opens per person?<br/>no → fail {cards, own seed}: the cards that open there name their sealers<br/>a partner? → redo {complaint with seed}
    X->>H: done {count, sig}
    H->>X: complete {every signed done} → "Your gift draw is ready"
```

**A void** — before any card exists: a key in `S` nobody can seal to, or a
mixer whose signed input doesn't peel. Every household sends `open {seeds}`
(signed; the hub keeps only a validly signed one), the hub
passes on a fresh `start {because: void, why, openings[, k, batches,
chain]}`, and every household — finding its own opening there exactly as it
sent it — runs the same check on its own copy of the list and names the
same households (`blamed`, shown to everyone at the table). Only a validly
signed opening that doesn't rebuild what its household signed names that
household; a missing or unsigned one proves nothing and the restart is
refused.

**A completed round is never opened.** After the cards, only a card
complaint that named nobody (below) opens that one round — before it can
complete: a household that opened never takes its `complete`, and a void
or a complaint after `complete` is refused. Nothing ever opens a seed after
the list.

1. **Start.** People (seat, household, username), each household's identity
   key, the settings. Every signature and seal is bound to the **round
   context** `SHA-256(engine ‖ suite ‖ table ‖ gen ‖ round ‖ people ‖
   identity keys ‖ settings)` and every seal label starts with the engine id
   (`draw|…`). A household takes a first round / Draw again only when the
   people are **exactly** the seated people of its own copy of the table (no
   invisible extras). Identity keys: the stored pairing key, or one that
   derives to the household id. Mixing order: households sorted by
   `SHA-256(table ‖ gen ‖ round ‖ household)`.
2. **Keys and onions.** A fresh signed X25519 mixer key per household. Per
   person a fresh 32-byte **seed**; the box key, the commitment nonce and the
   onion's ephemeral keys all derive from it, so the onion is deterministic:
   a complaint that reveals the seed lets anyone rebuild it byte for byte.
   Each household also commits to a random `r`.
3. **The mix (signed chain).** Mixer k takes only exactly the previous
   mixer's signed output (the first: every household's signed onions), peels
   its layer, checks every entry, shuffles, signs `(k, in, out)` — `out` the
   digest of **every output entry's hash**, which the chain carries (n × 32 B
   a link) — once per round. A seed rebuilds its person's entry at every step
   of the mix, so it shows **which mixer's signed output lacks it**. A mixer
   whose signed input doesn't peel says so with its seeds (`open {stuck:
   k}`): the round is void.
4. **Check.** Every household verifies the whole chain from hashes and that
   every key in `S` is one it can seal to (a low-order key voids the round
   **publicly**: every household opens its seeds; whoever's opening doesn't
   rebuild what it signed is named, and so is the first mixer whose output
   lacks someone's entry — the one that put the key there). A household
   whose own box is missing says so with the seed — **instead of** revealing,
   decided from the list alone, before any card exists — and the chain names
   the mixer that dropped it (refused when that is the complainer's own
   mixer: it proves nothing against anyone, and names the complainer).
5. **The circle.** Every household reveals `r`; `R = H(ctx ‖ every r)`; the
   circle is `S` sorted by `H(ctx ‖ R ‖ box)` — no mixer, the last one
   included, chooses who sits next to whom. The box at position `j` gives to
   `j + 1`.
6. **Cards.** Each person's card (name and household as their own household
   knows them, the wish list, signed by that household for this round,
   padded to exactly 1 KiB) is sealed key-private to the box at `j − 1`.
   Every household **commits to its sealed cards' hashes before any card is
   sent**; a card list is taken only when it is exactly the commitments (a
   relay can't doctor one, and a household can't swap its own card after a
   peek).
7. **Open and complete.** Exactly one usable card opens per person (a card
   of another person of the round, signed by their household, `rev` 0 — the
   one test, the same for the giver and for everyone checking its
   complaint). Anything else → `fail {cards}` with the giver's **own seed**
   — it shows only the giver's own box and what opens there. Every card that
   opens there and isn't usable carries the signed hash of the household
   that sealed it: that household is named (refused when it is the
   complainer's own). No card, or two usable ones: a **card void** — every
   household shows where it sealed its people's cards (`open {cards {user:
   [box, nonce, target, eph]}}`: the box and nonce against its commitment,
   the box it sealed to, the seal's ephemeral key, which derives from the
   person's seed; anyone opens the sealed card with it and checks it went to
   the box before its person's). The household whose card isn't there is
   named. This opens that dead round only, before it can complete. A partner → `redo` with the seed as proof (chance:
   nobody named). A round completes only with every household's **signed**
   `done`.

**No re-rolls on demand.** A household takes a new round only for: the
table's first draw (or a fresh one after a draw gave up — an `end` it
checked itself, never after the names were revealed; a fresh draw counts as
a Draw again: its count and its exchange day); the organiser's
visible **Draw again** after a reveal (≤ 10, until the exchange day); fewer
people — before `S` is out, or a whole household that holds the round up
removed by the organiser ("Draw without them"), and only once its own copy
of the table shows them gone; a complaint it verifies itself against the
round as **it** received it (the complainer's seed rebuilds the committed
box and the submitted onion; the cards are the committed ones); or a public
void. **Once `S` is out, a leave never starts a new round** — the round
completes and the leaver hands over. A complaint without proof names its
household to the organiser and changes nothing. ≤ 40 rounds per draw.
**Once the same household broke a second round of a draw, no more rounds:**
the draw waits for the organiser, who sees that household at once
(`waitingFor`) and can draw without it — a visible removal. Every household
counts who broke rounds itself and refuses a round after the same household
broke two. A removal once the cards are out (people may have read theirs)
starts a new draw: it counts as a Draw again (its count, its exchange day).

**An `end` is checked too.** A household takes `end {too_few}` only when
fewer than three of the round's people are seated in its own copy;
`end {rules}` only when the pairs no longer fit the people still seated
there, or at the last round with the proof that broke it (`because`, the same
a next round would carry). It never takes an end after the reveal. An end
its copy doesn't show (yet) is kept for 24 hours, then dropped.

**Couples** (D14): non-overlapping pairs of seats, ≤ ⌊n/3⌋, ≥ 4 people.
Checked by each giver's household; a hit is redrawn with proof.

### Steps

All carry `engine: "draw"`, `engine_suite`, `gen` (1, +1 per Draw again or
fresh start), `round`; sent only to households at v_78:

| Step | From → to | Fields |
|---|---|---|
| `start` | hub → all | `people [{seat, instance, user, name}]`, `identities {instance: hex}`, `settings {name, budget, day, pairs}`, `because {kind: first \| again \| changed \| complaint \| void, complaint?, why?: unusable \| stuck, openings?, k?, batches?, chain?}` — every household checks it on its own copy of the round; nothing it already holds is sent again |
| `key` / `mixers` | each → hub / hub → all | `pub`, `sig` / `keys [{instance, pub, sig}]` |
| `onions` | each → hub | `onions`, `commits [[user, c]]`, `r_commit`, `sig` |
| `mix` / `mixed` | hub ↔ mixer k | `k`, `batches` or `list` + `prev {instance, in, sig}` / `list`, `in`, `sig` |
| `list` | hub → all | `S`, `batches [{instance, hashes, commits, r_commit, sig}]`, `chain [{instance, hashes, sig}]` (every output entry's hash) |
| `reveal` / `reveals` | each → hub / hub → all | `r` / `reveals {instance: r}` |
| `open` | each → hub | `opening {seeds {user: seed}, stuck?, sig}` (before any card; `stuck: k` from the mixer whose input doesn't peel) or `opening {cards {user: [box, nonce, target, eph]}, sig}` (a card void) |
| `void` | hub → all | `why: stuck, k, claim, batches, chain` (the chain so far: no household holds it yet) / `why: cards, complaint` |
| `commit` / `commits` | each → hub / hub → all | `hashes`, `sig` / `commits {instance: {hashes, sig}}` |
| `cards` / `cards_all` | each → hub / hub → all | `cards` (1 072 bytes each) |
| `fail` / `redo` | each → hub | `complaint {kind: missing \| cards \| partner, instance, user, seed, sig}` |
| `done` / `complete` / `end` | each → hub / hub → all | `count`, `sig` / `dones {instance: {count, sig}}` / `reason: rules \| too_few`, `because?`, `people?` (a give-up at the last round carries its proof) |
| `handover` / `wish` | each → hub → all (`relayed: true`) | `sealed` |

**Receivers enforce:** the suite; `gen` ≤ 11, `round` ≤ 40; 3 ≤ n ≤ 30,
unique seats and people; exact sizes (box 32 B, onion `32 + 48·(h − k)`,
sealed card 1 072 B, hand-over 1 200 B, digests 32 B, signatures 64 B); lists
of exactly n distinct entries; one draw per table; hub-to-participant steps
only from the hub, participant-to-hub steps only from a household with
people in the round; nothing from a household below v_78; nothing for an app
that, as installed here, doesn't declare `draw`.

### After the reveal, leaving, offline homes

- **Leave after the reveal:** the leaver's household seals the leaver's
  **box keys** to the leaver's giver (`handover`): their own, then every key
  handed to them before (a chain of leavers, ≤ 29 keys). The giver checks the
  first is exactly the box it gives to and each next one the box after it in
  the circle, opens the new card sealed to the last one from the round's
  committed card list it holds — byte for byte, nothing else — and takes it
  only once its own copy of the table shows the leaver gone, and every
  person the chain skips (the card sealed to each key but the last names
  them) gone too. So X → Y → Z → W
  with Z, then Y leaving gives X to W (never to someone who left). Hand-overs
  are kept and retried with every key a household holds (one taken may open
  the next), for 24 hours. The others keep their names; Draw again needs
  three people seated.
- **Leave during a round:** before `S`, a fresh round without them; after
  `S`, the round goes on and the leaver hands over when it completes.
- **Wish list after the reveal:** sealed to the same giver box (`wish`, rev
  + 1, ≤ 5); a giver who took a hand-over holds the leaver's box and opens
  the updates sealed to it.
- **A household that doesn't answer:** after an hour the organiser's status
  names the seats it waits for (`waiting_for`); removing them ("Draw without
  them") starts a fresh round. Nobody is removed automatically.
- Rows never name anyone a person gives or gave to (notification rows are
  stored in plain text): "The person you were giving to left the gift draw —
  see who you're giving to now".

### Cost (once per round)

`n` people in `h` households; onion `32 + 48h` B; sealed card 1 072 B;
base64 +33 %; envelope ≈ 400 B. Envelopes per round ≈ 15(h − 1) (the
reveal and the card commitments add 4(h − 1) small ones).

| | n = 12 in 5 homes | n = 30 in 30 homes (worst) |
|---|---|---|
| start + keys + mixer list | 12 × ~1 KB | 87 × ~1.5 KB ≈ 130 KB |
| onions to the hub | 4 × ~1 KB | 29 × ~2.4 KB ≈ 70 KB |
| the mix | 8 × ~3 KB ≈ 25 KB | 58 × ~30 KB ≈ 1.7 MB |
| `S` + chain to everyone | 4 × ~4 KB | 29 × ~45 KB ≈ 1.3 MB |
| reveal + card commitments (in and out) | 16 × ~0.6 KB | 116 × ~1 KB ≈ 120 KB |
| cards in + all cards out | ≈ 80 KB | 29 × 2 KB + 29 × 44 KB ≈ 1.3 MB |
| done + complete | ~5 KB | 29 × 0.5 KB + 29 × 3 KB ≈ 100 KB |
| **one round** | **≈ 60 envelopes, ≈ 165 KB** | **≈ 435 envelopes, ≈ 4.7 MB** |

Worst realistic draw: 30 homes, ~3 rounds with couples ≈ 14 MB once a year,
≈ 470 KB per household. A redo's `start` carries its proof only — the
complaint (< 1 KB), or the openings (≤ 6 KB) plus, for a stuck mix, the
batches and the chain so far (≤ 45 KB); every household checks it on its
own copy. The largest draw messages — the `list` (≈ 60 KB), `cards_all`
(≈ 43 KB), a stuck void's `start` — stay under 60 % of the GFS relay's
envelope cap (191 KiB) sealed (a test pins the worst case). After the reveal: a
hand-over or wish update is 1 envelope in + h out (≈ 1.7 / 2 KB), ≤ 12 per
person. CPU: a mixer peels n onions; a household tries its boxes on n cards
(≤ 900 X25519 operations); a void rebuilds every onion (n × h seals,
≤ 900) — in a worker thread.

### The app's side (bridge)

`engine.draw.configure({id, settings})` — a **patch**: only the fields
given change (`name`, `budget`, `day`, `pairs`); `null` clears one.
`engine.draw.setWishes({id, text})`, `engine.draw.start({id, again?})`,
`engine.draw.status({id})` →
`{id, state: gathering | drawing | ready | failed, reason?, round, again,
of, done?, waitingFor?, blamed?, settings, wish, hasResult, isHost}`
(`blamed`: households whose misbehaviour this household proved itself —
shown to every person, not only the organiser),
`engine.draw.reveal({id})` → `null` (opens Social Home's sheet). Events:
`{type: 'engine:event', engine: 'draw', event: {type: 'status', id, status}}`.
No call returns a result; the app's sandboxed frame can't reach the API.

### Residual risks

- **One-household draws:** that household's server holds every box key; its
  admin could rebuild the draw from the database. Stated in the app.
- **Two households:** each knows its own boxes, so the other's are the rest;
  when the other household has one person, both households can work out the
  whole circle — "recipients never learn their giver" doesn't hold against
  the household servers there (their people learn nothing from Social
  Home). Stated in the app.
- **A malicious (not just curious) hub** in a picked group of households
  that aren't paired with each other can impersonate a participant household
  with a key of its own (a self-certifying id it controls) and run split
  rounds; it can also leave a seated person out or stall. Space tables and
  paired households don't have the impersonation.
- **Forced redraws cost a name:** a household (or a mixer working with
  the hub) can still break a round on purpose — drop an entry, seal a card
  wrong, lie about a stuck input — but every such round names it to every
  household, and a fresh round (fresh keys) follows. The last household to
  reveal `r` (the hub sees every `r` as it relays) can, with a mixer that
  dropped its entry, choose to complain after seeing the circle: that names
  the mixer to everyone.
- **A household's server works out some pairs touching its own people**
  (see "What holds"; the chance table there), and colluding households pool
  what they know. Accepted (owner, 2026-11-01); the app doesn't say so.
- **Card complaints come after the cards opened:** a complaint redraws a
  round people have read; its culprit is named (from the card that opens,
  or the card void), and that dead round's circle and cards are shown to
  every household — it never completes.
- **Removals hand over pairs:** a hand-over shows the giver the card sealed
  to the leaver's box — the leaver's own recipient; a chain of leavers, or a
  giver colluding with the leavers' household, walks further.
- **Couples on small tables:** a box names nobody, so the circle can't steer
  around a pair; four people with a pair need 3 rounds on average, and give
  up after 40 rounds once in ~11 million draws.
- **Stalls:** a household can stall (refuse to mix, withhold its `r`, its
  commitment or its cards); the organiser sees it after an hour and can draw
  without it — a visible removal, which is the only way a household's
  silence leads to a new round. The organiser can also re-roll by removing
  someone (visible to all) or by Draw again (visible, counted).
- **A household's own people:** it controls which of its people's cards goes
  where (it holds their keys anyway).
- **Couples after a hand-over:** a hand-over can give a giver their partner;
  Draw again is the remedy (L3, documented).
- **Display names** on the round's people list are labels only (not bound to
  the context); the card's name is signed by the person's household (L2).
- v1 runs Draw names / Draw again from the host household only
  (`APP_DRAW_HOST_ELSEWHERE` after a host hand-over to another household's
  person). One `late_join` flag per table (follow-up: "late join until the
  draw starts").

### Implementation

`socialhome/app_engines/` (registry, domain, `draw/`),
`socialhome/routes/app_engines.py`,
`socialhome/repositories/app_engine_state_repo.py`, migration
`0138_app_engine_state.sql`; `AppGroupService` routes the `engine` verb and
table changes to the registry. Tests: `tests/app_engines/`,
`tests/protocol/test_app_engine_draw.py` (security — including every attack
of the first independent review), `tests/routes/test_app_engines.py`.

## Spec references

Group-play design §5, §6 D13/D14; the first independent review
(`draw-review-1`, 2026-11-01); [`capabilities.md`](./capabilities.md) v_78;
[`../crypto.md`](../crypto.md) (gift draw).
