# TURN setup for production WebRTC

## When you need this

SocialHome's federation transport (server-to-server WebRTC), the
sync DataChannel, and the SPA's calls / live highlights all use
WebRTC. WebRTC's standard path is **direct peer-to-peer over UDP**.
On the open internet, that path works for ~70% of household pairs
out of the box (cone NAT, IPv4 reflexive). The other ~30% need a
relay because of:

* **Symmetric NAT** — the household's NAT picks a different port
  for every destination, so the address the STUN server reports
  doesn't match what the peer would have to dial.
* **Strict residential / corporate firewalls** — outbound UDP
  blocked entirely; WebRTC must fall back to TCP-on-443 via TURN.
* **CGNAT** — the ISP NATs you behind a shared address; no inbound
  UDP arrives at your machine ever.

When you see federation peers stuck on transport `https` even after
the 30 s settle window, or the log shows
``juice: Connectivity timer expired`` and the PeerConnection
transitions to ``failed`` without a DataChannel ever opening,
**that's the symptom**. STUN alone can't rescue these cases — only
a relay (TURN) can.

The federation transport will log a one-shot warning at startup if
no TURN is configured:

```
WARNING:socialhome.webrtc_ice:WebRTC: no TURN server configured.
STUN alone can't traverse symmetric NAT or strict firewalls; if
federation peers fail to establish RTC (transport stays on 'https'
indefinitely), deploy a TURN server (coturn is easy; see
docs/operations/turn.md) and set webrtc_turn_url +
webrtc_turn_secret (or webrtc_turn_user/cred) in socialhome.toml.
```

That's your signal to set this up.

## Recipe: coturn + HMAC time-limited credentials

