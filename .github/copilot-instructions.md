# GitHub Copilot Instructions — socialhome

### Project
Social Home: privacy-first federated social platform for Home Assistant.
Language: Python 3.14 + aiohttp + SQLite. Frontend: Preact + TypeScript.
Architecture: platform adapter pattern, service layer, repository pattern.

### Key conventions
- Route handlers are `BaseView` subclasses (`routes/base.py`), one class per
  REST resource. Use `self.svc(key)`, `self.user`, `self._json(data)`.
  No function-based handlers. No per-handler try/except — `BaseView._iter`
  maps domain exceptions centrally
- Services depend on `Abstract*Repo` Protocols, not on `Sqlite*Repo`
  concretes — and not on `db: AsyncDatabase` directly when a repo covers
  the operation
- All I/O is async. Use asyncio.sleep(), never time.sleep()
- GPS coordinates always truncated to 4dp before storage or transmission
- Federation events: Ed25519 signature verified BEFORE any state mutation
- Push notifications: title only — never include message content or coordinates in body
- Use AsyncDatabase.enqueue() for writes; fetchall()/fetchone() for reads —
  inside the repo only
- Domain dataclasses in domain/: frozen, slots=True, no I/O. Row-shaped
  dataclasses live in domain/, not repositories/
- Background loops use `_stop: asyncio.Event` (template:
  `replay_cache_scheduler.py`) — never `_running: bool`
- `create_app` only orchestrates — wiring goes in the `_build_*` factories
- Multi-write handlers needing atomic event publish: `async with
  UnitOfWork(db, bus=bus)` (`db/unit_of_work.py`)
- Composable list/search reads: build a `Spec` and call `repo.find(spec)`
  (`repositories/_spec.py`)
- New federation transports satisfy `TransportStrategy`, new envelope
  crypto satisfies `EncryptionStrategy` (`federation/strategies.py`)
- New inbound federation validation steps go into the `InboundPipeline`
  chain (`federation/inbound_validator.py`) — never edit
  `handle_inbound_webhook` directly

### What not to generate
- Imports inside functions or methods — all imports at the top of the file
  (only exception: `if TYPE_CHECKING:` blocks for circular deps)
- Env-var-gated stubs in production code for testing — mock at the test
  boundary (sys.modules injection or unittest.mock.patch)
- Subclasses named *Addendum or *Extension — always edit the original class
- SQL in route handlers OR service methods — belongs in repositories
  (exceptions: backup_service, data_export_service — whole-table dumps)
- A row-shaped `@dataclass` declared in `repositories/` — put it in `domain/`
  and re-export from the repo module
- Inline `Sqlite*Repo()` construction in `create_app` — extend `_build_repos`
- `_running: bool` scheduler loops — copy the `_stop: asyncio.Event` pattern
- print() statements — use logging.getLogger(__name__)
- Hard-coded HA-specific logic outside platform/ha_adapter.py
