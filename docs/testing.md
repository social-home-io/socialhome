# Test strategy

How tests are organised, what counts as "release-blocking", and what
the coverage gate is. Distilled from §27 of `spec_work.md` plus the
actual layout under `tests/`.

CI runs the same commands listed below; pre-commit hooks run a strict
subset on every commit (`ruff`, `mypy`, frontend lint + typecheck) and
the full `pnpm build` at pre-push.

## Principles

- **Branch coverage gate: 90 %.** Configured in `pyproject.toml`
  (`[tool.coverage.report] fail_under = 90`). CI fails when coverage
  drops below.
- **`pytest` everywhere; no `unittest.TestCase`.** Async tests use
  `pytest-asyncio` with `asyncio_mode = "auto"`, so `@pytest.mark.asyncio`
  is implicit.
- **Plain async functions, no `TestXxx` classes.** Every test file is
  a flat list of `async def test_xxx(...)` functions.
- **Test files mirror the source tree.** A function in
  `socialhome/services/foo_service.py` gets its tests in
  `tests/services/test_foo_service.py`. Adding a new source file
  without its mirror test file is a smell.
- **No real network, no real disk in unit tests.** Repositories are
  in-memory stubs; HTTP calls are mocked via `aioresponses`. Real
  SQLite (in `tmp_path`) and a real `aiohttp` `TestClient` only
  appear in integration tests.
- **Federation tests** spin up two in-process instances sharing an
  in-memory queue — never sockets.
- **Tests mock at the test boundary.** No env-var-gated stubs or
  dual code paths in production code to make tests easier (see
  `CLAUDE.md` → "Never add env-var-gated stubs"). Mock with
  `unittest.mock.patch` or `sys.modules` injection at the test edge.

## Layout

The repo's `tests/` tree mirrors `socialhome/`:

```
tests/
├── conftest.py               shared fixtures (db, app, client, event bus)
├── factories.py              dataclass factories for domain types
├── test_app.py               app bootstrap regression tests
├── db/                       AsyncDatabase + Unit of Work
├── domain/                   pure dataclass behaviour
├── federation/               federation service, encoder, sync, RTC
│   └── sync/                 per-feature sync chunkers
├── global_server/            GFS routes + service
├── i18n/                     translation utilities
├── infrastructure/           schedulers, idempotency, key manager
├── media/                    image processor + thumbnail pipeline
├── platform/                 standalone, ha, haos adapters
│   ├── ha/
│   └── haos/
├── protocol/                 §27.9 release-blocker security tests
├── repositories/             every Sqlite*Repo
├── routes/                   one file per BaseView resource
├── scenarios/                multi-component end-to-end flows
├── serialization/            JSON shape regression tests
└── services/                 every service in socialhome/services/
```

## Markers

Two `pytest.ini_options` markers are registered:

| Marker | Meaning |
|---|---|
| `security` | Spec §27 release-blocker protocol / security test. Failure blocks deployment regardless of overall coverage. |
| `integration` | Touches a real SQLite (in `tmp_path`) and a real `aiohttp` `TestClient` (§27.5). |

Run only the release-blockers (the fast local pre-commit check):

```sh
pytest tests/protocol/ -m security
```

### Release-blocker guard

`tests/security_guard.py` (registered from `tests/conftest.py`) makes it
impossible for a `security` test to drop out of a run silently:

1. **Every test under `tests/protocol/` must carry the mark.** A module
   without `pytestmark = pytest.mark.security` is a collection error, so
   `-m security` always selects the whole directory.
2. **A skipped, xfailed or xpassed `security` test fails the run** — any
   run, the local `-m security` one included. A release blocker that
   can't run is a failure, not a yellow `s`.
3. **Deselected / never run** is caught in CI: the test run writes the
   outcome of every `security` test it ran (`--security-manifest=PATH`),
   and `pytest --collect-only -qq -n 0 --security-verify=DIR` then
   collects the whole tree and fails unless every collected `security`
   test is `passed` in the union of the manifests under `DIR` — a `-m` /
   `-k` / `--deselect` in the CI command, a crashed worker or a CI shard
   that never ran all show up as missing.

