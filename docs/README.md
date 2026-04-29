# Social Home — Documentation

Reference material for running and understanding Social Home.

## Contents

- **[principles.md](./principles.md)** — Design principles distilled
  from §2: households first, encryption-first, no third-party trust,
  fail closed on crypto, GPS truncation, layered architecture.
- **[architecture.md](./architecture.md)** — System shape: HFS ↔ GFS
  topology, identity (§4.1), progressive sync (§4.2), space crypto
  (§4.3), resilience (§4.4), and where each concern lives in the
  source tree.
- **[database.md](./database.md)** — Table-by-table reference for the
  v1 SQLite schema, grouped by domain. Source of truth is
  `socialhome/migrations/0001_initial.sql`.
- **[testing.md](./testing.md)** — Test strategy: layout, markers,
  the 90 % branch-coverage gate, and the §27.9 release-blocker
  protocol tests under `tests/protocol/`.
- **[api.md](./api.md)** — REST API reference for the Household Federation
  Server (HFS) and Global Federation Server (GFS), plus WebSocket channels
  and the inbound federation inbox.
- **[crypto.md](./crypto.md)** — Cryptographic design: identity keys,
  pairing DH, per-space session keys, post-quantum migration (§25.8).
- **[protocol/](./protocol/)** — The federation protocol, feature by
  feature. Start with [protocol/README.md](./protocol/README.md) for the
  HFS ↔ GFS architecture overview.

## Glossary

- **HFS — Household Federation Server.** The per-household instance.
  Runs either inside Home Assistant (`SOCIAL_HOME_MODE=ha`) or as a
  standalone service. Source lives under `socialhome/` (excluding
  `socialhome/global_server/`).
- **GFS — Global Federation Server.** A public relay service operated
  per community. Provides the public-space directory, push fan-out, and
  WebRTC signalling bootstrap. Source lives under
  `socialhome/global_server/`.
- **Space.** A group context shared across households — a private
  family space, a neighbourhood watch, a public community. Spaces are
  the unit of content federation.
- **Pairing.** The one-time handshake that establishes an end-to-end
  encrypted trust relationship between two HFS instances.
- **Envelope.** A signed, AES-256-GCM-encrypted JSON payload — the
  unit of federation traffic. Delivered over WebRTC DataChannel when
  possible, falling back to HTTPS inbox.

## Where the spec lives

The authoritative specification is `spec_work.md` in the
meta-repository (`social-home-io/social-home`), at the repo root. These
docs are derived from the current code plus the spec — when they
disagree, the code wins and the docs should be fixed. Spec section
references appear throughout as "§NN".
