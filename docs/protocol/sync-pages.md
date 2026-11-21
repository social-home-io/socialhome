# Sync pages (signed state pages)

**Status: design (Phase 2 / Phase 3 of the sync-pages plan). Nothing here
ships yet.** Builds on [sync.md](./sync.md), the §25.6 space sync.

The space authority signs, now and then, a small **page**: a commitment to
the space's current content, split into 256 **buckets**. A member keeps
only the newest page it verified. With it, a member can tell whether it is
behind even while the host is offline, and it can take **anyone's** rows
from **any** member household, because each row is checked against the
signed page — not against who delivered it.

## Recommendation

1. **Commit to state, not to a log (option B).** A page is
   `{space, epoch, signer, seq, issued_at, cutoff, rows, root}` signed with
   the space authority key; `root` = SHA-256 over 256 bucket hashes; a
   bucket hash = SHA-256 over the sorted leaf hashes of the rows in it;
   a leaf = SHA-256 of the row's canonical **commit form**.
2. **Store almost nothing.** Host: the newest page + its bucket vector,
   ~9 KB per space, no per-row data, no history. Member: the newest
   verified page, ~0.7 KB per space. Hashes are recomputed on demand
   (≈ 3 µs per record of hashing; a 50 000-row space in well under a
   second of hashing, a few seconds with the database reads on a Pi).
3. **No new message in Phase 2.** The page rides the sentinel of the
   periodic BEGIN that already runs every 30 min, sealed under the space
   content key. The host seals lazily: when a BEGIN arrives and the space
   changed since the stored page, at most once per 15 min. No timer, no
   push, no fan-out.
4. **Phase 3 reuses the §25.6 session**: `SPACE_SYNC_BEGIN` with
   `sync_mode: "buckets"` to any member household; it streams the rows of
   the buckets the requester lacks plus their bucket listings; the
   receiver accepts a row only if its leaf is in a listing that hashes to
   the signed page. The same mode replaces the host's daily full stream
   (anti-entropy) with a bucket diff.
5. One new suite tag (`PAGE_SUITE_SHA256_B256`), one new authority event
   type (`space_sync_page`, existing `authority_sig_suite`), one version
   bump (`MIN_FOR_SYNC_PAGES`), one small migration (member: the stored
   page; host: its page + bucket vector).

## Scope

- **HFS**: every household of a space member. The **signer** is a holder
  of the space authority seed acting as host (the owner, or a delegated
  admin while delegation is on); every member household verifies, stores
  the newest page and, from Phase 3, delivers and checks rows.
- **GFS**: uninvolved. Pages travel only inside the §25.6 sync session
  (paired, mesh `SPACE_ROUTED`, or a GFS mailbox seat's household path —
  sealed end to end, content-blind to any relay). Followers get no pages.
- **Not covered**: media bytes (already content-addressed,
  `SPACE_MEDIA_WANTED`), space config (`SPACE_CONFIG_CATCH_UP`), the
  moderation queue, the resources listed as *host-only* below.

## Event types

No new `FederationEventType`.

- `SPACE_SYNC_CHUNK` sentinel (`__complete__`): new optional field
  `page_sealed` (Phase 2).
- `SPACE_SYNC_BEGIN`: new optional `page` (the requester's newest page
  header, Phase 2) and `sync_mode: "buckets"` with `buckets` (Phase 3).
- New sync resources inside the existing chunks (Phase 3): `__buckets__`
  (the page's bucket vector) and `__listing__` (one bucket's leaves).

## 1. Log or state? (decision)

Numbers for a **big** space — 50 000 rows in the commitment, 2 000
changes a day, 200 member households — and a **small** one (2 000 rows,
50 changes a day, 5 households). Measured with Python's `json` (the
`canonical_json` encoder) and `hashlib` on synthetic 577-byte records, and
a real SQLite file (`WITHOUT ROWID`, after `VACUUM`).