`tests/test_security_guard.py` pins each of these.

Run with coverage and the 90 % gate:

```sh
pytest --cov=socialhome --cov-fail-under=90
```

## Protocol data-minimisation tests (§27.9)

Tests under `tests/protocol/` are **release-blocking** — a failure
here blocks deployment regardless of overall coverage. They verify
that the protocol never transmits more information than strictly
necessary:

| Concern | What it asserts |
|---|---|
| GFS payload | `test_gfs_payload_minimization.py` drives the real producers, the real household sender and the real GFS with real crypto: the `/gfs/publish` body is exactly `{space_id, event_type, payload}` and the fan-out frame exactly `{type, space_id, event_type, payload}`, neither carrying the relaying household's id, the author, the post text or a location label; each relay payload's cleartext key set is asserted by *equality*, so a new visible field must consciously extend it. |
| GFS member publish | `test_gfs_member_publish_blind.py` drives the real GFS app with a real AES-GCM payload: the `/gfs/member-publish` request, the GFS logs, the queued row and the fan-out frame never carry the item's content or its real type (the outer type is always `space_item`, the frame names no publisher), and a body that tries to carry plaintext alongside the ciphertext — an extra field, a non-generic type, a structured payload — is refused with 400. |
| GFS strict member publish | `test_gfs_member_publish_strict_blind.py` (v_50) drives the real GFS app and the household's real publisher in a strict space: no request, GFS log, queued row or fan-out frame carries a household id or key, a household signature, the writer cert or its users; two households' requests are identical apart from per-request randomness; an identified publish into the strict space is refused; a trusted space with a pinned writer key (v_57) takes the anonymous publish just as blind. |
| GFS multi-listing | `test_gfs_multi_listing_blind.py` (v_57) drives two real GFS apps with the household's real connection service and publisher, registered on one of them: nothing that reaches the other — over any session — carries the household's id or key, a household signature, a cookie or the writer cert, and it keeps no client row for the household; a ciphertext sent anonymously never goes identified anywhere (not on a retry after the key is gone); targets come only from the owner's list (a listed private address we never joined is dropped before any request); a delisted server gets no retry; the host relay reaches the unregistered server identity-free. The per-type dedupe of copies arriving from several servers is pinned in `test_gfs_member_publish_items.py`. |
| GFS private-space channel | `test_gfs_private_channel_blind.py` (v_51) drives the real GFS app with the owner's real `GfsChannelService`, a link-joined member that takes the only seat and a paired member that publishes publish-only (trusted, then strict): no request (URL or body), GFS log record, database byte or fan-out frame carries the private space's id, name or authority key (hex or base64url), or the space-scoped writer cert; every request hits a body-only `/gfs/channels/*` route; only the link-joined household subscribes; a v_50 member gets no grant and a server without `private_channels` gets no channel. |
| Space GFS list | `test_space_gfs_servers_owner_only.py` drives the real inbound config handler, the real §D1b invite handler and redeem coordinator and the real space service: a delegated admin's own GFS list is ignored (on the host and a member mirror) while the owner's list it relays is taken; a replayed older owner list is ignored; an admin-issued invite or link seats the stub under the REAL owner, takes only the owner-signed list and never records the admin's key as the host's; the owner's later config (owner-only `private_gfs` / `gfs_publish_mode` / list) still reaches that household while the admin's does not; a forwarded `update_config` carrying a list leaves the owner's alone. |
| Owner statement | `test_space_owner_statement_join.py` drives the real invite handler, the real accept (`SpaceService.accept_remote_invite`) and snapshot builder: an invitation seats nothing before its accept; two pending invitations for one space id (MAL's claiming R's space, and R's) coexist and are both listed, accepting R's seats R's stub with no trace of MAL and expires MAL's, accepting MAL's seats under MAL as before 0093 and expires R's, and nothing MAL sends blocks or alters R's; two accepts racing for one space id seat exactly one and the loser's household never lands in `space_instances`; an invitation past its `expires_at` is still accepted, a declined one seats nothing, another user's cannot be accepted; a paired non-member that claims another household owns a space — with no statement, a forged / other-space / other-key statement, or the real owner's statement but no seed proof — only ever seats a stub naming itself; an accepted stub is never taken over; an honest admin's real snapshot seats the real owner and is trusted, and may re-invite; an ex-admin seating a household on the OLD key (after a rotation) loses it when the owner answers the catch-up with its current statement, or when the held statement ages out, and a stale statement is refused outright; a demoted inviter that relays the owner's new cert and fresh statement never regains trust without a re-proof with the new seed, while an honest admin's re-proof restores it, and the stream that carried the re-proof is not recorded clean; a `removed` older than the household's current join does not archive; an owner answers a never-seated household non-terminally with its statement, and a join in flight (an accepted invitation, or a live seat whose household row has not landed) is `not_seated`, never `removed`. |
| Inviting admin | `test_space_inviting_admin_trust.py` drives the real invite handler, inbound service and §25.6 receiver on a household a delegated admin invited: that admin's full stream (roster and another household's post) applies, its role changes, live bans / unbans and member removals apply, its owner-only settings (`private_gfs`, `gfs_publish_mode`) are ignored; another admin household gets the member rules; delegation off or a demoted seat ends the trust; an owner's invite records no inviter. |
| Link preview (receiver) | `test_link_preview_receiver_no_fetch.py` turns a received card — member payload, sync record, GFS relay inner — into a stored preview with the outbound fetcher and the preview builder patched to fail, and asserts a remote / traversal / `file:` image reference never survives. The GFS payload test above also carries a card and asserts none of its fields reach the connection server. `tests/test_outbound_fetch.py` (marked `security`) covers every SSRF refusal class of the guard. |
| Federation payload | Outbound envelopes encrypt every field except the §24.11 routing keys (`event_type`, `from_instance`, `to_instance`, `space_id`, `epoch`). |
| API response | `SENSITIVE_FIELDS` (in `socialhome/security.py`) never appear in API responses. |
| WebSocket broadcast | Per-event WS payloads exclude fields that should be local-only. |
| Presence privacy | GPS is 4dp-truncated; instance_id leakage is gated by opt-in. |
| Space authority rotation | `test_space_authority_rotation.py` runs two real applications (owner + member household) and moves the owner's real envelopes into the member's dispatch registry: after an admin household is demoted, removed, banned or leaves (or delegation is turned off) the owner rotates the space authority key (v_44), every config / roster / snapshot / rekey signed with the old key is refused by the member and the owner, a forged / replayed / same-epoch-other-key / unknown-suite cert moves nothing, a lagging member jumps to the latest epoch, the bundle resets state the revoked household inflated, the remaining admin gets the new seed, the owner never lets gossip raise a seat, households below v_44 get no bundle, and the GFS refuses old-key relays, handoffs, queries and forged certs. It also pins the review-round cases: an admin household dropping its own seat (`SPACE_REMOTE_MEMBER_REMOVED`) or a remove / ban of an already-tombstoned admin seat still rotates, a household removed by gossip gets no bundle or key, a late or repeated bundle never rolls back state accepted under the new key, the epoch outranks a restored owner's, a role-raising gossip on the host is dropped whole, and the remaining admin's seed goes out before the bundle. |
| Host-sequenced pages | `test_space_page_host_sequencing.py` (marked `security`) runs four real applications (host + three member households) over real SQLite in one space and delivers their real `SPACE_PAGE_*` payloads through the post-decrypt gates in chosen orders. Every probe of the earlier decentralised design is a test. P1: three concurrent appends end on one state and one `seq` in every delivery order. P2: a concurrent undo is kept. P3 / P3b: a household offline for 25 edits fast-forwards with no conflict, live and by sync. P4: a cover change converges. P5: one conflict side per user, the host's own edit still merges, one resolution converges everyone. P6: every household holds the identical conflict. Further cases: an unreachable host (drafts wait, one proposal per page, convergence on return) and stop-and-wait; a member forging the host's `seq` (live, chunk, resume) moves nothing; refusals (`access`, `gone`, `rate_limited`, `bad_base`, archived); moderated stale / force-is-fast-forward and a released resolution; mixed fleets (a v47 member, a v47 host); delete while pending or conflicted; the same-household stale save; proposals go to the host alone. `tests/services/test_page_conflict_service.py` adds the CPU benchmark: the adversarial `["a"]*2000` vs `["a","b"]*1000` merge finishes in < 250 ms with event-loop lag < 100 ms. |
| Space authorship | `test_space_content_authorship.py` delivers every row-writing space event type through the real handler registry from the rightful household (the write must land — a positive control per type) and from other member households, a moderator, the host and a seatless household (a refused case must leave every content table unchanged), covering creates, re-sends over existing / moderated / bot rows, owned edits / deletes, collaborative edits, personal actions (votes, RSVPs, bids), owner-only state changes and moderator-only zones; plus every §25.6 catch-up resource from the host and from other member households, and writes that beat the roster gossip (held, then replayed when the seat or a roster snapshot lands). A new type or resource fails the tripwire until it has both. |
| Space scope | `test_space_content_scope.py` sends every space-content event type (the `SPACE_WRITE_EVENT_TYPES` vocabulary, enumerated against the real handler registry) gated for one space but naming rows of another space or the household's own rows, plus the same through the §25.6 sync receiver, and asserts every content table is unchanged. A new space-write event type fails the tripwire until it has a cross-space case. |
| Media blob scope | `test_media_blob_scope.py` sends `SPACE_MEDIA_BLOB` / `DM_MEDIA_BLOB` aimed at another space's rows, another household's or a local member's DM, existing files and unsafe names, and asserts the data directory and message rows are unchanged (write-once). |
| Calendar RSVP scope | `test_personal_calendar_rsvp_scope.py` sends personal-calendar RSVP updates/deletes for events not shared with the sender, for local members and for other households' invitees, and asserts no RSVP row changes. |
| Inbound binding (non-space) | One protocol test per family sends events naming rows or people of another household, a local member or an unknown id, and asserts the tables (and, where relevant, published events and relayed sends) are unchanged, with an honest-flow control per family: `test_dm_scope.py` (messages, edits, deletes, reactions, typing, history), `test_personal_calendar_event_scope.py`, `test_user_sync_scope.py` (profile sync, removal, status, online state, contact requests), `test_highlight_scope.py`, `test_moment_scope.py`, `test_call_signal_scope.py`, `test_app_session_scope.py`, and the pairing cases in `test_pairing_auth_boundary.py`. |
| Inbound binding tripwire | `test_inbound_binding_tripwire.py` enumerates every non-space event type the real app registers a handler for and requires each to map to the protocol test proving its binding rule, or to a recorded reason there is nothing to bind. A new inbound handler fails it until one is added. |

