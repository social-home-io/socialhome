# Momentum discovery

Owner request 2026-11-24, scoped down and decided the same day. Discover
(`/momentum/discover`, see
[Public Momentum › In-app Discover](momentum_public.md#public-directory--profile-sync))
gets:

1. **A Friends section** — the people the Friends feature already knows
   (your own household and your paired households), each with a one-tap
   Follow / Following, as simple as following a GFS author. It uses the
   existing household follow (`user_follows`).
2. **Ranking of the GFS author directory** — "Most followed" (the default)
   and "Newest", from metadata the GFS already holds, adding no new data
   toward the GFS.

Out of scope (owner, 2026-11-24):

* "People your paired households follow" — it would need each person's
  follow list shared with paired households. No follow list is shared with
  anyone; no new data goes anywhere.
* "Active recently" — the GFS does not keep when an author last posted, and
  showing it would make an author's posting habit public to anyone. People
  signed up to be listed, not that.

## What each part needs and where it lives

| Need | Lives in | New? |
|---|---|---|
| Friends list (people, household, picture) | `GET /api/friends` (`FriendsService`): own household + socially paired households, blocks and guardian blocks already removed | no |
| Who I follow at home | `GET /api/moments/follows` → `user_follows` (local only, never federated, migration 0001 comment) | no |
| Follow / unfollow a friend | `POST /api/moments/follows`, `DELETE /api/moments/follows/{user_id}` (`UserService.follow`), also used by the person sheet (`client/src/store/follows.ts`) | no |
| GFS directory | `GET /gfs/moments/users` (≤ 200 rows, `gfs_user_registrations`), proxied by `GET /api/gfs/{gfs_id}/moments/users` | a `sort` parameter |
| Follower count per author | `gfs_moment_follows` (indexed on `followed_user_id`); **already public**, exact, on `GET /gfs/moments/users/{user_id}` (`follower_count`), the HTML landing `/moments/{user_id}` and the viewer page `/gfs/pages/people/{user_id}` | now also on each directory row |

## 1. Friends in Discover

Pure SPA (`client/src/features/momentum/DiscoverFriends.tsx`). No endpoint,
no wire field, no migration, no new data: the section reads
`GET /api/friends` and `GET /api/moments/follows` (both local) and toggles
with the same `followUser` / `unfollowUser` the person sheet uses. A
household follow never leaves the household; the friend's moments already
reach it through pairing, and a follow keeps them as long as their author
chose instead of 24 hours. The hint under the heading says so.

* **Who:** everyone `GET /api/friends` returns — your own household and
  your paired households — except yourself. Blocked and guardian-separated
  people are already removed by `FriendsService`.
* **Order:** your household first, then paired households by name, people
  by name. A tap never re-sorts the list, so a row doesn't move under the
  finger.
* **Following:** the heading counts it ("You follow 1 of 9"); the button
  turns into a secondary "Following" with a check, same width either way.
  Unfollow is one tap; its toast says that moments you saved from that
  person are removed (the 0124 triggers drop them with the last follow) and
  offers Undo, which follows again.
* **More than six people** show five plus an in-place "N more"; a search
  shows every match.
* **Each person once:** you and the people of your own and paired
  households leave the GFS list — they are in Friends with the household
  follow (owner decision 7, "one Follow").
* **Discover is always a tab.** A protected account (`public_moments`
  restricted) sees the Friends section only, and no GFS is read.

## 2. Ranking the GFS directory

### Wire

`GET /gfs/moments/users?sort=followers|new` (default `new`, today's
order). Each row now carries `follower_count` (follower users, the number
the per-author pages already show) and the response echoes `sort`. An
unknown `sort` is `422 invalid_sort`. `q` is unchanged and still never sent
by Social Home (the search box filters in the browser); a blank `q` is no
search, and a search is never ranked (`sort=followers` with `q` is
`422 search_not_ranked` — ranking it would recount on every request).

* `sort=followers` — the 200 active authors **followed by the most
  households**, ties newest first. Households, not follower ids: a
  follow's `follower_user_id` is whatever the signed household says, so
  one household could make up any number of followers — it counts once
  toward the order, and follower ids never break a tie. (The label stays
  the public follower count, so an author with many followers in few
  households may sit below one with fewer.) The order is one grouped count
  over `gfs_moment_follows`; each node keeps only the ordered ids for
  `RANKED_DIRECTORY_TTL_S` (5 minutes), recounted by one request at a time
  while the others wait for it — visitors can't make it recount. The rows
  and counts are read live (≤ 200 by primary key, one indexed `IN` count),
  so an author who leaves or is suspended is gone at once; one who just
  signed up joins the order at the next recount (≤ 5 minutes) and shows
  under Newest at once.
* `sort=new` — newest registration first, with the counts of those ≤ 200
  rows (one indexed `IN` count).

The household proxy `GET /api/gfs/{gfs_id}/moments/users?sort=…` passes
`sort` through (anything else is `422`) and treats what comes back as
display data: `sort` only when the GFS echoed exactly the order asked for
(else `null`), a `follower_count` only as a whole number from 0 to 1e9,
and a body that isn't JSON is `502`. The app checks both again; they label
the list and its rows and drive nothing else.

**No capability flag.** An older GFS ignores the parameter and answers in
its old order without `sort` or counts; the app then offers no sort buttons
and shows no counts. Nothing breaks on an older server, so a signed
capability would add a moving part for nothing.

**Reveals nothing new.** Each author's exact follower count is already
public on three anonymous GFS pages; the ranked list is the same numbers in
one response, and its order follows from them. The counts stay exact
(owner, 2026-11-24) and are shown compactly ("1.2K followers", nothing for
0).

### Versions and migrations

SH↔GFS only — no federation event, no peer protocol bump (nothing rides
v_96), no migration on either side.

### Cost

* Response: +`follower_count` (~20 B) × ≤ 200 rows ≈ +4 KB per directory
  fetch; one fetch per Discover visit and sort per GFS, as before.
* GFS, most followed: one grouped count over the follow graph. Large GFS,
  50 000 authors × 20 followers = 1 M rows: ~0.05–0.5 s in SQLite, at most
  once per 5 minutes per node whatever the number of visitors (12 an hour
  per node, worst case), kept as ≤ 200 ids in memory (~10 KB). Each request
  then reads ≤ 200 rows by primary key and one indexed count — the same as
  newest.
* GFS, newest: as before plus one indexed count over ≤ 200 authors.
* Not rate-limited per address (it never was): every answer is bounded by
  the two reads above.

## UI

```
Discover
[ Find people ]                       (filters both sections, here)

Friends                                        You follow 1 of 9
Following keeps their moments for as long as they choose, instead of
24 hours · stays between your households
 (●) Daan       Bakker family                  [ ✓ Following ]
 (●) Eva        Bakker family                  [ Follow ]
 (●) Anna       Müller-Lüdenscheidt Fam…       [ Follow ]
     4 more

People sharing on example GFS      [ Most followed ] [ Newest ]
Following someone here tells example GFS that your household follows them.
 (●) Dana  @dana · 1.2K followers               [ Follow ]
 (●) Eli   @eli · 87 followers                  [ Follow ]
```

States:

* No friends and no GFS: one card, "Pair with a household to follow its
  people here." with **Open Connections**.
* No friends but a GFS: the GFS list leads; Friends is one line under it.
* Friends but no GFS: Friends, then one line about connecting a GFS.
* `GET /api/friends` fails: an inline error with Try again in Friends only;
  the GFS list still shows.
* GFS unreachable: the cached list with its age + Try again, as before;
  the sort buttons are disabled.
* Each section loads on its own.
* An empty Moments inbox offers **Find people to follow** → Discover.

Sort buttons are toggle buttons (`aria-pressed`), not tabs — they reorder
one list. A change is announced politely ("Sorted by most followed").

## Phases

1. **P1 — Friends in Discover** with the UX pass.
2. **P2 — Most followed** (GFS `sort`, household proxy, sort buttons,
   compact counts).
3. Docs, harness check, final review.
