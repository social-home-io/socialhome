# CLAUDE.md — socialhome

Canonical AI-agent instruction file for Social Home core. Symlinked as
`AGENTS.md`, `.cursorrules`, and `.github/copilot-instructions.md` — edit
only this file so the agent surfaces can't drift.

Read `spec_work.md` (the spec) before any change. The spec is the source of
truth — if code and spec disagree, fix the code.

### Delivery workflow

For any **non-trivial change** (a feature, refactor, multi-file change, a
risky bug fix, or anything you'd open a PR for), follow the
**`superpower-light`** skill (`.claude/skills/superpower-light/`): plan →
decompose into bite-sized TDD tasks → implement each with a fresh subagent →
review every stage twice (spec-compliance then code-quality; adversarial for
security-sensitive code) → final whole-change review → ship via a PR on green.
Its hard gates apply to all such work:

- **Debug to root cause before any fix** — no symptom patches; add a
  regression test.
- **Verify before you claim it** — run the command in the same turn and quote
  the real output; never claim a pass you didn't observe (a subagent's
  "success" report is not evidence).
- **PR-only — never commit or push to `main` directly.** Every change lands
  through a pull request; branch first.
- **Don't merge your own PRs** — open it, drive CI green, hand off to a human;
  self-merge only on explicit, per-session authorization.

Trivial edits, Q&A, and read-only exploration skip the skill — but the
PR-only and verify-before-claiming gates still hold for anything that lands.

### Architecture

- **Platform adapters:** `SH_MODE` selects one of three: `standalone` →
  `StandaloneAdapter` (local users, password auth), `ha` → `HaAdapter`
  (HA Core REST + local password auth), `haos` → `HaosAdapter` (HA
  Supervisor add-on, Ingress). All adapter-specific code lives in
  `socialhome/platform/`. Never branch on `config.mode` outside `platform/`
  and `config.py` — query `adapter.capabilities`. Never `isinstance`-check
  concrete adapters — use Provider methods (`adapter.auth.authenticate`,
  `adapter.users.list_users`). Lifecycle hooks (`on_startup`, `on_cleanup`,
  `get_extra_services`, `get_extra_routes`) handle mode-specific wiring.
  HA-automation platform events are defined in `HaBridgeService` — add new
  event types there, not in `app.py`.
- **Service layer:** all business logic in `socialhome/services/`. Route
  handlers in `routes/` are thin `BaseView` subclasses — one view class per
  REST resource, dispatched by HTTP method (`get/post/patch/delete`). Use
  `self.svc(key)`, `self.user`, `self._json(data)`. Domain exceptions are
  mapped centrally by `BaseView._iter()` — handlers need no try/except. See
  `routes/base.py`. No SQL in handlers; no business logic in repos.
- **Repository pattern:** `repositories/` holds abstract bases + SQLite
  impls. Services depend on `Abstract*Repo` Protocols only, never concrete
  `Sqlite*Repo`. Exception: `backup_service`, `data_export_service`, and
  `recovery_kit_service` (whole-table dumps) use raw SQL.
- **No SQL outside `repositories/`:** services/routes call repo methods, not
  `db.fetchall` / `db.enqueue` / `db.transact`. New SQL in a service is a
  smell — extract it into the appropriate repo.
- **Domain layer:** `domain/` holds pure dataclasses — no I/O, no imports
  from services/repos, frozen (`@dataclass(slots=True, frozen=True)`).
  Row-shaped dataclasses live here, not in `repositories/`. Exception:
  repo-local DTOs (e.g. `SearchHit`) that never escape the repo — mark
  "repo-local DTO" in the docstring.
- **`create_app` stays thin:** new wiring goes in the matching `_build_*`
  factory in `app.py` (`_build_repos`, `_build_middleware`). `create_app`
  orchestrates, doesn't enumerate.
- **Async everywhere:** all I/O is `async def`. Never `time.sleep()` — use
  `asyncio.sleep()`. Never block without `run_in_executor`.
- **Filesystem I/O via `aiofiles`** — never `open(...)`,
  `Path.read_bytes/write_bytes/read_text/write_text/is_file/exists/stat/mkdir/unlink/replace`,
  or `shutil.*` inside `async def`. Use `aiofiles.open` and `aiofiles.os`
  (`path.isfile`, `stat`, `makedirs`, `remove`, `replace`). Refs:
  `routes/media.py`, `services/dm_media_sync_service.py`,
  `services/federation_inbound_service.py` (DM media blob assembly),
  `services/highlight_signaling_handler.py` (DataChannel streaming).