The encryption-first rule (§25.8.21) is the load-bearing invariant
behind these tests — see [`principles.md`](./principles.md) for why
it's a hard line.

Run them before every commit that touches federation or presence
code:

```sh
pytest tests/protocol/ -m security
```

## Contract tests (wall-panel API)

`tests/dashboard/test_display_api.py` validates the real
`/api/display/v1/` payloads against the JSON Schemas in
`docs/display-api/v1/` (with `jsonschema`, a dev dependency) and compares
them with the documented fixtures in `docs/display-api/v1/fixtures/`.
After an intended payload change, regenerate the fixtures with
`SH_UPDATE_DISPLAY_FIXTURES=1 pytest tests/dashboard/test_display_api.py`
and review the diff — the firmware pins the same files.

The wall-panel map picture frames a household like the browser's Map tab:
`tests/dashboard/map_frame_vectors.json` holds shared framing cases that
both `tests/dashboard/test_map_frame.py` (the Python port,
`socialhome/dashboard/map_frame.py`) and
`client/src/dashboardMapScript.test.ts` (`map.js` in jsdom) assert. A
change to either framing must keep both green.

## Frontend tests

Frontend tests live in the client tree, not under `tests/`:

- **Vitest + `@testing-library/preact`** for Preact components.
  Test files sit next to source: `client/src/components/Foo.test.tsx`
  next to `Foo.tsx`.
