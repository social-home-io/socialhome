# CLAUDE.md — socialhome

This is the Claude Code instruction file for the Social Home core application.
Read spec_work.md (the project specification) before making any changes.
The spec is the source of truth — if code and spec disagree, fix the code.

### Architecture

- **Platform adapters:** `SOCIAL_HOME_MODE=ha` → `HomeAssistantAdapter`,
  `=standalone` → `StandaloneAdapter`. ALL adapter-specific code lives in
  `socialhome/platform/`. Never branch on `config.mode` outside `platform/`
  and `config.py`. Never `isinstance`-check concrete adapters — use protocol
  methods (e.g. `adapter.supports_bearer_token_auth`). Adapter lifecycle hooks
  (`on_startup`, `on_cleanup`, `get_extra_services`, `get_extra_routes`) handle
  mode-specific wiring. Platform events for HA automations are defined in
  `HaBridgeService` — add new event types there, not in `app.py`.
- **Service layer:** all business logic lives in `socialhome/services/`.
  Route handlers in `routes/` are thin `BaseView` subclasses — one view class
  per REST resource, dispatched by HTTP method (`get()`, `post()`, `patch()`,
  `delete()`). Use `self.svc(key)` for service access, `self.user` for auth,
  `self._json(data)` for responses. Domain exceptions are mapped centrally
  by `BaseView._iter()` — individual handlers do NOT need try/except blocks.
  See `routes/base.py` for the base class.
  No SQL in route handlers. No business logic in repositories.
- **Repository pattern:** `repositories/` contains abstract base classes and
  SQLite implementations. Services depend on `Abstract*Repo` Protocols only —
  never on concrete `Sqlite*Repo` implementations. The two whole-table-dump
  services that need raw SQL (`backup_service`, `data_export_service`) are
  the documented exception.
- **No SQL outside `repositories/`:** services and routes call repo methods,
  not `db.fetchall` / `db.enqueue` / `db.transact` directly. Adding new SQL to
  a service is a smell — extract the query into the appropriate repo.
- **Domain layer:** `domain/` contains pure dataclasses — no I/O, no imports from
  services or repos. Keep them frozen (`@dataclass(slots=True, frozen=True)`).
  Row-shaped dataclasses live in `domain/`, not `repositories/`. The only
  exception is repo-local DTOs (e.g. `SearchHit`) that never escape the repo
  layer; document them with "repo-local DTO" in the class docstring.
- **`create_app` stays thin:** new wiring goes into the matching `_build_*`
  factory in `app.py` (`_build_repos`, `_build_middleware`). The `create_app`
  body should only orchestrate, not enumerate.
- **Async everywhere:** all I/O is `async def`. Never use `time.sleep()` —
  use `asyncio.sleep()`. Never call blocking I/O without `run_in_executor`.
- **Schedulers follow the `asyncio.Event` lifecycle.** Every background loop
  takes the shape: `_stop: asyncio.Event` set in `stop()`, drained in `start()`,
  body is `while not self._stop.is_set()`. Reference template:
  `infrastructure/replay_cache_scheduler.py`. Do not introduce a `_running:
  bool` flag instead — that pattern is gone from the codebase.

### Design patterns

- **Registry** for federation event dispatch. New inbound event handlers
  register via `_event_registry.register(event_type, handler)` in the
  service's `attach_*` method. Never add if/elif branches to
  `_dispatch_event`. Reference: `federation/event_dispatch_registry.py`.
- **Middleware chain** for the §24.11 inbound federation validation pipeline.
  Each validation step (JSON parse, instance lookup, timestamp check, signature
  verify, replay check, decrypt, idempotency, ban check, persist replay) is an
  independently-testable async callable composed via `InboundPipeline`. New
  steps (quota enforcement, sealed-sender unseal) are added by appending to
  the chain — never by editing the monolithic `handle_inbound_webhook`. The
  same chain validates both HTTPS-webhook and DataChannel-delivered envelopes.
  Reference: `federation/inbound_validator.py`.
- **Strategy** for transport + envelope crypto. Concrete classes
  (`WebhookTransport`, `_RtcPeer`, `FederationEncoder`) satisfy the
  `TransportStrategy` / `EncryptionStrategy` Protocols in
  `federation/strategies.py`. New transports / crypto schemes plug in by
  satisfying the protocol — no `FederationService` edits required.
