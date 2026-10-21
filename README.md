# Social Home — `core`

The Python + Preact application that runs inside a household. Federates
peer-to-peer with other households, optionally subscribes to a Global
Federation Server (GFS) for public spaces. Runs as a Home Assistant
add-on or a standalone Docker container.

## Develop

### VS Code + Dev Container (recommended)

The repo ships a Dev Container (`.devcontainer/devcontainer.json`)
that installs everything for you on first open: the Python project
in editable mode at the locked versions (`uv sync --locked --extra dev`),
the pre-commit hooks,
pnpm, and the frontend deps under `client/`.

1. Open the repo in VS Code with the **Dev Containers** extension
   installed; pick **Reopen in Container** when prompted.
2. Wait for the container to build (a few minutes the first time).
3. Press **Ctrl+Shift+B** (or **Terminal → Run Build Task…**) to
   launch **Dev: backend + frontend** — the backend boots in
   standalone mode against `/tmp/sh-dev`, and Vite starts at
   <http://localhost:5173> with `/api` + `/ws` proxied to the
   backend on `:8099`. Nothing is forwarded automatically: open the
   **Ports** view and forward `5173` (and `8099` if you need the API
   directly) when you want to reach them from your host browser.
4. The first request lands on `/setup`, where you pick the admin
   username + password.

The other tasks under **Terminal → Run Task…**:

- **Run backend (standalone)** — backend only.
- **Run frontend dev** — Vite only.
- **Clean test data (/tmp/sh-dev)** — wipe the throwaway data
  dir so the next backend launch drops you back at the wizard.
  Stop the backend first.

`.vscode/launch.json` includes a **Debug backend (standalone)**
config and a **Debug current pytest file** config (uses
`debugpy`).

The container uses [`uv`](https://github.com/astral-sh/uv) as
the Python package manager and provisions a project venv at
`.venv/` (the system Python's site-packages is root-owned, so a
local venv keeps installs writable). VS Code is wired to use
`${workspaceFolder}/.venv/bin/python` as the default
interpreter. Reinstall deps with
`uv sync --locked --inexact --extra dev` — every Python package,
transitive ones included, is pinned in `uv.lock`. To change a version,
edit the pin in `pyproject.toml` (or move a transitive one with
`scripts/lock.sh --upgrade-package <pkg>`) and run `scripts/lock.sh`
(it only updates `uv.lock` — the images export it at build time);
see [Dependency lock](docs/testing.md#dependency-lock).

### Manual setup

If you're not using the Dev Container, do the same one-time
setup:

```sh
uv sync --locked --extra dev && .venv/bin/pre-commit install
cd client && pnpm install
```

(Without uv, `pip install -e .[dev]` works too, but resolves transitive
dependencies freshly instead of using the lock.)

Run the backend in standalone mode with a throwaway data dir under
`/tmp`. The first request lands on `/setup` so you can pick the admin
username + password through the wizard:

```sh
SH_MODE=standalone SH_DATA_DIR=/tmp/sh-dev python -m socialhome
```

In a second terminal, start the frontend dev server (Vite proxies
`/api` and `/ws` to `localhost:8099`):

```sh
cd client && pnpm run dev --host
```

Open the URL Vite prints (typically <http://localhost:5173>). Reset
the dev instance any time by stopping the backend and `rm -rf
/tmp/sh-dev` — the next start drops you back at the wizard.

Run the test suite with `pytest` (backend) and `pnpm exec vitest run`
(frontend, from `client/`).

## Documentation

- [`docs/principles.md`](docs/principles.md) — design principles.
- [`docs/architecture.md`](docs/architecture.md) — HFS ↔ GFS topology,
  identity, sync tiers, space crypto, resilience.
- [`docs/database.md`](docs/database.md) — v1 schema reference,
  grouped by domain.
- [`docs/testing.md`](docs/testing.md) — test strategy + the 90 %
  coverage gate.
- [`docs/api.md`](docs/api.md) — HTTP + WebSocket API reference.
- [`docs/crypto.md`](docs/crypto.md) — cryptographic design.
- [`docs/protocol/`](docs/protocol/) — federation protocol,
  feature-by-feature.
- [`docs/operations/household-dashboard.md`](docs/operations/household-dashboard.md)
  — the wall-tablet household dashboard: enabling it, ports, LAN-only
  guidance, and ESPHome wall panels (firmware, web installer, pairing).
- [`docs/display-api/`](docs/display-api/README.md) — the wall-panel
  JSON API: versioning, JSON Schemas, fixtures.
- [`spec_work.md`](../../spec_work.md) — authoritative specification.
  When code and spec disagree, the spec wins.
- [`CLAUDE.md`](CLAUDE.md), [`AGENTS.md`](AGENTS.md) — guidance for
  AI assistants working in this repo.

## License

[Mozilla Public License 2.0](LICENSE).