- **The household dashboard's browser scripts** are plain files served
  by the backend (`socialhome/dashboard/static/`), but their behaviour
  is tested here too: `client/src/dashboardMapScript.test.ts` runs
  `map.js` in a fresh jsdom window per test (no layout, so the map box
  size is stubbed). Their markup and security guards stay in
  `tests/dashboard/`.
- **`tsc --noEmit`** for type checks — TypeScript 7 (the native
  compiler, installed as `@typescript/native`).
- **ESLint** for lint. typescript-eslint and madge still need the
  TypeScript 6 compiler API, which TypeScript 7 doesn't ship, so the
  `typescript` package name is aliased to `@typescript/typescript6`
  (its CLI is `tsc6`). Drop the alias once typescript-eslint supports 7.
- **`vite build`** at pre-push.

Run the full client suite:

```sh
cd client && pnpm vitest run
```

### Translation check (`pnpm i18n:check`)

`client/scripts/i18n-check.js` runs in the CI `frontend` job and fails when:

- a `t('key')` call in `client/src/` has no entry in `locales/en.json`;
- a shipped locale (`locales/_meta.json`) is missing an `en.json` key;
- a locale has a key that `en.json` no longer has;
- a translation's `{placeholder}` names differ from the English source.
  A `_one` plural may spell out its count instead.

