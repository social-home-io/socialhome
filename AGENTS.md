# AGENTS.md — social-home

AI agent instruction file. Read before editing any code in this repository.
Canonical spec: spec_work.md — the spec is always right.

### Architecture rules
- Services → `BaseView` subclass routes. Repos → data only. Domain → pure dataclasses.
- Route handlers are `BaseView` subclasses (see `routes/base.py`), one class
  per REST resource, dispatched by HTTP method. No function-based handlers.
  No per-handler try/except — `BaseView._iter` maps exceptions centrally.
- Platform adapter pattern: all HA-specific code in platform/ha_adapter.py only.
- All I/O is async. No blocking calls outside run_in_executor.
- Services depend on `Abstract*Repo` Protocols, not on `Sqlite*Repo`
  concretes — and not on `db: AsyncDatabase` directly when a repo covers
  the operation.
- `create_app` only orchestrates. New service/repo wiring goes into the
  `_build_repos` / `_build_services` / `_build_middleware` factories.
- Background loops use `_stop: asyncio.Event` + `while not self._stop.is_set()`
  (see `replay_cache_scheduler.py`). No `_running: bool` flags.

### Patterns to follow
- Match the error handling pattern in existing route handlers (_map_exc).
- Use AsyncDatabase.enqueue() for all writes. fetchall()/fetchone() for reads.
  Both are called from inside the **repo**, not from the service.
- Domain events via EventBus.publish() — never call WS manager directly from services.
- Ed25519 envelope validation always runs before business logic in federation handlers.
- For multi-write handlers that must publish events atomically, use
  `async with UnitOfWork(db, bus=bus)` (`db/unit_of_work.py`).
- For composable list/search reads, use `Spec` + `repo.find(spec)`
  (`repositories/_spec.py`). Bespoke `list_*` methods stay for common cases.
- New federation transports satisfy `TransportStrategy`; new envelope
  crypto satisfies `EncryptionStrategy` (`federation/strategies.py`).
- New inbound validation steps are added by appending to the
  `InboundPipeline` chain (`federation/inbound_validator.py`). Never edit
  the monolithic `handle_inbound_webhook` directly — each step is its own
  async callable with isolated tests.

### Patterns to avoid
- No Addendum/Extension subclasses. Merge into the original class.
- No print() statements. Use logging.getLogger(__name__).
- No imports inside functions or methods — all imports go at the top of
  the file. Only exception: `if TYPE_CHECKING:` blocks for circular deps.
- No env-var-gated stubs in production code for testing. Tests mock at the
  test boundary (sys.modules injection or unittest.mock.patch).
- No GPS coordinates without 4dp truncation: round(float(lat), 4).
- No user-generated content in push notification bodies.
- No SQL in route handlers or service methods — that belongs in repositories.
  (Exceptions: `backup_service`, `data_export_service` — whole-table dumps.)
- No row-shaped `@dataclass` declared in `repositories/`. Move it to `domain/`
  and re-export from the repo module so existing imports keep working.

### Keep docs in sync
Docs live in `docs/`. Ship the matching doc update in the same commit:
- New / renamed / removed `FederationEventType` → the matching page
  in `docs/protocol/` (event-type list, Mermaid diagram if the flow
  changed). New feature → new page, copy `docs/protocol/pairing.md`
  as the template, link from `docs/protocol/README.md`.
- New / renamed / removed HTTP endpoint → the matching table in
  `docs/api.md` (plus the "Rate limits" table if applicable).
- New WebSocket frame type → the WebSocket section in `docs/api.md`.
- Crypto suite change (signature, KDF, envelope format) →
  `docs/crypto.md`.
- New top-level doc file under `docs/` → link from `docs/README.md`
  and from the repo-root `README.md`.

### File locations
- Business logic: social_home/services/
- Data access: social_home/repositories/
- Domain types: social_home/domain/
- Route handlers: social_home/routes/ (or app.py for small handlers)
- Migrations: social_home/migrations/00NN_description.sql
- Documentation: docs/ (protocol pages, API reference, crypto notes)
