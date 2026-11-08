# Running a GFS as a cluster behind a load balancer

## When you need this

A GFS can run as several processes (nodes) on one shared `gfs.db` behind a
load balancer — the production `gfs.social-home.io` runs four Nomad
allocations behind Cloudflare and HAProxy (round-robin). Most GFS state is in
the shared database, and the cluster protocol (§24.10, `[cluster]`) carries
pushes to the node that holds a household's `/gfs/ws` socket
([protocol/discovery.md](../protocol/discovery.md), "Queued frames reach a
household on any cluster node" and the bullets after it).

One thing is not in the database on purpose: a **public-highlight viewing
session**. Its WebRTC signalling state and its relay byte pipe live in the
memory of the node that took the viewer's offer (or relay GET) — nothing is
shared between nodes and no highlight byte crosses nodes. The load balancer
must therefore send every later request of that session to the same node.
Without it, three viewers in four on a four-node cluster never see the
highlight.

## How a session names its node

Each node has a **route tag**: `[cluster] route_tag` in the config file, or
`GFS_CLUSTER_ROUTE_TAG` in the environment (an empty value counts as unset).
It is 1–64 characters of `A-Z a-z 0-9 -`, unique per node, and means nothing
outside your deployment — a Nomad allocation id is a good choice. An unusable
value is a startup error; a cluster node without one logs a WARNING at
startup.

Every session id and relay id the node mints is `<route_tag>.<32 hex>`. The
viewer and the author's household treat the id as opaque, exactly as before,
so neither needs an update — the pinned viewer and older households included.
The balancer reads the tag back from:

| Request | Where the id is |
|---|---|
| `GET /gfs/highlight_rtc/session/<id>` | last path segment |
| `POST /gfs/highlight_rtc/relay-stream/<id>` | last path segment |
| `POST /gfs/highlight_rtc/answer` | JSON body `session_id` |
| `POST /gfs/highlight_rtc/ice/viewer` | JSON body `session_id` |
| `POST /gfs/highlight_rtc/ice/author` | JSON body `session_id` |
| `PUT /gfs/media/serve/<ticket>` | last path segment (v_83, space media on demand) |
| `POST /gfs/media/serve/<ticket>/decline` | fourth path segment (v_83) |

The space media ticket works the same way: a reader's
`POST /gfs/media/{blob}/wanted` — which may land on **any** node — opens a
session in that node's memory, and the node asks a holder household (over
the cluster, wherever its socket is) to send the blob to
`/gfs/media/serve/<route_tag>.<32 hex>`. That upload must reach the node
holding the waiting reader.

Everything else — the offer and the relay GET that create a session, and
every other GFS route — keeps the normal balancing. The author's household
may hold its socket on any node: the offer, the viewer's ICE and the relay
request reach it through the cluster.

A request whose tag names no live node goes to the normal pool. The node it
reaches does not hold the session and answers `404`; the viewer then falls
back to the relay at once (a node restart or redeploy loses its sessions the
same way — they live for two minutes at most). A node that gets a request
for another node's session logs, at most once a minute:

```
gfs: N public-highlight request(s) for a session of another node reached this node (route tag '…') — the load balancer does not route by [cluster] route_tag …
```

That line means the balancer rules below are missing or wrong.

## HAProxy

HAProxy 2.4 or newer (`wait-for-body`, `json_query`). In the frontend, before
`default_backend`:

```haproxy
  acl hl_id_in_path path_reg ^/gfs/highlight_rtc/(session|relay-stream)/[^/]+$
  acl sm_ticket_in_path path_reg ^/gfs/media/serve/[^/]+(/decline)?$
  acl hl_id_in_body path /gfs/highlight_rtc/answer /gfs/highlight_rtc/ice/viewer /gfs/highlight_rtc/ice/author
  http-request set-var(txn.gfs_node) path,field(5,/),field(1,.) if hl_id_in_path
  http-request set-var(txn.gfs_node) path,field(5,/),field(1,.) if sm_ticket_in_path
  http-request wait-for-body time 2s if hl_id_in_body METH_POST
  http-request set-var(txn.gfs_node) req.body,json_query('$.session_id'),field(1,.) if hl_id_in_body METH_POST
  use_backend gfs_node_%[var(txn.gfs_node)] if { var(txn.gfs_node) -m found }
  default_backend gfs_app
```

