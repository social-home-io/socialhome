# Momentum discovery (design, 2026-11-24)

Status: **design — P0**. Owner request 2026-11-24, scoped down the same day:
Discover (`/momentum/discover`, see
[Public Momentum › In-app Discover](momentum_public.md#public-directory--profile-sync))
gets

1. **Ranking of the GFS author directory** — "Most followed" and "Active
   recently", from metadata the GFS already holds, adding no new data toward
   the GFS.
2. **A Friends section** — the people the Friends feature already knows
   (your own household and your paired households), each with a one-tap
   Follow / Following, as simple as following a GFS author. It uses the
   existing household follow (`user_follows`).

Out of scope (owner, 2026-11-24): "people your paired households follow".
No follow list is shared with anyone; no new data goes anywhere.

## What each part needs and where it lives today

| Need | Lives in | New? |
|---|---|---|
| Friends list (people, household, picture) | `GET /api/friends` (`FriendsService`): own household + socially paired households, blocks and guardian blocks already removed | no |
| Who I follow at home | `GET /api/moments/follows` → `user_follows` (local only, never federated, migration 0001 comment) | no |
| Follow / unfollow a friend | `POST /api/moments/follows`, `DELETE /api/moments/follows/{user_id}` (`UserService.follow`), already used by the person sheet (`client/src/store/follows.ts`) | no |
| GFS directory | `GET /gfs/moments/users` (≤ 200 rows, newest registration first, `gfs_user_registrations`), proxied by `GET /api/gfs/{gfs_id}/moments/users` | no |
| Follower count per author | `gfs_moment_follows` (indexed on `followed_user_id`); **already public**, exact, on `GET /gfs/moments/users/{user_id}` (`follower_count`), the HTML landing `/moments/{user_id}` and the viewer page `/gfs/pages/people/{user_id}` | no |
| Last time an author published | **not stored.** The GFS sees each `POST /gfs/moments/publish` (`author_user_id` and the step day are plaintext routing fields) but keeps only a 25 h in-memory digest cache, not keyed by author. Media rows (`gfs_moment_media`) exist only for moments with media | **yes** — see the decision below |

## 1. Ranking the GFS directory

### Most followed

`GET /gfs/moments/users?sort=followers` returns the 200 active authors with
the most followers, each row carrying `follower_count`. `sort=new` (and no
`sort`) keeps today's order. `q` is unchanged and still never sent by Social
Home (the search box filters in the browser).

*Reveals nothing new.* Each author's exact follower count is already public
on three anonymous GFS pages; the ranked list is the same numbers in one
response, and the order is derivable from them. Counting follower *users*
(the public number), not households, keeps it that way.

*Exact vs. coarse.* Exact counts let a watcher tie a follow to a moment
("+1 at 14:02"). That is true today. Rounding only in the new list would
change nothing while the detail pages stay exact, so the honest choices are
**keep exact everywhere (no change)** or **round everywhere** (`1–9` exact,
then `10+`, `50+`, `100+`, `500+`, `1k+`, `5k+`, `10k+`) — a tightening of
the pages that already exist, sort still by the exact count on the GFS
(the order within a bucket would still leak a little). Proposed: keep
exact; rounding is a separate, optional tightening.

### Active recently — needs an owner decision

The GFS does not keep when an author last published. Showing "active
recently" means it would **keep a new fact at rest** and **show it to
anyone**: today an anonymous visitor can't tell whether a listed author
posts at all (the landing page shows no moments, owner decision
2026-10-20). Options:

* **A. Skip it.** Ship New + Most followed only. Nothing new anywhere.
* **B. Day-coarse, order only.** GFS migration `ADD COLUMN
  last_active_day INTEGER` (NULL default) on `gfs_user_registrations`,
  set from the publish's plaintext step day (the GFS already reads it),
  written at most once per author per day. `sort=active` orders by it; no
  date or label is shown, but the top of the list is "posted lately" for
  anyone to see.
* **C. As B, but only for authors who opt in** ("Show that I'm active in
  the directory", off by default), carried on the existing register call.
  Adds a registration field, a GFS column and a Settings switch.

Proposed: **A** now; **B** if the owner accepts that authors' posting
activity becomes public (day-coarse).

### Wire, version, migration

* SH↔GFS only — no federation event, **no peer protocol bump** (nothing
  rides v_96). The GFS advertises `moment_directory_sort` in its signed
  capabilities (as `moment_counts`); the SH proxy passes `sort` only to a
  GFS proving it, and tells the SPA which sorts work. An older GFS keeps
  "New" only — no broken chip.
* No SH migration. Option B/C adds one GFS migration (one NULL column).

### Cost

* Response: +`follower_count` (~20 B) × ≤ 200 rows ≈ +4 KB per directory
  fetch; one fetch per Discover visit per GFS, as today.
* GFS: one grouped count over `gfs_moment_follows` (index on
  `followed_user_id`). Large GFS, 50 000 authors × 20 followers = 1 M rows:
  ~0.2–0.5 s in SQLite, so each node keeps the ranked top 200 in memory for
  5 minutes (≤ 200 × ~60 B). Worst case 12 recomputes an hour per node,
  whatever the number of visitors.
* Option B: ≤ 1 write per active author per day (50 000 authors → ≤ 50 000
  small UPDATEs a day, spread over the day), no extra message.

## 2. Friends in Discover

Pure SPA. No endpoint, no wire, no migration, no new data: the section reads
`GET /api/friends` and `GET /api/moments/follows` (both local) and toggles
with the same `followUser` / `unfollowUser` the person sheet uses. A
household follow never leaves the household; the paired person's moments
already reach it through pairing, and a follow keeps them longer (the
retention window) and lifts `max_hops` for that author.

* **Who:** everyone `GET /api/friends` returns — own household and paired
  households — except yourself. Blocked and guardian-separated people are
  already removed by `FriendsService`.
* **Order:** not followed first, then by household, then by name. The
  list is never hidden when you follow everyone; Following shows instead.
* **Discover is shown** to anyone with friends or a GFS (today: only with a
  GFS). A protected account (`public_moments` restricted) sees the Friends
  section only — the household follow is already allowed for it on the
  person sheet.
* **Search** filters both sections in the browser.
* The GFS list keeps marking paired people "Paired" (no GFS follow for them,
  owner decision 7), so nobody gets two Follow buttons.

## UI sketch

```
Discover
[ Search people… ]

Friends                                   (own + paired households)
 (●) Anna       Müller household       [ Follow ]
 (●) Ben        Müller household       [ Following ]
 (●) Chris      Home                   [ Follow ]
     Show all 14

On example GFS        [ New ] [ Most followed ] ( [ Active recently ] )
 (●) Dana  @dana   1,204 followers      [ Follow ]
 (●) Eli   @eli    Paired
 …
 Following someone here tells the GFS who follows whom.
```

Empty and error states:

* No friends: "Pair with a household to follow its people here." with
  **Open Connections**.
* `GET /api/friends` fails: inline error with Try again (the existing
  `connections_failed` pattern); the GFS section still renders.
* No GFS: Friends section only, plus today's "no GFS" card under it.
* GFS unreachable: today's cached list + age + Try again, unchanged.
* Older GFS: only "New" is offered.

Tap targets ≥ 36 px; chips wrap on a 390 px phone; the Follow button is a
toggle with `aria-pressed` and a per-person label, as in the GFS list. New
strings go to `en.json` and are translated to de / nl / fr / es (glossary:
"Follow" / "Friends" as the existing keys).

## Phases

1. **P1 — Friends in Discover** (SPA only).
2. **P2 — Most followed** (GFS `sort`, capability, SH proxy, chips).
3. **P3 — Active recently** only if option B or C is chosen.
4. Docs (`momentum_public.md`, `docs/api.md`), demo/harness check, final
   review.