- **CPU-bound work via `asyncio.to_thread`** — Pillow decode/encode, QR PNG
  render, gzip / tarfile, hashlib over large buffers, anything ≥ tens of ms
  of CPU. Pattern: `return await asyncio.to_thread(self._sync_helper, *args)`
  with the blocking body in a sibling sync method. Refs:
  `media/image_processor.py` (`process` / `generate_thumbnail`),
  `media/video_processor.py` (PyAV transcode), `media/audio_processor.py`
  (PyAV probe), `services/audio_transcription_service.py` (Opus→PCM16),
  `services/backup_service.py` (`_build_tarball_bytes`, `_read_restore_tar`),
  `global_server/public.py` (`_render_qr_png_data_uri`),
  `services/gallery_service.py` (`_extract_exif_date_sync`, `_read_dims_sync`).
- **Schedulers follow the `asyncio.Event` lifecycle:** `_stop: asyncio.Event`
  set in `stop()`, drained in `start()`, body `while not self._stop.is_set()`.
  Template: `infrastructure/replay_cache_scheduler.py`. Never use a
  `_running: bool` flag — that pattern is gone. Cluster / heartbeat /
  discovery loops follow suit (`global_server/cluster.py:_heartbeat_loop`,
  `services/public_space_discovery_service.py:_poll_loop`) — bare `cancel()`
  can rip a task out mid-DB-write or mid-HTTP-request. Sole exception: the
  sentinel-driven queue worker `db/database.py:_writer_loop` (documented so
  nobody "fixes" it).

### Design patterns

- **Registry** for federation event dispatch. New inbound handlers register
  via `_event_registry.register(event_type, handler)` in the service's
  `attach_*` method. Never add if/elif branches to `_dispatch_event`. Ref:
  `federation/event_dispatch_registry.py`.
- **Middleware chain** for the §24.11 inbound federation validation pipeline.
  Each step (JSON parse, instance lookup, timestamp, signature verify, replay,
  decrypt, idempotency, ban, persist replay) is an independently-testable
  async callable composed via `InboundPipeline`. Add steps by appending to
  the chain — never edit the monolithic `handle_inbound_envelope`. The same
  chain validates HTTPS-inbox and DataChannel envelopes. Ref:
  `federation/inbound_validator.py`.
- **Strategy** for transport + envelope crypto. `HttpsInboxTransport`,
  `_RtcPeer`, `FederationEncoder` satisfy `TransportStrategy` /
  `EncryptionStrategy` in `federation/strategies.py`. New transports/schemes
  plug in via the protocol — no `FederationService` edits.
- **Specification** for repo list/search reads with arbitrary filters. Build
  `Spec(where=[...], order_by=[...], limit=N)` and call `repo.find(spec)`.
  The repo's `_*_COLS` allow-list defends column names against injection.
  Keep bespoke `list_*` for the common cases — Spec is the escape hatch, not
  a replacement. Ref: `repositories/_spec.py` (used by `notification_repo`,
  `post_repo`).
- **Unit of Work** for handlers needing >1 write + ≥1 domain event together.
  `async with UnitOfWork(db, bus=bus) as uow` opens `BEGIN IMMEDIATE`,
  buffers `uow.exec(...)`, and dispatches `uow.publish(...)` only after
  commit — so a crash never publishes events whose writes rolled back. See
  `db/unit_of_work.py`. Single-write handlers use `db.enqueue` directly.
- **Adapter** for platform integration. The `PlatformAdapter` ABC
  (`platform/adapter.py`) composes `*Provider` Protocols (`AuthProvider`,
  `UserDirectory`, `PushProvider`, `STTProvider`, `AIProvider`,
  `ExternalEventSink`) plus a `capabilities` set
  (`Capability.PUSH | STT | AI | INGRESS | PASSWORD_AUTH | HA_PERSON_DIRECTORY`).
  Concrete adapters wire providers in their constructor and never branch on
  `config.mode`. Add a platform = new adapter + its providers; call sites
  consume via the Provider interfaces. Refs: `platform/adapter.py`,
  `platform/{standalone,ha,haos}/`, `platform/local_credentials.py` (shared
  password+token machinery for adapters opting into `PASSWORD_AUTH`).

### Code Conventions

- Python 3.14+. `match/case` for event dispatch.
- Type hints on all public methods. `str | None`, not `Optional[str]`.
- `log = logging.getLogger(__name__)` at module level — never `print()`.
- **All imports at file top** — never inside a function/method. Only
  exception: `if TYPE_CHECKING:` for circular-import type annotations. If a
  top-level import is circular, restructure or use `TYPE_CHECKING`.
