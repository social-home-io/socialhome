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

Run only the release-blockers:

```sh
pytest tests/protocol/ -m security
```

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
| Federation payload | Outbound envelopes encrypt every field except the §24.11 routing keys (`event_type`, `from_instance`, `to_instance`, `space_id`, `epoch`). |
| API response | `SENSITIVE_FIELDS` (in `socialhome/security.py`) never appear in API responses. |
| WebSocket broadcast | Per-event WS payloads exclude fields that should be local-only. |
| Presence privacy | GPS is 4dp-truncated; instance_id leakage is gated by opt-in. |
| Space authorship | `test_space_content_authorship.py` delivers every row-writing space event type through the real handler registry from the rightful household (the write must land — a positive control per type) and from other member households, a moderator, the host and a seatless household (a refused case must leave every content table unchanged), covering creates, re-sends over existing / moderated / bot rows, owned edits / deletes, collaborative edits, personal actions (votes, RSVPs, bids), owner-only state changes and moderator-only zones; plus every §25.6 catch-up resource from the host and from other member households, and writes that beat the roster gossip (held, then replayed when the seat or a roster snapshot lands). A new type or resource fails the tripwire until it has both. |
| Space scope | `test_space_content_scope.py` sends every space-content event type (the `SPACE_WRITE_EVENT_TYPES` vocabulary, enumerated against the real handler registry) gated for one space but naming rows of another space or the household's own rows, plus the same through the §25.6 sync receiver, and asserts every content table is unchanged. A new space-write event type fails the tripwire until it has a cross-space case. |
| Media blob scope | `test_media_blob_scope.py` sends `SPACE_MEDIA_BLOB` / `DM_MEDIA_BLOB` aimed at another space's rows, another household's or a local member's DM, existing files and unsafe names, and asserts the data directory and message rows are unchanged (write-once). |
| Calendar RSVP scope | `test_personal_calendar_rsvp_scope.py` sends personal-calendar RSVP updates/deletes for events not shared with the sender, for local members and for other households' invitees, and asserts no RSVP row changes. |

The encryption-first rule (§25.8.21) is the load-bearing invariant
behind these tests — see [`principles.md`](./principles.md) for why
it's a hard line.

Run them before every commit that touches federation or presence
code:

```sh
pytest tests/protocol/ -m security
```

## Frontend tests

Frontend tests live in the client tree, not under `tests/`:

- **Vitest + `@testing-library/preact`** for Preact components.
  Test files sit next to source: `client/src/components/Foo.test.tsx`
  next to `Foo.tsx`.
- **`tsc --noEmit`** for type checks.
- **ESLint** for lint.
- **`vite build`** at pre-push.

Run the full client suite:

```sh
cd client && pnpm vitest run
```

## CI

`.github/workflows/ci.yml` runs four jobs in parallel:

1. **`test (3.14)`** — `pytest --cov=socialhome --cov-branch
   --cov-fail-under=90 --durations=25` (the whole suite, `tests/protocol/`
   included, parallel via xdist), then `pytest tests/protocol/ -m security`
   again as the explicit release-blocker gate
2. **`lint`** — `ruff check .` + `ruff format --check .`
3. **`typecheck`** — `mypy socialhome/`
4. **`frontend`** — `pnpm lint`, `pnpm typecheck`, `pnpm build`

Pre-commit hooks (`.pre-commit-config.yaml`) run a strict subset of
the same on every commit, plus `pnpm build` at pre-push. **Never
pass `--no-verify`**: when a hook fails, fix the underlying issue —
fixtures, factories, and shared utilities are designed so the gate
is achievable.

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
- **1 ms write-batch window.** `AsyncDatabase` holds each write batch open
  for the whole window, so every *sequential* `enqueue` costs one window.
  Test databases that ask for the suite's "fast" 10 ms — or never choose a
  window and get the production 500 ms (`GfsApp`, a bare `Config(...)`) —
  run with 1 ms instead. A window a test sets on purpose (the batching
  tests' 200 ms) is kept.

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
  boundary.
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