Translations come in through Weblate (`client/weblate.yml`), which picks
up new English keys only after they land on `main`. A PR that adds keys
should translate them. If a PR deliberately leaves keys for Weblate, list
them per locale in `client/scripts/i18n-untranslated.json`:

```json
{ "de": ["feed.empty.title"], "fr": ["feed.empty.title"] }
```

Remove the entries once the Weblate PR with the translations is merged.
Unused `en.json` keys only print a warning, because some keys are built
at runtime.

### Self-hosted fonts check (`pnpm fonts:check`)

`client/scripts/fonts-check.js` runs in the CI `frontend` job right after
`pnpm build` and scans the built SPA (`socialhome/static/`), the GFS
static files and the household dashboard. It fails when a font CDN host
(`fonts.googleapis.com`, `fonts.gstatic.com`, …) appears anywhere, or when
a built `@font-face` URL is absolute (it would skip the HA ingress
`<base href>`), a `data:` URI (refused by `font-src 'self'`) or points at
a missing file. `client/src/styles/fonts.test.ts` (Vitest) and
`tests/test_no_third_party_fonts.py` guard the same rule in source.

### End-to-end: Home Assistant ingress

Unit tests (`client/src/baseUrl.test.ts`, `IngressLocationProvider.test.tsx`)
pin the URL helpers; they can't see a call site that bypasses them. The
`haos-ingress-ui` skill (`.claude/skills/haos-ingress-ui/`) runs the
real thing: the built SPA in headless Chrome (puppeteer-core, pinned in
`checks/package-lock.json`) behind a fake HA Supervisor + Core and an
ingress look-alike proxy under `/api/hassio_ingress/<token>/`, against a
real `SH_MODE=haos` backend. It walks the haos setup, every route in
`router.ts` on desktop and mobile, the live WebSocket, router history,
media upload, the iframe embed and the no-ingress-user 401 screen, then
audits every request: anything outside the prefix, a foreign origin or an
`Authorization` header fails the run.