- SQLite: `AsyncDatabase.enqueue()` for writes (WAL coalescing);
  `fetchall()` / `fetchone()` for reads.
- Raise domain exceptions (`SpaceNotFoundError`, etc.) in services; handlers
  map them via `_map_exc()`.
- Never add `*Addendum` / `*Extension` / `*Complete` subclasses — merge into
  the existing class.
- **Prefer mixins for cross-cutting behaviour.** When the same
  attribute+method pair recurs across unrelated services (presence, DMs,
  highlights, moments all filter on the per-pair visibility hide list), pull
  it into a mixin. **A mixin must own a shared *method* (behaviour), not just
  a slot** — inheritance purely to declare a field is the `*Addendum`
  anti-pattern (a `_bus`-only mixin over the 50+ services that hold a bus is
  the textbook bad case; what's shareable is the *fail-soft publish*, not the
  field). Two slot styles, picked by composability:
  - **Owns the slot** (`__slots__ = ("_field",)`, subclass writes it in
    `__init__`, inherits the reader) — only when the mixin is the *sole*
    slotted base for every consumer. Canonical:
    `socialhome.services.visibility.VisibilityMixin` (~10 outbound services).
  - **Behaviour-only** (`__slots__ = ()`, reads a slot the consumer declares)
    — required when a consumer composes it with another slotted mixin. Python
    forbids inheriting from >1 base with non-empty `__slots__` ("multiple
    bases have instance lay-out conflict"), so composable mixins carry no
    slots. Refs: `services.bus_publisher.BusPublisherMixin` (`_emit`),
    `services.peer_outbound.{ConfirmedPeerBroadcaster,SingleTargetSender}`. A
    consumer that needs a narrower type for the borrowed slot re-declares it
    (e.g. `_federation: "FederationService"` to narrow the mixin's
    `| None`).
- **Module filenames never start with `_`.** New shared helper →
  `socialhome/services/visibility.py`, test → `tests/services/test_visibility.py`.
  Privacy is signalled by package boundaries + `__init__.py` re-exports, not
  filenames. Existing `_`-prefixed modules (`repositories/_spec.py`) are
  grandfathered; rename only on mechanical touch.

### Frontend (SPA) & ingress

The Preact SPA in `client/` runs behind three document bases by mode:

- **standalone / dev** — base `/`; `fetch('/api/me')` resolves against the
  origin and reaches the backend.
- **ha** — same as standalone when reachable directly, or behind a reverse
  proxy with a path prefix.
- **haos** — base is HA Supervisor's `/api/hassio_ingress/<token>/`. The
  Supervisor proxies every request to the add-on, ADDING the auth headers
  (`X-Hass-Source: core.ingress`, `X-Remote-User-Name`) that the backend's
  `HaIngressStrategy` accepts as the handshake. The SPA carries NO bearer
  token in this mode.

All three load the same `index.html` + JS bundle. `SpaIndexView` rewrites
`<base href>` from the Supervisor-injected `X-Ingress-Path` header per
request; the SPA reads `document.baseURI` once at module load and anchors
every runtime URL on it.

Rules that keep this working:

- **Every SPA URL anchors on `document.baseURI`**, never on the origin. Use
  the `client/src/baseUrl.ts` helpers:
  - API fetches: call `api.get/post/patch/delete` — the client strips the
    leading slash so `fetch` resolves against `<base href>`. Never
    `fetch('/api/...')` directly. Same for WebSockets — the `ws` manager
    passes `'api/ws'` (no leading slash).
  - `window.location.href` / `.assign` to the app root: use `basePath`.
    Hard-coding `'/'` skips the ingress prefix and bounces the iframe to HA
    Core's frontend.
  - `window.location.href` to an in-app path: wrap in `addBase('/spaces/...')`.
  - markdown / user-supplied links: `utils/markdown.ts` already routes
    through the base — don't reinvent.
- **Auth is mode-agnostic:** `isAuthed` is `currentUser != null`. A successful
  `/api/me` is the only signal of a working handshake (bearer for
  standalone/ha, ingress for haos). Never re-add a `token != null` gate.
- **haos SPA never has a token.** `POST /api/setup/haos/complete` returns
  `{username}` and no token — ingress is the only entry point. Cold-start
  probes `/api/me` with no `Authorization` header. New haos setup endpoints
  follow this (no token, no password collection); standalone/ha setup
  endpoints DO return a token.
- **A 401 without a stashed token is NOT session expiry.** The api client
  guards the "Session expired" toast on `token.value !== null` — never toast
  for an ingress probe that 401'd. The App shell renders `IngressAuthFailed`
  instead (a reload-the-sidebar CTA, not a password form).
- **Tests cover the ingress shape:** `client/src/baseUrl.test.ts` (path
  helpers) and `IngressLocationProvider.test.tsx` (router). New URL surfaces
  (redirect, fetch target, icon/manifest ref) extend these — the test names
  mention the ingress prefix so a future contributor sees the invariant first.

Refs: `client/src/baseUrl.ts` (`basePath`, `addBase`, `stripBase`),
`client/src/api.ts` (`_rel` strips leading slashes), `client/src/ws.ts`
(relative WS path resolves against `document.baseURI`),
`client/src/router/IngressLocationProvider.tsx` (preact-iso glue),
`socialhome/routes/spa.py` (`SpaIndexView` rewrites `<base href>`),
`socialhome/auth.py` (`HaIngressStrategy` accepts the ingress headers).

### Visually verifying UI changes

**Every change that touches the SPA — layout, components, CSS, copy, dialog
flow, form interactions, anything a user can see or click — MUST be both (a)
visually verified in Chrome on desktop AND mobile viewports AND (b) walked
through a UX review pass before the PR is opened.** This applies to every
change that mounts a component, mutates the DOM, or alters user-facing copy.
`tsc` + Vitest cover code correctness, not visual/UX correctness — a button
can pass both and still bleed off-screen on a 360 px phone or strand a date
input in the past.

Use the `chrome-devtools-mcp:chrome-devtools` skill against a local `pnpm dev`
boot. `resize_page` flips sizes; reuse the same tab to compare the same state.

Both viewports are mandatory:

- **Desktop ~1280×720** — layout reads as designed, nothing overflows the
  column, hover/focus rings land where expected.
- **Mobile ~390×844** (iPhone 14 / Pixel-class) — strip/chip/form wraps, tap
  targets ≥ 36 px tall, no horizontal scroll on body content, no clipped
  text, sticky chrome doesn't cover the active control.

The UX review pass is mandatory too — walk the touched surface with the
user's goal in mind and ask:

- **Defaults:** is the pre-filled state right 90% of the time? (e.g. when a
  start date moves past the end date, does the end follow?)
- **Error / empty states:** what shows on network fail, empty list, invalid
  input? Is the next action obvious from that state alone?
- **Discoverability:** can a first-timer find the control? Is the affordance
  (button vs link vs chip) honest about what happens on click?
- **Recovery:** can a wrong choice be backed out in one click?
- **Cross-state coherence:** right with 0 / 1 / many items, mobile keyboard
  up, long copy in another language, RTL?
- **Keyboard / a11y:** reachable and operable by keyboard / screen-reader?
  Do focus rings render visibly against the surface tint?

Screenshot both viewports + a short UX note in the PR description. "No issues"
is valid only if you actually looked. If the dev server is unreachable
(sandbox/offline), say so in the PR rather than claiming verification — the
missing screenshots are the signal for a reviewer to reproduce locally.

### Federation & Security

- Every inbound federation event MUST pass the §24.11 pipeline: JSON parse →
  timestamp ±300 s → instance lookup → ban check → Ed25519 verify → replay
  cache → dispatch. Never skip signature verification for "trusted" instances.
- GPS coordinates: truncate to 4 decimal places before any storage or
  transmission — `round(float(lat), 4)`, never raw device precision.
- WebSocket auth uses `?token=` (unavoidable for browser WS). Tokens leak
  into access logs and browser history — never log the full `/api/ws` query
  string; operators must redact tokens.
- Push payloads: title only; body omitted for DMs, location messages, and any
  user-generated content (§25.3).
- `SENSITIVE_FIELDS` frozenset in `security.py` — never expose these in API
  responses.

### Testing

- Unit tests: no real network/disk; all repos are in-memory stubs.
- Integration tests: real SQLite in `tmp_path`, real aiohttp `TestClient`.
- **Every module under `socialhome/` has a same-named test** at
  `tests/<subpath>/test_<module>.py` (`services/foo.py` →
  `tests/services/test_foo.py`), added in the same commit as the production
  file. Only exception: `__init__.py` re-export shims.
- `tests/protocol/` is a release blocker — never skip.
- Run `pytest tests/protocol/ -m security` before any commit touching
  federation or presence code.
- Coverage gate: 90% branch — `pytest --cov=socialhome --cov-fail-under=90`.

### Pre-commit hooks

- Config `.pre-commit-config.yaml`; install once per clone:
  `pip install pre-commit && pre-commit install`.
- Hooks: ruff (lint + format), mypy (on `socialhome/`), frontend ESLint +
  `tsc --noEmit` on staged TS/TSX, `pnpm build` at pre-push.
- A failing hook means fix the underlying issue — never `--no-verify`.

### Releases

- CalVer `YYYY.M.D`, no `v` prefix. Version is dynamic via `hatch-vcs` —
  never edit the version field in `pyproject.toml`; the tag IS the version.
- The **Release — draft** workflow (`.github/workflows/release-draft.yml`)
  keeps a draft release reflecting the current merge backlog. Fires on every
  push to `main` (every PR merge) and on manual `workflow_dispatch` (optional
  `date` input back-dates). Each run computes the next `YYYY.M.D[.N]` tag
  (bumping `.N` when a same-day release is already published), auto-generates
  notes from merged PRs since the last published release, and creates/recreates
  the draft. Drafts are by contract the auto-generated view — don't keep
  hand-polish there. Publishing from the UI creates the tag and fires
  `release.published`. One-offs: `gh release create 2026.5.12 --target main
  --title 2026.5.12 --notes …`.
- `.github/workflows/publish.yml` listens for `release.published` and runs
  three parallel jobs: PyPI (OIDC trusted publishing), core Docker →
  `ghcr.io/social-home-io/socialhome`, GFS Docker →
  `ghcr.io/social-home-io/gfs`. Each push gets four tags: pinned
  (`:2026.5.10`), month (`:2026.5`), year (`:2026`), `:latest`.
- Bare `git tag && git push --tags` does NOT publish — only `release.published`
  triggers the publish workflow, so operators always go through the release
  UI/CLI and the pipeline never runs twice.

### PR labels (drive the changelog)

Auto-generated notes group PRs by label (`.github/release.yml`), so **every
PR needs ≥1**:

| Label | Bucket | When to use |
|---|---|---|
| `breaking` | Breaking changes | API / config / schema change needing migration on the user's side |
| `feat` | Features | New user-visible capability |
| `fix` | Fixes | Bug fix, regression repair, edge-case hardening |
| `security` | Security | Vulnerability fix, hardening on auth / crypto / federation |
| `docs` | Docs | Docs-only change |
| `chore` | Other changes | Tooling, devcontainer, CI, dependency bump, no-behaviour refactor |

The label drives the bucket; the PR title shows underneath — write a tight,
user-facing title (*"Pictures are click-to-zoom in the gallery again"*), not
a conventional-commit prefix. Attach with `gh pr create --label fix`.
Exclusions: `dependabot` PRs and the `skip-changelog` / `dependencies` labels
drop from the notes — use `skip-changelog` for trivial backend cleanup with no
user-visible effect. An unlabelled PR falls to *Other changes*; reviewers
should bounce it.

### Encryption-First Rule (§25.8.21)

Every field in every outgoing federation event is encrypted unless the
federation service needs it in plaintext to route or validate the event. New
event:
- Routing fields (event_type, from/to instance, space_id, epoch) → plaintext.
- Everything else (content, names, counts, choices) → inside the encrypted
  payload.
- Never add a `"payload": plaintext_fallback`.
- If `SpaceContentEncryption` isn't configured, raise `RuntimeError` — never
  degrade silently.

**Hard rule — non-member households MUST NOT see space content.** A household
that isn't a space member but sits on the mesh between host and a remote
member is a routing relay; it must not read any space content (posts,
comments, reactions, calendar events, location pins…):

- **Direct delivery → members only:** use `broadcast_to_space_members` for
  every space-content fan-out (targets `space_instances` — households with at
  least one member — not arbitrary paired peers). A `SPACE_*` event to a
  non-member household is a bug, not an optimization.
- **Mesh-routed → end-to-end sealed:** when a path crosses a non-member relay,
  wrap with `SPACE_ROUTED` so the relay sees only ciphertext sealed under the
  target's ephemeral X25519 pub (`socialhome/federation/routed_crypto.py`);
  the relay can't derive the shared secret. That sealing is for the mesh, not
  the GFS. GFS-relayed public/global content is encrypted under the space
  content key (`SpaceContentEncryption.encrypt`) inside a space-authority-
  signed envelope the content-blind GFS relays as-is
  (`services/space_public_outbound.py` / `space_public_inbound.py`). That
  relay is also **identity-free**: `POST /gfs/publish` carries exactly
  `{space_id, event_type, payload}` and is authorized on the authority
  signature alone — never send `from_instance` to a connection server.
- **Content key is membership-gated:** the per-space AES-256 content key
  (`SpaceContentEncryption`) is delivered to a new member via
  the §D1b key handoff (`apply_space_content_key_from_metadata`), never to a
  relay. Removed members lose access at the next epoch rotation (forward
  secrecy).
- **The rule is about *transport*.** A member who has the matching key
  decrypts and stores content locally like any other row (plaintext-at-rest
  under the local KEK for sensitive bytes). Re-encrypting at rest for
  receiving members is out of scope.

For new federation surfaces (space content, state mutations, roster events),
check both delivery paths — if an event could reach a non-member household,
route it through `space_instances` or wrap with `SPACE_ROUTED` end-to-end.

### Crypto wire shapes carry a `*_suite` tag (PQ-forward by default)

Every cryptographic wire shape — signature, KEM, symmetric AEAD, key delivery
— MUST carry a suite identifier so the algorithm can swap without breaking
older receivers. Receivers MUST reject unknown suites; never fall back to a
default.

Reference implementations:

- **Signatures:** `Encoder.sig_suite` per envelope (`federation/encoder.py`).
  Today `"ed25519"`; Phase-2 introduces `"ed25519+mldsa65"` (both signatures
  produced + verified in parallel).
- **KEM (mesh routing):** `KEM_SUITE_X25519` (`federation/routed_crypto.py`).
  Phase-2 introduces `"x25519+mlkem768"` (pubkey field carries concatenated
  X25519 + ML-KEM material).
- **Symmetric content key delivery:** `KEY_SUITE_AESGCM_256`
  (`services/space_crypto_service.py`). AES-256 is Grover-resistant (~128-bit
  PQ); the PQ lever is the delivery channel, not the symmetric primitive.
- **Pairing crypto:** `federation/crypto_suite.py` (negotiate / parse /
  validate).

When you add a new crypto wire format:

1. Module-level `<NAME>_SUITE_<VARIANT>` constant + `SUPPORTED_<NAME>_SUITES`
   frozenset that includes it.
2. `Unsupported<Name>Suite(ValueError)` exception.
3. Ship the suite id on the wire (every dict the receiver parses; never
   positional/unlabelled).
4. Receivers validate against `SUPPORTED_<NAME>_SUITES` and raise on unknown
   values — no default fallback.
5. At migration: add a sibling constant, grow the frozenset, ship the new
   variant's extra material (concatenated pubkeys, parallel signatures) — the
   wire shape itself is unchanged.

First-revision payloads missing a suite field default to the single supported
value (see `apply_space_content_key_from_metadata`); once universally shipped,
the default-on-missing branch becomes the migration tripwire. Full Phase-1→2
PQ plan: `docs/crypto.md`. Every shipped crypto wire shape now carries its
suite tag (prior gaps closed); new surfaces add the sibling pattern.

### Before adding a SQL migration, audit the code path

A migration extends the on-disk shape — a one-way ratchet (operator-painful to
roll back) run on every deployment's startup. Before writing
`socialhome/migrations/00NN_*.sql`, prove three things in the PR description:

1. **You audited every code path that already touches this data.** Grep the
   tables/columns, read the handlers (especially federation inbound, sync, and
   the migration that defined the column). The existing shape or another layer
   (a federation event, a service cache, a derived value) is often the right
   home.
2. **A non-migration alternative was considered and rejected** — reusing a
   column, computing at read time, storing in a federation event, decomposing
   into an existing field. Migrations are often right but never the *first*
   answer; pass through "should this even be in the database".
3. **The migration is the smallest possible change** — additive over
   destructive (`ADD COLUMN` over `RENAME TABLE`, NULL default over backfill,
   INDEX over partial-table rewrite). Changing existing rows has a much higher
   bar than adding a NULL-defaulted column.

Reviewers push back on any migration PR whose description doesn't address all
three. The `migrations/` directory is the project's longest-running data-shape
commitment — every production row passes through every migration in order on
startup, so changing the shape later means another migration, not an edit.

### Federation protocol versioning

Every confirmed peer carries a monotonic `proto_version: int` on its
`remote_instances` row, exchanged at startup via
`INSTANCE_CAPABILITIES_UPDATED`. Senders gate optional fields on
`FederationService.peer_supports(instance_id, min_version=N)` so a v_N field
never reaches a v_(N-1) peer. Full design/history:
[`docs/protocol/capabilities.md`](docs/protocol/capabilities.md).

When you add a federation surface an older peer cannot fail-soft against (a
new `FederationEventType`, or a field whose default-if-missing would be
silently wrong rather than just "unknown"):

1. Bump `OURS` in `socialhome/domain/federation_capabilities.py`.
2. Add a named `FederationCapability` constant (e.g. `MIN_FOR_OCCURRENCE_OVERRIDE`)
   so call sites reference intent, not a magic number; document v_N in the
   `OURS` history list.
3. Gate the outbound on `peer_supports(peer_id,
   min_version=FederationCapability.X)` with a degraded fallback for older
   peers (or skip the send if none is safe).
4. Append a row to the version-history table in `docs/protocol/capabilities.md`
   (what v_N changed, what an older peer's fallback is).
5. Update the demo harness in `.claude/skills/federation-demo/` so `verify`
   asserts the new version round-trips.

Per-peer feature-flag sets on top of the integer are deferred until there's
real evidence of selective deployments, asymmetric support, or forks — a
single monotonic integer covers every case today (adding flags later is a
one-line additive migration).

### Keep docs in sync

`docs/` is the public reference for the federation protocol and HTTP API —
treat the matching doc file as part of the same change:

- **Added/renamed a `FederationEventType`?** Update the `Event types` list on
  the matching `docs/protocol/` page. A new feature with no page → add one,
  copying `docs/protocol/pairing.md`'s shape (summary, scope, event types,
  Mermaid sequence diagram, impl pointers, spec refs) and link from
  `docs/protocol/README.md`.
- **Changed a feature's message flow** (new signalling step, `_VIA` relay,
  transport swap)? Update the Mermaid sequence diagram on the matching page.