- **Specification** for repo list/search reads with arbitrary filter
  combinations. Build a `Spec(where=[...], order_by=[...], limit=N)` and
  call `repo.find(spec)`. The repo's `_*_COLS` allow-list defends the
  column names against injection. Keep bespoke `list_*` methods for the
  common cases — Spec is the escape hatch, not a wholesale replacement.
  Reference: `repositories/_spec.py`, used by `notification_repo` and
  `post_repo`.
- **Unit of Work** for any handler that needs more than one write and at
  least one domain event to ship together. `async with UnitOfWork(db,
  bus=bus) as uow` opens a `BEGIN IMMEDIATE`, buffers `uow.exec(...)`
  writes, and dispatches `uow.publish(...)` events only after commit —
  so a handler crash never publishes events whose writes rolled back.
  See `db/unit_of_work.py`. Single-write handlers can keep using
  `db.enqueue` directly.

### Code Conventions

- Python 3.14+. Use `match/case` for event dispatch.
- Type hints on all public methods. Use `str | None` not `Optional[str]`.
- `log = logging.getLogger(__name__)` at module level — never `print()`.
- **All imports at the top of the file** — never import inside a function
  or method. The only exception is `if TYPE_CHECKING:` blocks for type
  annotations that would otherwise cause circular imports. If a top-level
  import causes a circular dependency, restructure the modules or use
  `TYPE_CHECKING`.
- SQLite: always use `AsyncDatabase.enqueue()` for writes (WAL coalescing).
  Use `fetchall()` / `fetchone()` for reads.
- Error handling: raise domain exceptions (`SpaceNotFoundError`, etc.) in services.
  Route handlers catch them with `_map_exc()`.
- Never add `*Addendum`, `*Extension`, or `*Complete` subclasses.
  Merge changes directly into the existing class.
- Never use inheritance to patch gaps. Fix the original class.

### Federation & Security

- Every inbound federation event MUST pass through the §24.11 validation pipeline:
  JSON parse → timestamp ±300 s → instance lookup → ban check → Ed25519 verify
  → replay cache → dispatch.
- Never skip signature verification for "trusted" instances.
- GPS coordinates: truncate to 4 decimal places before any storage or transmission.
- WebSocket auth uses `?token=` query parameter (unavoidable for browser WS).
  Raw API tokens appear in access logs and browser history — operators must
  redact tokens from logs. Never log the full query string of `/api/ws`.
  `round(float(lat), 4)` — never store raw precision from device.
- Push notification payloads: title only, body omitted for DMs, location messages,
  and any user-generated content (§25.3).
- `SENSITIVE_FIELDS` frozenset in `security.py` — never expose these in API responses.

### Testing

- Unit tests: no real network, no real disk. All repos are in-memory stubs.
- Integration tests: real SQLite in `tmp_path`, real aiohttp `TestClient`.
- Protocol tests in `tests/protocol/` are a **release blocker** — never skip them.
- Run `pytest tests/protocol/ -m security` before every commit touching federation
  or presence code.
- Coverage gate: 90% branch coverage. `pytest --cov=socialhome --cov-fail-under=90`.

### Pre-commit hooks

- Config: `.pre-commit-config.yaml`. Install once per clone with
  `pip install pre-commit && pre-commit install`.
- Hooks: ruff (lint + format), mypy (on `socialhome/`), frontend
  ESLint + `tsc --noEmit` on staged TS/TSX, and `pnpm build` at
  pre-push time.
- If a hook fails, fix the underlying issue — never pass `--no-verify`.

### Releases

- Triggered by pushing a `v*.*.*` git tag or publishing a GitHub
  Release. `.github/workflows/publish.yml` runs three jobs in
  parallel: PyPI (via OIDC trusted publishing), core Docker image
  to `ghcr.io/social-home-io/socialhome`, and GFS Docker image to
  `ghcr.io/social-home-io/gfs`.
- Bump the version in `pyproject.toml` before tagging.

### Encryption-First Rule (§25.8.21)

