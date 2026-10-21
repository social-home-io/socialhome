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
  acl hl_id_in_body path /gfs/highlight_rtc/answer /gfs/highlight_rtc/ice/viewer /gfs/highlight_rtc/ice/author
  http-request set-var(txn.gfs_node) path,field(5,/),field(1,.) if hl_id_in_path
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

Other balancers need the same two things: route by the text before the first
`.` of the id in those five places, and fall back to normal balancing when it
names no node.

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
