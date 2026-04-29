# Social Home — `core`

The Python + Preact application that runs inside a household. Federates
peer-to-peer with other households, optionally subscribes to a Global
Federation Server (GFS) for public spaces. Runs as a Home Assistant
add-on or a standalone Docker container.

## Develop

One-time setup:

```sh
pip install -e .[dev] && pre-commit install
cd client && pnpm install
```

Run the backend in standalone mode with a throwaway data dir under
`/tmp`. The first request lands on `/setup` so you can pick the admin
username + password through the wizard:

```sh
SH_MODE=standalone SH_DATA_DIR=/tmp/sh-dev python -m socialhome
```

In a second terminal, start the frontend dev server (Vite proxies
`/api` and `/ws` to `localhost:8099`):

```sh
cd client && pnpm run dev
```

Open the URL Vite prints (typically <http://localhost:5173>). Reset
the dev instance any time by stopping the backend and `rm -rf
/tmp/sh-dev` — the next start drops you back at the wizard.

Run the test suite with `pytest` (backend) and `pnpm exec vitest run`
(frontend, from `client/`).

## Documentation

- [`docs/`](docs/) — API reference, cryptography, and the federation
  protocol page-by-page.
- [`spec_work.md`](../../spec_work.md) — authoritative specification.
  When code and spec disagree, the spec wins.
- [`CLAUDE.md`](CLAUDE.md), [`AGENTS.md`](AGENTS.md) — guidance for
  AI assistants working in this repo.

## License

[Mozilla Public License 2.0](LICENSE).