Every field in every outgoing federation event is encrypted unless the
federation service needs it in plaintext to route or validate the event.
When adding a new federation event:
- Put routing fields (event_type, from/to instance, space_id, epoch) in plaintext
- Put everything else — content, names, counts, choices — inside the encrypted payload
- Never add a `"payload": plaintext_fallback` pattern
- If `SpaceContentEncryption` is not configured, raise `RuntimeError` — never degrade silently

### Keep docs in sync

`docs/` is the public reference for the federation protocol and the
HTTP API. Trust erodes fast when docs drift from code, so treat the
matching doc file as part of the same change.

- **Added or renamed a `FederationEventType`?** Update the `Event types`
  list on the matching page under `docs/protocol/`. If the new event
  belongs to a feature that doesn't have a page yet (new feature), add
  one — copy the shape from `docs/protocol/pairing.md` (summary, scope,
  event types, Mermaid sequence diagram, implementation pointers, spec
  refs) and link it from `docs/protocol/README.md`.
- **Changed a feature's message flow** (new signalling step, new
  `_VIA` relay, transport swap)? Update the Mermaid sequence diagram
  on the matching page. Diagrams exist to be accurate, not
  decorative.
- **Added or removed an HTTP endpoint?** Update the matching table in
  `docs/api.md`. If the endpoint is rate-limited, add a row to the
  "Rate limits" table too. If it's a new WebSocket frame type,
  document it under the WebSocket row.
- **Changed the crypto suite** (signature algorithm, key derivation,
  envelope format)? Update `docs/crypto.md`.
- **Added a new top-level doc file under `docs/`?** Link it from
  `docs/README.md` and add a pointer in the repo-root `README.md`
  under "Documentation".

Reviewer checklist: if a PR adds a federation event, a route, or a
crypto change and the docs aren't touched, push back. The check is
"did the author update the matching doc?" — not "are the docs
perfect?" Incremental accuracy beats big bang rewrites.

### What to Never Do

- Never import inside a function or method body — all imports go at the
  top of the file. Use `if TYPE_CHECKING:` for type-only circular imports
- Never add env-var-gated stubs or dual code paths in production code to
  simplify testing. Tests mock at the test boundary (`sys.modules`
  injection or `unittest.mock.patch`). Production code always uses the
  real dependency.
- Import `aiolibdatachannel` without alias — use `import aiolibdatachannel as rtc`
- Never add `import *`
- Never commit `.env` files or secrets
- Never change the LICENSE file or SPDX identifier without explicit instruction — all source code is Mozilla Public License 2.0 (MPL-2.0)
- Never call `broadcast_to_all()` for space-scoped events — use `broadcast_to_space_members()`
- Never store passwords, emails, phone numbers, or GPS coordinates in federation payloads
- Never bypass `_require_space_admin()` / `_require_space_member()` guards
- Never add an endpoint without a matching integration test
- Never add bootstrap logic to `run.sh` — it belongs in `ha_bootstrap.py`
- Never assume `SUPERVISOR_TOKEN` is always present — check before calling Supervisor API
- Never write a function-based route handler — use a `BaseView` subclass
  (see `routes/base.py`). Group handlers by URL resource, not by function
- Never add try/except for domain exceptions in a handler — `BaseView._iter`
  handles it centrally. Only catch exceptions that need a non-standard
  response code not covered by the base mapping
- Never write SQL directly in a route handler
- Never write SQL directly in a service — extract it into the matching
  `Abstract*Repo` + `Sqlite*Repo` (the documented exceptions are
  `backup_service` and `data_export_service`, which dump whole tables)
- Never declare a row-shaped `@dataclass` in `repositories/` — it belongs
  in `domain/`. Re-export from the repo module if existing imports need it
- Never declare a service constructor that takes `db: AsyncDatabase`
  directly when an `Abstract*Repo` already covers its needs
- Never inline service/repo construction in `create_app` — add the wiring to
  `_build_repos` / `_build_services` / `_build_middleware` factories
- Never roll your own `_running: bool` scheduler loop — copy the
  `_stop: asyncio.Event` pattern from `replay_cache_scheduler.py`
- Never create a new migration without incrementing the number
- Never add / rename / remove a `FederationEventType` or an HTTP
  endpoint without updating the matching page in `docs/protocol/` or
  `docs/api.md` in the same commit. See "Keep docs in sync" above