```sh
python .claude/skills/haos-ingress-ui/harness.py verify --out /tmp/haos-ingress-ui
```

Exit 0 green, 1 a check failed, 2 setup failed; `report.md` and
screenshots land in `--out`. The fakes' HA contracts and how to read a
failure or add a check are in the skill's `SKILL.md`. Not part of
`pytest` or the coverage gate — it's its own CI job.

## CI

`.github/workflows/ci.yml` runs seven jobs in parallel. `test` and
`typecheck` install with `uv sync --locked --extra dev`, i.e. exactly the
versions in `uv.lock` (see [Dependency lock](#dependency-lock)).

1. **`test (3.14)`** — `pytest --cov=socialhome --cov-branch
   --cov-fail-under=90 --durations=25 --security-manifest=…` (the whole
   suite, `tests/protocol/` included, parallel via xdist), then
   `pytest --collect-only --security-verify=…`: the release-blocker gate
   checks that every `security` test passed in that run instead of running
   them a second time (see [Release-blocker guard](#release-blocker-guard))
2. **`lint`** — `ruff check .` + `ruff format --check .`
3. **`typecheck`** — `mypy socialhome/`
4. **`lock`** — `scripts/lock.sh --check`: fails when `uv.lock` no
   longer matches `pyproject.toml` (`uv lock --check`) or when
   `requirements/constraints*.txt` differ from a fresh export of the lock
5. **`images (amd64)` / `images (aarch64)`** — installs the locked set the
   way every shipped image does, natively on both arches: builds
   `Dockerfile.sh` and `Dockerfile.gfs` from a freshly built wheel and
   imports the native deps, then runs the Home Assistant App's builder
   step (`pip3 wheel --require-hashes -r requirements/constraints.txt`
   on the App's Alpine/musl base image). A lock bump that loses a wheel
   or hash for one of those platforms fails here, before a release
6. **`frontend`** — `pnpm build`, `pnpm fonts:check`, `pnpm typecheck`, `pnpm lint`, `pnpm lint:circular`, `pnpm i18n:check`, `pnpm test`
7. **`haos-ingress-ui`** — builds the SPA, installs the locked runtime
   set (`uv sync --locked`) and runs
   `.claude/skills/haos-ingress-ui/harness.py verify` against the
   runner's preinstalled Google Chrome (see
   [End-to-end: Home Assistant ingress](#end-to-end-home-assistant-ingress)).
   The report, request log and screenshots are uploaded as the
   `haos-ingress-ui-report` artifact on every run. It runs on every PR,
   not path-filtered: backend changes (auth middleware, `SpaIndexView`,
   the platform adapter) can break the prefix as easily as SPA changes

Pre-commit hooks (`.pre-commit-config.yaml`) run a strict subset of
the same on every commit, plus `pnpm build` at pre-push. **Never
pass `--no-verify`**: when a hook fails, fix the underlying issue —
fixtures, factories, and shared utilities are designed so the gate
is achievable.

## Dependency lock

Every Python package Social Home installs — direct and transitive, for
every extra — is pinned in [`uv.lock`](../uv.lock), a universal
resolution (any OS / arch, Python ≥ 3.14) made by
[uv](https://docs.astral.sh/uv/). CI, the dev container, both Docker
images and the Home Assistant App all install that one tested set, so a
rebuild of a tag months later gets the same packages.

| File | Contents | Installed by |
|---|---|---|
| `uv.lock` | everything, incl. `[dev]` + `[global-server]` | CI + dev container (`uv sync --locked --extra dev`) |
| `requirements/constraints.txt` | core runtime: version + sha256 of every file | `Dockerfile.sh`; the Home Assistant App, fetched by tag from `raw.githubusercontent.com/social-home-io/socialhome/<tag>/requirements/constraints.txt` |
| `requirements/constraints-gfs.txt` | core + `[global-server]` | `Dockerfile.gfs` |

The two `requirements/` files are generated from `uv.lock` by
`scripts/lock.sh` — never edit them by hand. The images install them
with `uv pip install --require-hashes -r …` (every file hash-checked),
then add the Social Home wheel with `--no-deps` and run `uv pip check`,
so a lock that disagrees with the wheel's own requirements fails the
image build. The App's builder runs the same two steps with `pip wheel`
on Alpine (musl); everything there has a musllinux wheel except
`aiolibdatachannel`, which it compiles from the locked, hashed sdist.
The App fetches `requirements/constraints.txt` from the git tag equal to
its `SOCIALHOME_VERSION` (the tag *is* the PyPI version, so the file at
the tag is exactly the set that release was tested and imaged with);
releases that predate the lock fall back to an unlocked
build there, and any newer release without the file fails the App build.
CI's `images` job runs that same musl step on every PR, so the lock can't
reach a release with a platform the App can't install.

How to change the set (run with [uv](https://docs.astral.sh/uv/) on
`PATH`; the dev container has it):

- **Bump a direct dependency:** edit its `==` pin in `pyproject.toml`,
  then `scripts/lock.sh`.
- **Bump one transitive dependency** (e.g. a security fix in `yarl`):
  `scripts/lock.sh --upgrade-package yarl`.
- **Refresh every transitive dependency** the pins allow:
  `scripts/lock.sh --upgrade`.
- **Check without changing anything:** `scripts/lock.sh --check` (what
  CI's `lock` job and the release workflow run).

Commit `uv.lock` and `requirements/` together with the `pyproject.toml`
change, and run the full suite on the new set (`uv sync --locked
--extra dev && pytest`). The `uv` version is pinned (`0.12.10`) in CI,
`publish.yml` and both Dockerfiles — bump every occurrence together.
Build-time tooling (`hatchling`/`hatch-vcs` for the wheel, the build
backend `pip` fetches to compile `aiolibdatachannel` in the App) is not
part of the lock.

## Warnings are errors

`[tool.pytest.ini_options] filterwarnings = ["error", …]` turns every
warning into a test failure, so a new one is fixed in the PR that adds it
instead of joining a summary nobody reads. The only exception is a narrow,
commented third-party one (aiohttp's advisory about posting a >1 MiB
`bytes` body, which the upload-limit tests do on purpose). The patterns
that used to warn, and what to do instead:

- **Close every `sqlite3` connection.** An `AsyncDatabase` a fixture
  starts is shut down in its teardown (`yield`, then `await
  db.shutdown()`). Raw connections in `tests/db/` go through
  `tests.db.connections.connect`, which closes them after the test.
- **Never mutate a started aiohttp app.** `app[key] = …` after startup is
  deprecated ("Changing state of started or joined application"). Route
  tests build the client with the double already in place:
  `await make_client({key: stub})` (or a callable that receives the
  started app), `client_with_stubs` for a stub fixture, and
  `tests.app_overrides.override_app_on_startup` for a hand-built app —
  the override is the app's last startup step, before the freeze.
  Patching a method on a service object (`monkeypatch.setattr(db,
  "fetchone", …)`) is fine; the app dict is what is frozen.
- **Route tests run the server with production's handler cancellation**
  (`ProductionLikeTestServer`, `handler_cancellation=False` like
  `web.run_app`). aiohttp's `TestServer` cancels a handler as soon as its
  client hangs up, which stranded files that a streaming handler had open
  mid-read.
- **Give an `AsyncMock` the plain attributes the code reads.** Every
  attribute of an `AsyncMock` is another `AsyncMock`; `fed.own_identity_pk.hex()`
  on one returns a coroutine nobody awaits.
- `asyncio_mode = "auto"` already marks async tests — a module-level
  `pytestmark = pytest.mark.asyncio` on a file with sync tests warns.

## Keeping the suite fast

Nearly all of the suite's historical wall time was tests **waiting**, not
working. Two mechanisms in `tests/conftest.py` (`_fast_test_databases`,
session-scoped, autouse) remove the costs every test used to pay, at the
test boundary — production code is untouched:

- **Migrated-schema template** (`tests/migration_template.py`). The first
  empty database per worker runs the real migration chain; every later
  empty database for the same migrations directory is a SQLite
  backup-API page copy of that result. A database that already has any
  schema (a test seeding a pre-migration shape, a restart) always runs the
  real runner. `tests/test_migration_template.py` pins that a restored
  database is identical to a freshly migrated one. Tests of the runner
  itself call `socialhome.db.migrations.run_migrations` directly and never
  see the cache.
- **1 ms write-batch window.** `AsyncDatabase` waits the whole window for
  companion statements before it commits, so every *sequential* `enqueue`
  costs one window. Test databases that ask for the suite's "fast" 10 ms — or
  never choose a window and get the production default (`GfsApp`, a bare
  `Config(...)`; 5 ms, formerly 500 ms) — run with 1 ms instead. A window a test sets on purpose (the batching
  tests' 200 ms) is kept.

- **Migration-chain snapshots** (`tests/migration_chain.py`). The
  per-migration tests (`tests/db/test_migration_00NN_*.py`, the GFS ones)
  build "every migration through N" on a fresh file before seeding rows —
  ~100 migrations, one commit each, per test. `apply_through` serves a
  database that holds only an empty `schema_version` from a per-process
  in-memory snapshot of that same replay (keyed by N, the migrations
  directory's fingerprint, the `schema_version` DDL, `foreign_keys` and
  transaction mode); anything already seeded or partially migrated takes
  the real replay. `tests/test_migration_chain.py` pins that a restored
  database matches a replayed one.

The app itself compiles the household-dashboard templates once per process
(`socialhome.dashboard.render.compiled_environment`) rather than once per
`create_app` — that compile was half of every app-backed test's setup.

Rules for new tests, from the root causes fixed so far:

- **Never wait out a production timeout or back-off.** Shrink the module
  constant for that test (`monkeypatch.setattr(cluster_mod,
  "SYNC_RETRY_DELAY_S", 0.01)`, `HELLO_TIMEOUT_SECONDS`,
  `ICE_BUFFER_TIMEOUT_S`) or pass the constructor knob
  (`ice_prime_timeout_s=`, `reconnect_delays=`). Release gates the test
  isn't about (`transport.mark_ice_primed()`).
- **Never touch the real network.** A hostname like `ha.local` or
  `gfs.test` that reaches a real client waits out DNS; inject the fake
  client (`ha_client=_FakeHaClient()`) or swap the factory at the
  boundary. `.local` names are mDNS: a resolver with nss-mdns waits ~5 s
  per lookup — an HA-mode app built from a bare `Config(mode="ha")` tries
  `homeassistant.local` several times at startup (18 s per backup-route
  test). Point `ha_url` at a refused loopback port
  (`http://127.0.0.1:9`) for "HA unreachable".
- **A reference request only needs the shape, not the full wait.** A test
  that proves "fails at once instead of waiting the budget" by comparing
  with a request that *does* wait can give that reference a tiny budget
  (`test_relay_failure_is_uniform_and_immediate`).
- **A fake server must behave like the real one at shutdown** — e.g. read
  its WebSocket so it answers the client's CLOSE, or the client's close
  handshake waits out aiohttp's timeout.
- Check with `pytest --durations=25`; CI prints the same list, so a new
  multi-second test is visible in every run.

## Spec references

- §27 — test strategy (this page)
- §27.1 — principles (90 % coverage, pytest, no real I/O in unit
  tests)
- §27.5 — integration tests
- §27.6 — federation tests
- §27.9 — protocol / data-minimisation tests
- §25.8.21 — encryption-first rule (load-bearing for `tests/protocol/`)