- **Added/removed an HTTP endpoint?** Update the `docs/api.md` table (+ the
  Rate limits table if rate-limited; + the WebSocket row for a new frame type).
- **Changed the crypto suite** (signature algorithm, KDF, envelope format)?
  Update `docs/crypto.md`.
- **Added/dropped/renamed a table / column / index, or shipped a migration?**
  Update `docs/database.md` under the existing domain heading (Identity,
  Spaces, DMs…), keeping the one-paragraph "Purpose" shape.
- **Architecture-level change** (new sync tier, resilience step,
  identity-rotation tweak, GPS-precision change, new Provider Protocol, new
  platform mode)? Update `docs/architecture.md`.
- **Touched a §2 invariant** (relaxing encryption-first, allowing third-party
  trust, raising GPS precision, removing fail-closed behaviour)? Update
  `docs/principles.md` AND flag it in the PR for explicit sign-off.
- **Changed the test strategy** (new test dir, pytest marker, coverage-gate
  change, §27.9 protocol-test set)? Update `docs/testing.md` to match
  `pyproject.toml`.
- **New top-level `docs/` file?** Link it from `docs/README.md` and add a
  pointer in the repo-root `README.md` under "Documentation".

Reviewers bounce a PR that adds a federation event, route, crypto change,
schema change, architectural shift, principle change, or test-strategy change
without touching the matching doc. The check is "did the author update the
matching doc?", not "are the docs perfect?" — incremental accuracy beats a big
bang.

