# Social Home — `core`

The Python + Preact application that runs inside a household. Federates
peer-to-peer with other households, optionally subscribes to a Global
Federation Server (GFS) for public spaces. Runs as a Home Assistant
add-on or a standalone Docker container.

## Run

```sh
docker build -t socialhome:dev .
docker run --rm -p 8099:8099 -v /tmp/sh-data:/data socialhome:dev
```

For Home Assistant, set `SH_MODE=ha`; the Supervisor provides
`/data` + `SUPERVISOR_TOKEN` automatically.

## Develop

```sh
pip install -e .[dev] && pre-commit install
pytest
cd client && pnpm install && pnpm run dev
```

## Documentation

- [`docs/`](docs/) — API reference, cryptography, and the federation
  protocol page-by-page.
- [`spec_work.md`](../../spec_work.md) — authoritative specification.
  When code and spec disagree, the spec wins.
- [`CLAUDE.md`](CLAUDE.md), [`AGENTS.md`](AGENTS.md) — guidance for
  AI assistants working in this repo.

## License

[Mozilla Public License 2.0](LICENSE).