and one backend per node, named `gfs_node_<route_tag>`, tracking the health
of the same server in the main pool:

```haproxy
backend gfs_node_<route_tag>
  server gfs-0 127.0.0.1:<port> track gfs_app/gfs-0
```

`use_backend` with a dynamic name falls through to `default_backend` when no
backend of that name exists, so an id without a tag (an older node during a
rolling upgrade) or with a stale one is balanced normally. `wait-for-body`
only buffers the three small signalling POSTs; their bodies are an SDP or one
ICE candidate. The gfs-deployment repository renders both parts from the
Nomad service registry (`route_tag = NOMAD_ALLOC_ID`, one backend per
`nomadService "gfs"` entry, keyed by its `AllocID`).

The space media serve upload is a body of up to 8 MiB: it is never buffered
by these rules (the ticket is in the path), only routed.

Other balancers need the same two things: route by the text before the first
`.` of the id in those seven places, and fall back to normal balancing when it
names no node. Without the serve rule, a holder's upload lands on another
node, which answers `404 unknown_ticket`; the reader then gets a clean miss
after its wait (one in N on an N-node cluster still works).

## Space media cache on a shared data dir

The space media cache (`[space_media]`, [protocol/discovery.md](../protocol/discovery.md#space-media-cache-on-a-gfs-capability-space_media))
keeps encrypted blobs as files under `<data_dir>/space_media/` (not under the public `media/` branding dir) and their
rows in the shared `gfs.db`, so every node must mount the **same** data dir:
whichever node took an upload, any node serves the fetch. Size
`max_bytes` (default 5 GiB) to the disk that volume has — it is a cache, so
the owner holding the most bytes — a household across all its spaces, or
for channel blobs the uploader's network prefix (/48, IPv4 /24) — loses its own least recently
fetched blobs first when it is full — never one global order and never per
scope, so one household can't flush the others with many spaces (and a space or channel past `space_max_bytes` evicts
its own least recently fetched). One client address uploads at most
`ip_bytes_per_hour` (1 GiB) an hour across every scope, counted cluster-wide
in `gfs.db`. A private space's channel takes media while a member household
holds a seat in it. And
nothing lives past `ttl_seconds` (3 days). Behind Cloudflare a fetch (`GET /gfs/media/{blob}`) is
`Cache-Control: public, max-age=3600, immutable`, so the edge absorbs
repeated reads; a miss is `no-store`. Set `max_bytes = 0` (or
`GFS_SPACE_MEDIA_MAX_BYTES=0`) to turn the cache off — uploads then answer
`503 space_media_disabled` and it is no longer advertised in `/gfs/info`.
Concurrent uploads on several nodes may briefly hold the total above
`max_bytes` by the uploads other nodes have in flight — at most
(nodes − 1) × 8 MiB, each node making room one upload at a time; leave that
much disk headroom. The upload rate holds exactly (reserved in the shared
database). One node at a time (a lease row in `gfs.db`) runs the hourly
orphan sweep: it walks the next 100 000 entries of the shared directory —
resuming where the last run stopped — and removes files left without a row
(a crash mid-upload) and stale `.part` files older than 10 minutes. Every
node's sweep also removes stale `.part` files under `media/moments/` (a page
of that directory per run, the same code). The admin overview (`GET /admin/api/overview`,
`space_media`) shows the blobs and bytes held and this node's evictions and
quota refusals — in the admin portal, the **Media cache** card on Overview:
used of `max_bytes` with a bar, the number of pictures and files, the TTL,
and this node's evictions and refusals since it started (or "Turned off"
while `max_bytes = 0`). Each node counts its own; reload against another
node to see its counters.

### Missed pictures on request (v_83)