### What to Never Do

- Import inside a function/method body — all imports at top; `if TYPE_CHECKING:`
  for type-only circular imports.
- Add env-var-gated stubs or dual code paths in production code to ease testing.
  Tests mock at the boundary (`sys.modules` injection or `unittest.mock.patch`);
  production always uses the real dependency.
- **Add a new key, table, registry, or event when an existing one already proves
  what you need.** Authorize by verifying a signature against a key the receiver
  already stores — not a parallel allow-list/roster; reuse an existing content
  key rather than minting a per-audience variant. New persistent structure (a
  key, a table, a migration) has a high bar: reach for it only after reusing an
  existing primitive is shown insufficient. (E.g. the GFS authorizes a space
  relay by verifying a space-authority signature against the space pubkey it
  already pins — no publisher-roster table, no separate subscriber key. The
  §24.11 pipeline + the §"audit before a migration" rule are the same instinct:
  prefer the existing shape.)
- Import `aiolibdatachannel` without alias — use `import aiolibdatachannel as rtc`.
- `import *`.
- Commit `.env` files or secrets.
- Change the LICENSE / SPDX identifier without explicit instruction — all
  source is MPL-2.0.
- `broadcast_to_all()` for space-scoped events — use `broadcast_to_space_members()`.
- Inline a `member.role == "subscriber"` check on a space write path — call
  `_assert_writable_member()` (or `_reject_subscriber()` if the row isn't
  fetched yet) so subscriber gating stays uniform.