[coturn](https://github.com/coturn/coturn) is the reference TURN
server. ~3 MB binary, runs anywhere, supports the TURN-REST
HMAC-credential scheme SocialHome's `webrtc_turn_secret` mode
expects.

Pick a public hostname + a TLS cert (Let's Encrypt is fine). Then:

```
# /etc/turnserver.conf
listening-port=3478
tls-listening-port=5349
external-ip=<your.public.ip>
realm=<your.public.hostname>
fingerprint
lt-cred-mech
use-auth-secret
static-auth-secret=<long-random-string>
# TLS
cert=/etc/letsencrypt/live/<host>/fullchain.pem
pkey=/etc/letsencrypt/live/<host>/privkey.pem
# Lock down — TURN open to the world is a relay-spam risk.
# 200 Mbps cap is generous for a small household.
total-quota=200
bps-capacity=200000
no-loopback-peers
no-multicast-peers
log-file=/var/log/turnserver/turnserver.log
no-stdout-log
```

Open 3478/tcp+udp + 5349/tcp+udp on your firewall. Ports 49152-65535
UDP need to be open too for the actual relay channels (or pin a
narrower range via `min-port` / `max-port`).

Then on **every** SocialHome instance, set:

```toml
# socialhome.toml
[network]
webrtc_stun_url = "stun:<your.public.hostname>:3478"
webrtc_turn_url = "turn:<your.public.hostname>:3478"
webrtc_turn_secret = "<same long-random-string as coturn's static-auth-secret>"
webrtc_turn_ttl_seconds = 3600
```

…or via env vars `SH_WEBRTC_STUN_URL`, `SH_WEBRTC_TURN_URL`,
`SH_WEBRTC_TURN_SECRET`, `SH_WEBRTC_TURN_TTL_SECONDS`.

Restart the SocialHome process. The next federation handshake will
include a TURN candidate; a household that can't pair-check
directly will allocate a relay channel on coturn and pair through
that.

## How HMAC time-limited credentials work

coturn's `--use-auth-secret` mode expects each client to present a
username of the form ``<expiry>:<user_id>`` (where ``expiry`` is a
Unix timestamp) and a password that's `base64(HMAC-SHA1(secret,
username))`. The server recomputes the HMAC and checks that
``expiry`` is in the future. Credentials thus expire — a leaked
TURN credential is bounded by ``webrtc_turn_ttl_seconds`` (default
3600 = 1 h).

SocialHome derives these credentials on demand:

* **SPA users** (calls, live highlights) — `user_id` is the
  authenticated user's id; credentials are issued via
  `GET /api/calls/ice-servers` (auth-required).
* **Federation transport** (server-to-server) — `user_id` is the
  local instance's `instance_id`. The list is derived once, at process
  startup. A rebuilt PeerConnection (after a FAILED PC, or after a peer
  is retired because the ICE list moved on) re-reads the list the
  transport currently holds — it does not re-derive a fresh credential.
  Only an applied ICE-server list replaces it (see "Home Assistant
  deployments" below). So on a long-running process, federation keeps
  presenting the credential minted at boot: pick a
  `webrtc_turn_ttl_seconds` that outlasts your uptime between restarts,
  and restart Social Home after changing `webrtc_turn_secret`.

Both surfaces use the same shared secret (`webrtc_turn_secret`).
You do **not** need separate secrets per instance — the
`expiry:user_id` scheme already binds each credential to its
consumer.

## Home Assistant deployments (`ha` / `haos`)

Under either Home Assistant mode, Social Home **pulls** an ICE-server list
from HA Core over the WebSocket (`web_rtc/ice_servers`) once shortly after
boot and then every 24 h, and that list **replaces** the TOML-derived one
*for the federation transport*. That is how a Nabu Casa Cloud TURN server
reaches Social Home without any configuration on your part — and why the
cadence is daily, roughly matching the cloud credential's lifetime.

What this means for the settings above:

* If HA supplies servers, your `webrtc_turn_url` / `webrtc_turn_secret` are
  **not** what federation ends up using. Setting them is still worthwhile as
  the pre-pull default and for the SPA surfaces, which are unaffected by the
  pull.
* If HA has nothing to offer (no cloud subscription, WebRTC integration
  absent) the reply is ignored rather than applied, so your configured
  servers stay in effect.
* Every applied list is re-checked by the same diagnostics as boot, so a
  pulled list with no TURN — or TURN without credentials — is reported in the
  log rather than failing silently.
* The first federation handshake after boot waits up to **15 s** for that
  first pull to land, so the boot-time outbox drain doesn't build every peer
  STUN-only moments before the TURN credentials arrive. The wait is paid at
  most once per process (by whichever peer handshakes first), and the sync
  releases it after its first fetch *attempt* whatever the outcome — an HA
  Core that is slow, unreachable, or has nothing to offer costs that bound
  once, never a stalled transport.
* A list that lands *after* peers were already built still reaches them —
  see "When TURN arrives late" below.

Self-hosted and standalone deployments never pull; the TOML settings are the
steady state there, and they never pay the 15 s wait.

If you'd rather use static long-lived credentials (e.g. a hosted
TURN provider that only supports username/password), set
`webrtc_turn_user` / `webrtc_turn_cred` instead. The HMAC path
wins when both are configured.

## When TURN arrives late, and when a failed peer retries

Two behaviours matter when you deploy or change TURN on a running system.

**Peers that never connected are rebuilt under the new list.** Applying an
ICE-server list whose content actually changed bumps an internal
*generation* counter. Every peer records the generation it was built under,
and a peer that has not managed to open its DataChannel while sitting behind
the current generation is torn down and rebuilt — with the new servers — on
the next envelope addressed to it. Already-connected peers are left alone:
their channel works, and renegotiating it buys nothing. This is what fixes
the symptom where peers built during the boot outbox drain, before the TURN
credentials landed, stayed STUN-only for the entire life of the process.

**A failed handshake backs off, but no longer for a day.** When a
PeerConnection reaches `failed`, the transport suppresses rebuilding that
peer for a while — without it, every queued envelope would rebuild and
re-fail, and the outbox polls every 5 s. The window grows with the peer's
consecutive failure count, capped at 6 h and jittered by ±20 % so a
household whose peers all failed together doesn't retry them in lockstep:

| Consecutive failure | Suppressed for (before jitter) |
|---|---|
| 1st | 60 s |
| 2nd | 4 min |
| 3rd | 16 min |
| 4th | ~64 min |
| 5th | ~4.3 h |
| 6th and later | 6 h (cap) |

The 60 s floor is the anti-hammer guarantee: even at full outbox cadence a
failing peer costs at most one handshake per minute. A genuinely unreachable
peer reaches the 6 h ceiling after roughly 1.5 h and settles at a handful of
attempts per day. The payoff is the other end of the scale — a *transient*
failure now recovers in about a minute, where the flat 24 h rule this
replaced cost a full day of HTTPS-only federation for that peer. The count
resets as soon as the peer's DataChannel opens.

**Applying an ICE-server list is the "try again now" lever.** Every applied
list — content changed or not — clears all current suppressions *and* every
accumulated failure count, so the next outbound envelope retries each failed
peer immediately, and a fresh failure starts again from the 60 s base rather
than from whatever ceiling it had climbed to. It is the only thing that
clears a suppression early. Under Home Assistant the daily pull does this
for you; elsewhere, restarting Social Home has the same effect (a restart
also re-derives the HMAC credentials — see above).

## Testing

Quick sanity check that TURN is reachable + authenticating:

```bash
# Get a fresh credential the way SocialHome would
python -c "
from socialhome.webrtc_ice import make_turn_credential
u, p = make_turn_credential('<your secret>', 'test-user', ttl_seconds=600)
print('username:', u)
print('credential:', p)
"

# Try a TURN allocate against your server using those creds.
# coturn ships with ``turnutils_uclient`` for this. From the coturn
# install:
turnutils_uclient -u <username> -w <credential> <your.public.hostname>
```

If the allocate succeeds, the federation transport's TURN
candidates will also work. If it fails with `401 Unauthorized`,
double-check that ``static-auth-secret`` on the server side
matches ``webrtc_turn_secret`` in your TOML byte-for-byte.

## Privacy / abuse notes

* **Don't expose a TURN server without authentication.** Open
  relays are scraped within minutes and used to NAT-traverse out
  of your network for various flavours of abuse. coturn's
  `--use-auth-secret` (the recipe above) gates every allocation
  behind a fresh HMAC.
* **TURN sees the encrypted DTLS stream, not the plaintext.** The
  SocialHome federation envelope sealed by `SpaceContentEncryption`
  / `routed_crypto` is still end-to-end encrypted between
  households; the TURN operator just sees opaque relayed bytes.
  Same guarantee applies to the SPA's calls (SRTP keyed by DTLS).
* **A self-hosted TURN box logs allocations.** Consider rotating
  `static-auth-secret` periodically and tuning coturn's
  `--log-file` retention to your privacy policy.

## Spec references

- coturn TURN-REST: <https://github.com/coturn/coturn/wiki/TURN-REST-API>
- WebRTC ICE: [RFC 8445](https://datatracker.ietf.org/doc/html/rfc8445)
- TURN: [RFC 5766](https://datatracker.ietf.org/doc/html/rfc5766)