When a reader views a picture the cache no longer holds, it asks
(`POST /gfs/media/{blob}/wanted`) and the GFS asks one household that holds
the file to send it again — streamed to the waiting reader(s) and not kept,
or re-uploaded into the cache when the content is fresh or asked for
repeatedly ([protocol/discovery.md](../protocol/discovery.md#space-media-on-demand-v_83)).
The `wanted` request can land on any node; the holder's upload is routed
to the node that took it by the ticket's route tag (the HAProxy rule
above). What a blob needs across nodes — when it was stored, how often it
was asked, the one re-upload claim — is in `gfs.db`
(`gfs_space_media_demand`), so a blob is re-uploaded once per
`coalesce_seconds` cluster-wide; streams are per node (at most one per node
per window), held in memory only. All keys go in `[space_media]`, each
with an env override `GFS_SPACE_MEDIA_<KEY>` (upper case):

| Key | Default | |
|---|---|---|
| `on_demand` | `true` | Turn the whole thing off (`false`): every `wanted` is a `404` and `/gfs/info` stops advertising it. |
| `fresh_seconds` | `604800` (7 days) | Content stored here at most this long ago goes back into the cache. |
| `cache_after_requests` | `3` | Requests within `request_window_seconds` that earn a re-upload, however full the cache. |
| `request_window_seconds` | `3600` | The window for that count. |
| `coalesce_seconds` | `60` | One re-upload per blob per window; a delivered stream stays joinable this long; one the holder could not serve (not an unusable hint) is not asked again on that node this long. |
| `full_percent` | `95` | Above this share of `max_bytes`, only repeatedly asked content is cached. |
| `stream_wait_seconds` | `25` | The longest a reader waits for a holder (1–60). |
| `max_streams` | `16` | Holder sessions per node (each ≤ 8 MiB in memory). |
| `scope_max_streams` | `4` | Holder sessions per space or channel per node. |
| `scope_stream_bytes_per_hour` | `536870912` (512 MiB) | Bytes one space or channel may stream an hour, cluster-wide; one reader network prefix may use at most 1/8 of it (64 MiB by default). |
| `prefix_wanted_per_hour` | `240` | Holder requests one reader network prefix (/48, IPv4 /24) may trigger an hour, cluster-wide. |

Memory: at most `max_streams` × 8 MiB (128 MiB by default) per node for
sessions in flight — a session a slow reader is still being written from
counts too, and a reader that stops reading is cut off; a holder must
deliver within 120 s. The **Media cache** card on Overview adds this node's
holder requests, streams served (and bytes), re-uploads, declines and
requests nobody could serve ("Not found"), or says the feature is off.

## Envelope relay media frames (v_87)

`POST /gfs/envelope` also takes binary media frames (`\x00SHR`, up to
**528 KiB**, see [gfs-relay.md](../protocol/gfs-relay.md#binary-media-frames-v_87)).
Their caps live in `[envelope_relay]`, each with a `GFS_ENVELOPE_RELAY_<KEY>`
env override:

```toml
[envelope_relay]
media = true                                # carry them; false drops the envelope_media capability
media_ip_bytes_per_minute = 67108864        # 64 MiB per client address, per node (429 past it)
media_queue_bytes_per_recipient = 16777216  # 16 MiB queued for one offline household (tail-drop)
media_queue_ttl_seconds = 3600              # 1 h
media_queue_max_total_bytes = 1073741824    # 1 GiB queued for everyone (tail-drop)
media_max_inflight_bytes = 67108864         # 64 MiB held in memory between answer and push
```

The queue is the shared `gfs_media_queue` table (GFS migration 0028), so a
frame posted to one node for a household connected to another is drained by
that node on the `NODE_DRAIN_HINT` (ids only — the bytes never cross the
cluster link, so the 768 KiB `relays` cap does not apply).

**Per-node budgets multiply.** The byte budget and the in-flight bound are
counted in each node's memory. Behind the round-robin balancer one client
address spreads over every node, so with 4 allocs it gets about
**4 × 64 MiB ≈ 256 MiB of media a minute**, and the cluster can hold about
**4 × 64 MiB ≈ 256 MiB of media in flight** (each node its own 64 MiB). The
queue caps (16 MiB per recipient, 1 GiB total) are in the shared database
and hold cluster-wide. Size `media_ip_bytes_per_minute` and
`media_max_inflight_bytes` per node with that in mind.

**Residual (documented, accepted):** the 1 GiB server-wide media queue is
shared by every offline recipient, so an anonymous sender that knows many
instance ids can fill it (tail-drop) and further media for OFFLINE
households is dropped until rows expire (1 h, swept on every insert). Live
recipients are unaffected — a frame for a connected household is pushed,
never queued — and so are the control envelopes (their own table and caps).
No per-sender share: the queue would have to store a sender address next to
the recipient, which the GFS must never keep. A dropped frame heals on the
reader's next view (see [gfs-relay.md](../protocol/gfs-relay.md#binary-media-frames-v_87)).

**Proxy body limits.** Every front end must let a 528 KiB request body
through on `/gfs/envelope`. HAProxy has no request body limit by default and
does not buffer it unless a rule asks (`wait-for-body` is only on the three
signalling POSTs above), so the stock config is fine; nginx defaults to
`client_max_body_size 1m` (fine), and Cloudflare's 100 MB default upload limit
(Free / Pro) is far above it. A front end set below 528 KiB answers 413, and a
household then cannot send media frames through that server.

## Anonymous mailboxes (GFS redesign PR 1)

A GFS with mailboxes on (the default) advertises `envelope_mailbox` in its
signed `/gfs/info` and serves two anonymous routes (see
[gfs-relay.md](../protocol/gfs-relay.md#anonymous-per-space-mailboxes-gfs-redesign-pr-1)):
`POST /gfs/mailbox/register` and the claim socket `GET /gfs/mailbox/ws`.
The keys live in `[envelope_relay]`, each with a `GFS_ENVELOPE_RELAY_<KEY>`
env override:

```toml
[envelope_relay]
mailboxes = true            # serve them; false drops the routes (404) and the capability
mailbox_max_per_ip = 200            # NEW mailboxes per address per UTC day (/64; ×16 per /56, ×256 per /48)
mailbox_max_new_per_day = 20000     # NEW mailboxes server-wide per UTC day (shared table)
mailbox_max_sockets_per_ip = 512    # open claim sockets per address (/64; ×16 /56, ×256 /48)
mailbox_idle_days = 30              # last claim (or registration) this many days ago → dropped
mailbox_queue_max_per_recipient = 200         # queued envelopes for one offline mailbox
mailbox_queue_bytes_per_recipient = 4194304   # 4 MiB
mailbox_queue_max_total_bytes = 268435456     # 256 MiB for ALL mailboxes, then tail-drop …
mailbox_queue_ceiling_bytes = 1073741824      # … except a mailbox under its reserve (< 20 envelopes, < 64 KiB), up to 1 GiB
mailbox_media_queue_bytes_per_recipient = 8388608   # 8 MiB of media for one mailbox
mailbox_media_queue_max_total_bytes = 268435456     # 256 MiB of media for ALL mailboxes (tail-drop)
```

Mailbox frames live in a pool of their own: they never count against, and
never evict, households' queued envelopes (1 GiB total, tail-drop with the
same reserve up to 2 GiB) or media
(`media_queue_max_total_bytes`). Worst-case queue disk ≈ 2.75 GiB whatever
the number of minted ids (see
[gfs-relay.md](../protocol/gfs-relay.md#anonymous-per-space-mailboxes-gfs-redesign-pr-1)).

The per-address caps and the 30 / min register limit (120 / min claim
sockets) count in each node's memory — the caps in salted count-min sketches
that keep no address at all — and are divided over the live nodes like the
other anonymous limits (below). The address is never written to the
database or logged next to a mailbox id.

**Per-alloc public ids.** A registration and a claim are bound to the GFS id
the household pinned. A node accepts its own id, its `instance_id_aliases`
and the ids its shared-key siblings report in their HELLO / heartbeat, so
a cluster whose allocs still serve `gfs-N` ids works (register via one
alloc, claim on another). Every node also writes its own id and its
siblings' ids to the shared `gfs_known_gfs_ids` table and accepts any id
seen there in the last 90 days, so a household pinned to an alloc that
has since gone keeps claiming. **Retiring a node or changing an id still
needs `instance_id_aliases`** on the remaining nodes: the table only knows
ids that were live while this release ran, and `/gfs/info` / the rest of
the protocol rely on the aliases — the gfs-cluster demo checks it (step 3k). A
freshly started alloc learns a sibling's id from that sibling's first
HELLO / heartbeat (≤ 30 s); until then a claim bound to it is refused like a
wrong key and the household retries. Still
move every alloc to one `instance_id` (the cluster logs an ERROR until
then).

**No sticky routing needed.** The registrations (`gfs_mailboxes`) and the
queued frames (`gfs_envelope_queue` / `gfs_media_queue`, keyed by the
mailbox id) are in the shared database, and a frame posted to one node for a
mailbox whose claim socket another node holds is drained by that node on the
`NODE_DRAIN_HINT` (it carries mailbox ids like instance ids). So both routes
can be round-robined like `/gfs/ws`.

**What `gfs-deployment` needs** (no change in this repo; open it there):

- **Routes.** Send `/gfs/mailbox/register` (POST, ≤ 2 KiB body) and
  `/gfs/mailbox/ws` (GET + `Upgrade: websocket`) to the GFS backend like
  `/gfs/envelope` and `/gfs/ws` — if the frontend lists paths explicitly,
  add both. The WebSocket route needs the same `timeout tunnel` as `/gfs/ws`
  (aiohttp pings every 30 s, inside Cloudflare's 100 s idle timeout, so an
  idle claim socket stays up through both).
- **Connections and file descriptors.** Every household reached through the
  GFS holds one extra socket **per space** it is reached for (one mailbox per
  socket, by design), on top of its `/gfs/ws`. Size HAProxy `maxconn`
  (frontend and per-server) and each Nomad alloc's `nofile` ulimit for
  `households × (1 + spaces reached via this GFS)`; e.g. 2 000 households in
  5 such spaces each is ~12 000 sockets (~3 000 per alloc on 4 allocs, plus
  the same again on HAProxy, which holds both sides). Raise
  `ulimit -n` / Nomad `config { ulimit { nofile = "65536:65536" } }` and
  `maxconn` accordingly.
- **Cloudflare.** WebSockets on (already needed for `/gfs/ws`); no rule
  change.

**Rolling upgrade.** A node older than this release has no mailbox routes
(`404`) and drops an envelope addressed to a mailbox id as "unknown
recipient" (the same uniform `202`). Households use mailboxes only from
redesign PR 2 on, and only where `/gfs/info` signs `envelope_mailbox`, so
upgrade every node before rolling out a household release that uses them.

## Rate limits across nodes

The anonymous per-IP limits on the public pages, the highlight offer /
relay entry points, `/gfs/highlights/ice-servers` and the space media
`wanted` requests (60 / min) and the mailbox register / claim limits count in each node's
memory, so each node allows its share — the limit divided by the live
nodes, rounded up (`public.node_share`). Behind round-robin, a client gets
about one budget across the cluster. When a node goes away its peers mark
it offline after three missed heartbeats and the share grows back.
Before the first heartbeat round after a start, a node counts only itself.

## Rollout

Both orders are safe; this one never routes a tagged id nowhere:

1. Deploy the GFS version with route tags, setting `route_tag` on every node.
   Until the balancer routes by tag, sessions behave as before.
2. Deploy the balancer rules. From then on every session sticks to its node.

Doing the balancer first is also fine: ids without a tag match no per-node
backend and are balanced as before.

## Check it

`python .claude/skills/federation-demo/harness.py gfs-cluster` runs three
nodes on one database behind a balancer and plays a highlight link through
it (step 3g) — with the real HAProxy rules above when `GFS_DEMO_HAPROXY`
names an `haproxy` binary, otherwise with an in-process stand-in that applies
the same rules.

On a live cluster, open a public highlight link a few times: every attempt
should play, and no node should log the misroute WARNING above.

## Cost

Per viewing session, at most: the offer (an SDP, ~2–6 KiB), up to 32 viewer
ICE candidates (~300 bytes each) and one relay request (~400 bytes) forwarded
once to each other node, riding the coalesced `NODE_DRAIN_HINT` frames (at
most one per peer every 1.5 s, however many sessions). Nothing is forwarded
when the author's socket is on the node that took the request. No highlight
byte crosses nodes: the relay upload goes to the node holding the viewer's
GET.
