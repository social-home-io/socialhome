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
| GFS payload | The GFS sees routing metadata only — never plaintext content, votes, names, or message bodies. |
| Federation payload | Outbound envelopes encrypt every field except the §24.11 routing keys (`event_type`, `from_instance`, `to_instance`, `space_id`, `epoch`). |
| API response | `SENSITIVE_FIELDS` (in `socialhome/security.py`) never appear in API responses. |
| WebSocket broadcast | Per-event WS payloads exclude fields that should be local-only. |
| Presence privacy | GPS is 4dp-truncated; instance_id leakage is gated by opt-in. |

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

1. **`test (3.14)`** — `pytest --cov=socialhome --cov-fail-under=90`
2. **`lint`** — `ruff check .` + `ruff format --check .`
3. **`typecheck`** — `mypy socialhome/`
4. **`frontend`** — `pnpm lint`, `pnpm typecheck`, `pnpm build`

Pre-commit hooks (`.pre-commit-config.yaml`) run a strict subset of
the same on every commit, plus `pnpm build` at pre-push. **Never
pass `--no-verify`**: when a hook fails, fix the underlying issue —
fixtures, factories, and shared utilities are designed so the gate
is achievable.

## Spec references

- §27 — test strategy (this page)
- §27.1 — principles (90 % coverage, pytest, no real I/O in unit
  tests)
- §27.5 — integration tests
- §27.6 — federation tests
- §27.9 — protocol / data-minimisation tests
- §25.8.21 — encryption-first rule (load-bearing for `tests/protocol/`)