- Compare `member.role` against bare role strings — import `SpaceRole` from
  `domain.space` (`OWNER` / `ADMIN` / `MEMBER` / `SUBSCRIBER`). The schema
  CHECK is the on-disk authority; the enum is the in-code authority.
- Store passwords, emails, phone numbers, or GPS coordinates in federation
  payloads.
- Bypass `_require_space_admin()` / `_require_space_member()` guards.
- Add an endpoint without a matching integration test.
- Add bootstrap logic to `run.sh` — it belongs in `ha_bootstrap.py`.
- Assume `SUPERVISOR_TOKEN` is present — only set in `haos`. Query
  `adapter.capabilities` (`Capability.INGRESS in caps`), not the env var.
- **Guess a Home Assistant API contract.** When SH fetches from HA or calls
  an HA service (REST `/api/...`, WS commands, `notify.*` / other service
  names, entity & attribute shapes), verify the endpoint/shape against HA's
  source or developer docs (developers.home-assistant.io, the REST/WS API
  reference, the relevant integration) — never infer it from a
  plausible-looking pattern. Cite the contract in a comment/PR note, and
  surface HA call failures at **WARNING** (not DEBUG) so a wrong assumption
  is diagnosable, not silent. (The #497 push bug shipped
  `notify.mobile_app_{username}` — a guess; HA names that service after the
  device slug, so it silently 400'd for everyone.) HA surface lives in
  `socialhome/platform/ha/{client,providers}.py`.
- Branch on `config.mode` outside `platform/__init__.py` and `config.py`, or
  `isinstance`-check concrete adapters — consume the Provider interfaces
  (`adapter.auth` / `users` / `push` / …) so a fourth platform drops in
  without touching call sites.
- Auto-generate an admin password on first boot — `/setup` collects one, or
  `[standalone].admin_password` / `SH_ADMIN_PASSWORD` for headless. Never log
  a generated password.
- Write a function-based route handler — use a `BaseView` subclass
  (`routes/base.py`), grouped by URL resource.
- Add try/except for domain exceptions in a handler — `BaseView._iter` maps
  them. Only catch for a non-standard response code.
- Write SQL in a route handler.
- Write SQL in a service — extract into `Abstract*Repo` + `Sqlite*Repo`
  (exceptions: `backup_service`, `data_export_service`, `recovery_kit_service`).
- **Store local time in DB timestamps — store UTC.** Two shapes coexist:
  SQLite `datetime('now')` → naive `"YYYY-MM-DD HH:MM:SS"`; Python
  `datetime.now(timezone.utc).isoformat()` → tz-aware `"…+00:00"`. Both UTC,
  both fine — match whichever the file already uses so one column doesn't mix.
  Locale conversion is the **SPA's job** at the boundary, never repo/service/
  route — relative labels via `client/src/utils/relativeTime.ts:parseDelta`
  (treats naive as UTC), absolute via `toLocaleString(undefined, ...)`.
  Federation wire payloads keep tz-aware ISO 8601 (a protocol field, separate
  from any DB column).
- Declare a row-shaped `@dataclass` in `repositories/` — it belongs in
  `domain/` (re-export from the repo module if existing imports need it).
- Declare a service constructor taking `db: AsyncDatabase` when an
  `Abstract*Repo` already covers its needs.
- Inline service/repo construction in `create_app` — wire it in `_build_repos`
  / `_build_services` / `_build_middleware`.
- Roll your own `_running: bool` scheduler loop — use the `_stop:
  asyncio.Event` pattern (`replay_cache_scheduler.py`).
- Call sync filesystem APIs from `async def` — see "Filesystem I/O" above. The
  DM media + highlight streaming path was rebuilt on this rule; a new sync FS
  call reverts a recent fix.
- Run CPU-bound work directly in `async def` — see "CPU-bound work" above.
- `time.sleep(...)` in async code — use `await asyncio.sleep(...)`; a blocking
  sleep in a scheduler tick stalls every coroutine for the duration.
- Create a new migration without incrementing the number.
- Add/rename/remove a `FederationEventType` or HTTP endpoint without updating
  `docs/protocol/` or `docs/api.md` in the same commit — see "Keep docs in
  sync".
- `window.location.href` / `.assign` / `.replace` to an absolute path in the
  SPA — use `basePath` (app root) or `addBase('/path')` (in-app); route
  `fetch('/api/...')` through the `api` client. See "Frontend (SPA) & ingress".
- Gate `isAuthed` on `token != null` — under haos the SPA carries no token;
  the gate is `currentUser != null`. See "Frontend (SPA) & ingress".
- Return a bearer token from a haos setup endpoint — ingress is the only entry.
  Standalone/ha setup DO return a token; haos returns `{username}` and the SPA
  cold-start probes `/api/me` via ingress headers.