| | A. change log + Merkle pages | **B. state commitment, 256 buckets** | C. B + per-row leaf cache |
|---|---|---|---|
| Host storage, big | 114 B × 2 000 × 30 days ≈ **6.8 MB** (grows with activity, cliff at 30 days) | page + vector ≈ **9 KB** | 56 B × 50 000 ≈ **2.8 MB** |
| Host storage, small | ≈ 170 KB | ≈ 9 KB | ≈ 110 KB |
| Member storage | header + position | header ≈ **0.7 KB** | header ≈ 0.7 KB |
| Host CPU per seal | O(changes) | O(rows): read + export + hash the space (~0.2 s hashing, est. 2–4 s with DB reads on a Pi, 50 k rows) | O(changes) |
| Seals per day, worst | 1 per 256 changes or 5 min | ≤ 96 (lazy, ≥ 15 min apart, only when asked) | as B |
| Member offline > 30 days | full stream from the host | **nothing special** — a state commitment has no history to expire | as B |
| Two signers (owner + delegated admin, a restored backup) | two chains of one log fork; the owner must merge them | each page stands alone: a signed snapshot of that signer's state | as B |
| Catch-up proof | entry lists of every missing page (~20 KiB / page) + rows | listings of the differing buckets only (~6 KiB raw / bucket at 50 k rows) + rows | as B |
| Replaces the daily full anti-entropy stream | no (a log says nothing about rows older than the window) | **yes** — compare roots, then buckets | yes |

**Why B.** Catch-up only needs the **latest version of each row**; a log
stores every superseded version, needs a retention window with a cliff,
and forks the moment two households sign (owner and a delegated admin, or
a host restored from a backup that reuses counter values). A state
commitment answers exactly "is this row, in this version, part of the
space now?", has no history, and two signers' pages simply describe two
states, which the existing merge rules already reconcile.

**Why no leaf cache (C) yet.** Storage is the priority, and the lazy seal
bounds the CPU: ≤ 96 recomputes a day for a space that changes all day,
none for a quiet one. If profiling on a Pi shows seals hurt in very big
spaces, C is a drop-in (one `WITHOUT ROWID` table, 56 B per row, O(changes)
seal) without any wire change — see [Open questions](#open-questions).

### Shape

```
rkey   = SHA-256("sh-sync-rkey:v1:" ‖ resource ‖ 0x00 ‖ row_id)
bucket = rkey[0]                                  (0 … 255)
leaf   = SHA-256("sh-sync-leaf:v1:" ‖ canonical_bytes(commit_form(resource, record)))
bucket_hash[i] = SHA-256("sh-sync-bucket:v1:" ‖ concat(sorted leaves of bucket i))
root   = SHA-256("sh-sync-root:v1:" ‖ concat(bucket_hash[0 … 255]))
```

The bucket is fixed by the row's identity, so a row stays in its bucket
across versions; the leaf changes with every version. An empty bucket
hashes the prefix alone. Domain prefixes keep each hash from being
confused with any other. `commit_form` puts `resource` and the row id
inside the hashed object, so a leaf names its row.

**What is committed** = the set of records a full §25.6 stream from the
signer to a current, paired member household would send, restricted to
the *committed resources* (§2), with the retention cutoff the page
names (`cutoff`). Every household evaluating the commitment uses the
page's `cutoff`, not its own clock, so the sets agree.

## 2. Canonical commit form per resource

`commit_form(resource, record)` = `{"r": resource, "v": projection}` where
`projection` keeps only an **allow-list** of the record's fields per
resource (`COMMIT_FIELDS`, one table in one module), normalised:

- **Files by content hash.** Every file reference (`image_urls`,
  `file_meta.url`, covers, page body pictures) is replaced by the file's
  SHA-256 from the record's `files` map (v_85); local names (`sc-`, `sg-`,
  upload names) never enter a hash. A record whose file has no hash in
  the signer's export → that row is **host-only** (below).
- **Per-requester fields dropped**: `media_refs` (link-joined members
  only), version-gated legacy shapes (`votes` toward < v_94 — the commit
  form always uses the newest shape, since only ≥ `MIN_FOR_SYNC_PAGES`
  households take part).
- **Receipt-local fields dropped**: anything the receiver writes with its
  own clock or ids (the per-resource table below lists them).
- **Lists sorted, sets as sorted lists, no floats** (coordinates are
  already 4-dp; they are hashed as their decimal strings).
- `canonical_bytes` (sorted keys, compact, ASCII) from
  `socialhome/canonical_json.py`.

An allow-list, not a deny-list: a field a later release adds to a record
is **not** hashed until the commit form is revised, so a new field never
silently breaks every page. A revision of the commit form is a new page
suite (`sha256-b256-c2`, §3).

<!-- PER-RESOURCE TABLE: filled from the exporter/receiver audit -->

## 3. Signing, epochs and suites

<!-- TODO -->

## 4. Wire

<!-- TODO -->

## 5. Cost

<!-- TODO -->

## 6. Security

<!-- TODO -->

## 7. Phase plan

<!-- TODO -->

## Open questions

<!-- TODO -->
